"""Cognitive Recall Router: Level 1 活体卡直达 / Level 2 PPR 能量游走的统一召回入口"""
from __future__ import annotations

import logging
import re
import sqlite3
from functools import lru_cache
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import TIMESTAMP_FORMAT, ScratchpadManager
from baize.core.temporal_graph import BiTemporalGraphManager
from baize.core.visibility import SCOPE_DM, SCOPE_GROUP, group_view, is_group_visible, normalize_scope
from baize.intelligence.ppr_engine import PPREngine

log = logging.getLogger("baize.recall")

_PARTICLES = ("的", "什么", "怎么", "定的", "呢", "吗", "吧", "啊")
_TOKEN_RE = re.compile(r"[一-鿿A-Za-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]{2,}")
_DECAY_PER_DAY = 0.05
_RECENT_TURNS_CONFIDENCE = 0.35
_RECENT_TURNS_MAX = 3
_CANDIDATE_CONFIDENCE = 0.45
_CANDIDATE_MAX = 3
# Issue #39:纯语气/指示/连接词命中不算"接上了上一轮",它们在任何一句闲聊里都会出现。
_RECENT_TURN_STOPWORDS = frozenset({
    "可以", "好的", "好啊", "嗯嗯", "哦哦", "哈哈",
    "这个", "那个", "这样", "那样", "这边", "那边",
    "然后", "所以", "但是", "而且", "就是",
    "你说", "说的", "我说",
})
# 只有 2-gram 碎片撑不起一次召回:至少一个 >= 3 字的实义词,否则整层不触发。
_RECENT_TURN_MIN_CONTENT_LEN = 3

# Issue #93:文本兜底(DECISION_TEXT_MATCH)的准入与打分。
# 通用词:任何一句话里都会出现的代词、连接词、助动词和高频虚化名词。命中它们不说明「问的是这张卡」,
# 不计入准入证据。这是一张通用的语言功能词表,与具体卡片、具体题目无关。
_GENERIC_TERMS = frozenset(_RECENT_TURN_STOPWORDS | {
    "这些", "那些", "这里", "那里", "这么", "那么", "这种", "那种", "我们", "你们", "他们", "它们", "自己", "大家", "别人",
    "但是", "而且", "所以", "因为", "如果", "虽然", "然后", "或者", "以及", "并且", "还是", "只是", "不过", "另外", "同时",
    "否则", "既然", "即使", "除了", "要是", "其实", "当然",
    "已经", "一直", "可能", "应该", "需要", "能够", "不能", "不会", "不要", "不用", "没有", "不是", "还有", "比较", "非常",
    "真的", "一下", "一些", "一个", "一样", "一起", "有点", "有些", "再次", "马上", "刚才", "刚刚", "还要",
    "现在", "目前", "当前", "之前", "之后", "以后", "以前", "最近", "今天", "昨天", "明天", "时候",
    "知道", "觉得", "看看", "看到", "想要", "希望", "开始", "进行", "使用", "通过", "关于", "对于", "出现", "发现", "变成",
    "成为", "起来", "出来", "过来", "下去", "上去",
    "问题", "东西", "事情", "情况", "地方", "方面", "部分", "结果", "原因", "办法", "方式", "方法", "意思", "感觉", "样子",
    "什么", "怎么", "为什么", "多少", "哪个", "哪里", "是否", "是不是", "有没有", "能不能", "会不会", "可不可以",
})
_GENERIC_ENGLISH = frozenset({
    "the", "is", "to", "of", "and", "in", "it", "this", "that", "you", "are", "for", "with", "can", "not", "ok", "yes", "no",
    "do", "be", "on", "at", "as", "an", "or", "if", "so", "we", "my", "me", "he", "she", "they", "was", "were", "has", "have",
})
_GENERIC_BY_LENGTH = sorted(_GENERIC_TERMS, key=len, reverse=True)
_CJK_ONLY_RE = re.compile(r"[一-鿿]+")
# 卡片自己的 facts 里:一个词出现在超过这个比例的条目里,说明它在这张卡内部没有区分度
_FACT_COMMON_RATIO = 0.4
_FACT_COMMON_MIN_FACTS = 5
# facts 只在「同一条 fact」内累计证据:3 字以上的整词按 2 分、2-gram 按 1 分;命名卡要 2 分,无名无现行决策的卡要 3 分
_FACT_MIN_WEIGHT = 2
_FACT_MIN_WEIGHT_UNLABELED = 3
_FACT_SCORE_CAP = 4

Scored = Tuple[Dict[str, Any], Optional[float]]


def _strip_particles(text: str) -> str:
    for p in _PARTICLES:
        text = text.replace(p, "")
    return text.strip()


def _tokens(text: str) -> List[str]:
    return _TOKEN_RE.findall(text)


def _scoring_tokens(text: str) -> List[str]:
    """原词 + 中文 2-gram 滑窗双通道,去重保序。"""
    out: Dict[str, None] = {}
    for word in _tokens(_strip_particles(text)):
        out.setdefault(word, None)
        for run in _CJK_RUN_RE.findall(word):
            for i in range(len(run) - 1):
                out.setdefault(run[i:i + 2], None)
    return list(out)


@lru_cache(maxsize=8192)
def _is_generic_token(token: str) -> bool:
    """整个词是通用词,或由通用词拼成(如「这个问题」)。"""
    t = token.lower()
    if t in _GENERIC_TERMS or t in _GENERIC_ENGLISH:
        return True
    if len(t) < 3 or not _CJK_ONLY_RE.fullmatch(t):
        return False
    rest = t
    for term in _GENERIC_BY_LENGTH:
        rest = rest.replace(term, "")
        if not rest:
            return True
    return False

def _disjoint_grams(words: set, allowed: set, text: Optional[str] = None) -> int:
    """查询里命中的 2-gram 按「不重叠的片段」计数。

    相邻的 2-gram 共用一个字(如「需要重启」切出的「要重」「重启」),来自同一处文字,只算一份证据。
    allowed 限定可计入的 2-gram;text 不为空时还要求该 2-gram 出现在 text 里。
    """
    count = 0
    for word in words:
        for run in _CJK_RUN_RE.findall(word):
            i = 0
            while i < len(run) - 1:
                gram = run[i:i + 2]
                if gram in allowed and gram not in words and (text is None or gram.lower() in text):
                    count += 1
                    i += 2
                else:
                    i += 1
    return count

def _fact_evidence(facts: List[str], tokens: List[str], words: set) -> int:
    """facts 里「单条 fact」能给出的最高证据分。

    Issue #93:此前把全部 facts 拼成一个大文本再数命中,事实越多的卡越容易靠零散碎片被放行。
    现在只看单条 fact 内的非通用词命中,并丢掉在这张卡自己的 facts 里过于普遍的词。
    """
    if not facts:
        return 0
    usable = [t for t in tokens if not _is_generic_token(t)]
    if not usable:
        return 0
    texts = [f.lower() for f in facts]
    if len(texts) >= _FACT_COMMON_MIN_FACTS:
        usable = [t for t in usable if sum(1 for x in texts if t.lower() in x) <= _FACT_COMMON_RATIO * len(texts)]
    keep = set(usable)
    best = 0
    for text in texts:
        weight = 0
        for t in usable:
            if t in words and t.lower() in text:
                weight += 2 if len(t) >= 3 else 1
        weight += _disjoint_grams(words, keep, text)
        best = max(best, weight)
    return best

def _decay(updated_at: Optional[str], now: datetime) -> float:
    try:
        updated = datetime.strptime(updated_at or "", TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    days = max(0.0, (now - updated).total_seconds() / 86400.0)
    return max(0.0, 1.0 - _DECAY_PER_DAY * days)


def _with_living_states(
    result: Dict[str, Any], scored: List[Scored], provisional_rows: List[Dict[str, Any]]
) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    for state, score in scored:
        entry = {**state, "provisional": False, "authoritative": True}
        if score is not None:
            entry["score"] = round(score, 3)
        entries.append(entry)
    for row in provisional_rows:
        entries.append({
            "entity_id": f"scratchpad:{row['id']}",
            "display_name": "未固化记录",
            "current_decision": row["text"],
            "metadata": {},
            "provisional": True,
            "authoritative": False,
            "source_log_id": row["id"],
            "category": row.get("category"),
            "created_at": row.get("created_at"),
        })
    result["living_states"] = entries
    return result


class CognitiveRecallRouter:
    def __init__(self, conn: sqlite3.Connection, ppr_engine: Optional[PPREngine] = None):
        self.conn = conn
        self.living = LivingStateManager(conn)
        self.scratchpad = ScratchpadManager(conn)
        self.graph = BiTemporalGraphManager(conn)
        self.ppr = ppr_engine or PPREngine()

    def recall(
        self, query: str, top_k: int = 5, agent_id: Optional[str] = None, scope: Optional[str] = None
    ) -> Dict[str, Any]:
        """scope=None 为旧客户端(行为不变、不回显);dm 走全量并回显;其余一律走群聊视图并回显 group。"""
        scope = normalize_scope(scope)
        if scope == SCOPE_GROUP:
            return self._recall_group(query, top_k)
        result = self._recall_all(query, top_k, agent_id)
        if scope == SCOPE_DM:
            result["scope"] = SCOPE_DM
        return result

    def _recall_group(self, query: str, top_k: int) -> Dict[str, Any]:
        """群聊视图:只在 visibility=group_ok 的卡上匹配,且只返回白名单字段。

        草稿流(provisional/candidate/explicit)、最近对话层、PPR 排名与种子节点都可能带私聊内容,
        这里整层不走;卡片 facts 由夜间整理从任意会话折叠而来,同样不出。
        """
        top_k = max(1, int(top_k))
        now = datetime.now(timezone.utc)
        tokens = _scoring_tokens(query)
        words = set(_tokens(_strip_particles(query)))
        active = self.living.list_active_states()
        visible = [group_view(s) for s in active if is_group_visible(s)]
        private = [s for s in active if not is_group_visible(s)]

        by_id = {s["entity_id"]: s for s in visible}
        traced_private: List[str] = []
        ranked: List[Scored] = []
        direct = self._match_living_states(query, visible)
        if direct:
            ranked = self._rank(direct, tokens, words, now)[:top_k]
            result = {"strategy": "LIVING_STATE_DIRECT", "confidence": 1.0, "state": ranked[0][0], "nodes": []}
        else:
            seeds = self._match_seed_nodes(query)
            if seeds:
                energy = self.ppr.run_ppr(self.graph.get_active_subgraph(), seeds)
                order = [node for node, score in energy.items() if score > 0][:top_k]
                ranked = [(by_id[node], None) for node in order if node in by_id][:1]
            if ranked:
                result = {"strategy": "PPR_SPREADING", "resolved_state": ranked[0][0]}
            else:
                traced = self._plan_trace(seeds) if seeds else []
                # Issue #84:方案节点追溯到的卡同样只放行 group_ok;私人卡只记 id,不返回任何内容
                traced_private = [state["entity_id"] for state, _ in traced if state["entity_id"] not in by_id]
                shown = [(by_id[state["entity_id"]], only_old) for state, only_old in traced if state["entity_id"] in by_id]
                if shown:
                    ranked = [(card, None) for card, _ in shown][:top_k]
                    result = {
                        "strategy": "PLAN_NODE_TRACE", "confidence": 0.8, "state": ranked[0][0],
                        "superseded_plan_matched": any(only_old for _, only_old in shown), "nodes": [],
                    }
                else:
                    ranked = self._rank(visible, tokens, words, now, require_match=True)[:top_k]
                    if ranked:
                        result = {"strategy": "DECISION_TEXT_MATCH", "confidence": 0.6, "state": ranked[0][0], "nodes": []}
                    else:
                        result = {"strategy": "NO_MATCH", "confidence": 0.0, "state": None, "nodes": []}

        withheld = sorted(set(self._withheld_ids(query, private, tokens, words, now)) | set(traced_private))
        if withheld:
            log.warning("recall scope=group: 私人卡命中但未注入 count=%d entity_ids=%s", len(withheld), withheld)
        result["scope"] = SCOPE_GROUP
        result["withheld_entity_ids"] = withheld
        return self._finish(result, ranked, [])

    def _withheld_ids(
        self, query: str, private: List[Dict[str, Any]], tokens: List[str], words: set, now: datetime
    ) -> List[str]:
        hit = self._match_living_states(query, private)
        if not hit:
            hit = [s for s, _ in self._rank(private, tokens, words, now, require_match=True)]
        return sorted(s["entity_id"] for s in hit)

    def _recall_all(self, query: str, top_k: int, agent_id: Optional[str]) -> Dict[str, Any]:
        top_k = max(1, int(top_k))
        now = datetime.now(timezone.utc)
        tokens = _scoring_tokens(query)
        words = set(_tokens(_strip_particles(query)))

        direct = self._match_living_states(query)
        if direct:
            ranked = self._rank(direct, tokens, words, now)[:top_k]
            result = {"strategy": "LIVING_STATE_DIRECT", "confidence": 1.0, "state": ranked[0][0], "nodes": []}
            return self._finish(result, ranked, [])

        seeds = self._match_seed_nodes(query)
        if seeds:
            energy = self.ppr.run_ppr(self.graph.get_active_subgraph(), seeds)
            ranking = [{"node_id": node, "energy": score} for node, score in energy.items() if score > 0][:top_k]
            resolved = self._resolve_first_active_state(ranking)
            if resolved is not None:
                result = {
                    "strategy": "PPR_SPREADING",
                    "seed_nodes": seeds,
                    "ranking": ranking,
                    "resolved_state": resolved,
                }
                return self._finish(result, [(resolved, None)], [])
            # Issue #84:种子是方案节点(含已被取代的旧方案)时 PPR 解析不出现役卡,
            # 顺着 decided_as 边找到所属的现役卡;仍然什么都没有就继续走后面的兜底层,不能在这里返回空
            traced = self._plan_trace(seeds)
            if traced:
                return self._plan_trace_result(query, top_k, seeds, traced, tokens, words, now)
        return self._fallback_chain(query, top_k, agent_id, tokens, words, now)

    def _fallback_chain(
        self, query: str, top_k: int, agent_id: Optional[str], tokens: List[str], words: set, now: datetime
    ) -> Dict[str, Any]:
        ranked = self._rank(self.living.list_active_states(), tokens, words, now, require_match=True)[:top_k]
        if ranked:
            result = {"strategy": "DECISION_TEXT_MATCH", "confidence": 0.6, "state": ranked[0][0], "nodes": []}
            fallback = self._explicit_fallback(query, top_k)
            rows = fallback["facts"] if fallback else []
            if rows:
                result["provisional_facts"] = rows
                result["source_log_ids"] = [r["id"] for r in rows]
            return self._finish(result, ranked, rows)
        provisional = self._explicit_fallback(query, top_k)
        if provisional is not None:
            return self._finish(provisional, [], provisional["facts"])
        # Issue #44 PR4 红线1:candidate 候选降权召回(0.45),排在 explicit 之后、普通轮次之前
        candidate = self._candidate_fallback(query, top_k)
        if candidate is not None:
            return self._finish(candidate, [], candidate["facts"])
        recent = self._recent_turns_fallback(query, top_k, agent_id)
        if recent is not None:
            return self._finish(recent, [], recent["facts"])
        result = {"strategy": "NO_MATCH", "confidence": 0.0, "state": None, "nodes": []}
        return self._finish(result, [], [])

    def _plan_trace(self, seeds: Dict[str, float]) -> List[Tuple[Dict[str, Any], bool]]:
        """Issue #84:从命中的方案节点顺着 decided_as 边(含已失效的)找所属的现役卡。

        返回 [(卡片, 是否只命中了已被取代的旧方案)];只要有一条现役边指向命中的方案节点就不算旧方案。
        只认 decided_as,且只返回 ACTIVE 的卡;作废节点自己的内容永远不当现行结论返回。
        """
        placeholders = ",".join("?" * len(seeds))
        rows = self.conn.execute(
            f"SELECT src_id, invalid_at FROM graph_edges WHERE rel_type = 'decided_as' "
            f"AND dst_id IN ({placeholders}) ORDER BY edge_id",
            list(seeds),
        ).fetchall()
        superseded: Dict[str, bool] = {}
        for src_id, invalid_at in rows:
            superseded[src_id] = superseded.get(src_id, True) and invalid_at is not None
        traced: List[Tuple[Dict[str, Any], bool]] = []
        for entity_id, only_old in superseded.items():
            state = self.living.get_state(entity_id)
            if state is not None and state["status"] == "ACTIVE":
                traced.append((state, only_old))
        return traced

    def _plan_trace_result(
        self,
        query: str,
        top_k: int,
        seeds: Dict[str, float],
        traced: List[Tuple[Dict[str, Any], bool]],
        tokens: List[str],
        words: set,
        now: datetime,
    ) -> Dict[str, Any]:
        cards: List[Scored] = [(state, None) for state, _ in traced][:top_k]
        have = {state["entity_id"] for state, _ in cards}
        extra = [
            item for item in self._rank(self.living.list_active_states(), tokens, words, now, require_match=True)
            if item[0]["entity_id"] not in have
        ][: max(0, top_k - len(cards))]
        result = {
            "strategy": "PLAN_NODE_TRACE",
            "confidence": 0.8,
            "state": cards[0][0],
            "seed_nodes": seeds,
            "superseded_plan_matched": any(only_old for _, only_old in traced),
            "nodes": [],
        }
        fallback = self._explicit_fallback(query, top_k)
        rows = fallback["facts"] if fallback else []
        if rows:
            result["provisional_facts"] = rows
            result["source_log_ids"] = [r["id"] for r in rows]
        return self._finish(result, cards + extra, rows)

    def _finish(
        self, result: Dict[str, Any], scored: List[Scored], provisional_rows: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        self._record_hits([state["entity_id"] for state, _ in scored])
        return _with_living_states(result, scored, provisional_rows)

    def _record_hits(self, entity_ids: List[str]) -> None:
        if not entity_ids:
            return
        stamp = datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)
        placeholders = ",".join("?" * len(entity_ids))
        try:
            self.conn.execute(
                f"UPDATE living_states SET hit_count = COALESCE(hit_count, 0) + 1, last_recalled_at = ? "
                f"WHERE entity_id IN ({placeholders})",
                (stamp, *entity_ids),
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()

    def _match_living_states(
        self, query: str, states: Optional[List[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        stripped = _strip_particles(query)
        matched = []
        for state in (self.living.list_active_states() if states is None else states):
            names = [state["display_name"], *state["metadata"].get("aliases", [])]
            if any(name and self._name_matches(name, query, stripped) for name in names):
                matched.append(state)
        return matched

    @staticmethod
    def _name_matches(name: str, query: str, stripped: str) -> bool:
        if name in query or name in stripped:
            return True
        return len(name) >= 4 and bool(stripped) and stripped in name

    def _rank(
        self,
        states: List[Dict[str, Any]],
        tokens: List[str],
        words: set,
        now: datetime,
        require_match: bool = False,
    ) -> List[Scored]:
        scored: List[Scored] = []
        for state in states:
            score, qualifies = self._score_state(state, tokens, words, now)
            if require_match and not qualifies:
                continue
            scored.append((state, score))
        scored.sort(key=lambda item: item[0]["entity_id"])
        scored.sort(key=lambda item: item[0]["updated_at"] or "", reverse=True)
        scored.sort(key=lambda item: -item[1])
        return scored

    @staticmethod
    def _score_state(
        state: Dict[str, Any], tokens: List[str], words: set, now: datetime
    ) -> Tuple[float, bool]:
        metadata = state["metadata"]
        aliases = [a for a in metadata.get("aliases", []) if isinstance(a, str)]
        raw_facts = metadata.get("facts")
        facts = [f["text"] for f in raw_facts if isinstance(f, dict) and f.get("text")] if isinstance(raw_facts, list) else []
        display = (state["display_name"] or "").strip()
        # 黑户:展示名就是 id。它的「名字」不是语言,不能拿来当文本兜底的准入依据
        unlabeled = not display or display.lower() == str(state["entity_id"]).lower()
        name = (state["display_name"] or "").lower()
        alias_text = "\n".join(aliases).lower()
        decision_text = (state["current_decision"] or "").lower()
        haystack = "\n".join([name, alias_text, decision_text])

        hits = [t for t in tokens if t.lower() in haystack]
        score = (
            3 * any(t.lower() in name for t in tokens)
            + 2 * any(t.lower() in alias_text for t in tokens)
            + 1 * any(t.lower() in decision_text for t in tokens)
            + 2 * len(hits)
            + _decay(state["updated_at"], now)
        )
        # 准入一:名字(无名卡除外)/别名/现行决策里,有非通用的整词命中,或至少两个不同的非通用 2-gram 命中
        # Issue #44 CTO 红线:单 2-gram 命中卡名/别名严禁放行(「记忆」⊂「白泽记忆」会被闲聊捞出),全词命中走 LIVING_STATE_DIRECT
        identity = "\n".join(([] if unlabeled else [name]) + [alias_text, decision_text])
        strong = [t for t in tokens if t.lower() in identity and not _is_generic_token(t)]
        qualifies = any(t in words for t in strong) or _disjoint_grams(words, {t for t in strong if t not in words}) >= 2
        # 准入二(Issue #93):facts 只能靠单条 fact 内足够多的非通用命中放行,且只给封顶的加分,大卡不能靠体量取胜
        evidence = _fact_evidence(facts, tokens, words)
        if evidence >= (_FACT_MIN_WEIGHT_UNLABELED if unlabeled else _FACT_MIN_WEIGHT):
            qualifies = True
            score += min(evidence, _FACT_SCORE_CAP)
        return float(score), qualifies

    def _explicit_fallback(self, query: str, top_k: int) -> Optional[Dict[str, Any]]:
        stripped = _strip_particles(query)
        if len(stripped) < 2:
            return None
        rows = self.scratchpad.search_explicit(_tokens(stripped), top_k)
        if not rows:
            return None
        return {
            "strategy": "SCRATCHPAD_EXPLICIT_FALLBACK",
            "confidence": 0.5,
            "provisional": True,
            "authoritative": False,
            "source_log_ids": [r["id"] for r in rows],
            "facts": rows,
            "state": None,
            "nodes": [],
        }

    def _candidate_fallback(self, query: str, top_k: int) -> Optional[Dict[str, Any]]:
        """Issue #44 PR4:candidate 候选通道——未核实事实的降权透出,带 unverified 标记。

        高 noul 行与「疑似计划」(decision_approved 但 confidence<0.9)都落这里;
        慢波核实通过后改走活体卡,被否决的行已清退,不会再进本层。
        """
        stripped = _strip_particles(query)
        if len(stripped) < 2:
            return None
        tokens = [t for t in _scoring_tokens(query) if t not in _RECENT_TURN_STOPWORDS]
        if not any(len(t) >= _RECENT_TURN_MIN_CONTENT_LEN for t in tokens):
            return None
        rows = self.scratchpad.search_candidates(tokens, limit=min(top_k, _CANDIDATE_MAX))
        if not rows:
            return None
        return {
            "strategy": "CANDIDATE_UNVERIFIED",
            "confidence": _CANDIDATE_CONFIDENCE,
            "provisional": True,
            "authoritative": False,
            "unverified": True,
            "source_log_ids": [r["id"] for r in rows],
            "facts": rows,
            "state": None,
            "nodes": [],
        }

    def _recent_turns_fallback(
        self, query: str, top_k: int, agent_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Issue #37:末级兜底——窗口内的普通 user 轮次。

        只在活体卡、决策文本匹配、显式事实三路全部落空之后才轮到它,置信度压到 0.35 并标记
        provisional,避免闲聊抢占权威记忆。

        Issue #39:过滤掉纯语气/指示词,并要求 query 里至少有一个 >= 3 字的实义词,否则整层
        不触发——「可以」「这个」「然后」这类词不该把三条无关旧对话拉进上下文。
        """
        stripped = _strip_particles(query)
        if len(stripped) < 2:
            return None
        tokens = [t for t in _scoring_tokens(query) if t not in _RECENT_TURN_STOPWORDS]
        if not any(len(t) >= _RECENT_TURN_MIN_CONTENT_LEN for t in tokens):
            return None
        rows = self.scratchpad.search_recent_turns(
            tokens, limit=min(top_k, _RECENT_TURNS_MAX), agent_id=agent_id
        )
        if not rows:
            return None
        return {
            "strategy": "RECENT_TURNS_CONTEXT",
            "confidence": _RECENT_TURNS_CONFIDENCE,
            "provisional": True,
            "authoritative": False,
            "source_log_ids": [r["id"] for r in rows],
            "facts": rows,
            "state": None,
            "nodes": [],
        }

    def _match_seed_nodes(self, query: str) -> Dict[str, float]:
        rows = self.conn.execute("SELECT node_id, label FROM graph_nodes").fetchall()
        seeds: Dict[str, float] = {}
        for node_id, label in rows:
            if label and label in query:
                seeds[node_id] = float(query.count(label))
        return seeds

    def _resolve_first_active_state(self, ranking: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for item in ranking:
            state = self.living.get_state(item["node_id"])
            if state is not None and state["status"] == "ACTIVE":
                return state
        return None
