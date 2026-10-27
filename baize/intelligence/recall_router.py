"""Cognitive Recall Router: Level 1 活体卡直达 / Level 2 PPR 能量游走的统一召回入口"""
from __future__ import annotations

import logging
import re
import sqlite3
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

        direct = self._match_living_states(query, visible)
        if direct:
            ranked = self._rank(direct, tokens, words, now)[:top_k]
            result = {"strategy": "LIVING_STATE_DIRECT", "confidence": 1.0, "state": ranked[0][0], "nodes": []}
        else:
            ranked = []
            seeds = self._match_seed_nodes(query)
            if seeds:
                energy = self.ppr.run_ppr(self.graph.get_active_subgraph(), seeds)
                order = [node for node, score in energy.items() if score > 0][:top_k]
                by_id = {s["entity_id"]: s for s in visible}
                ranked = [(by_id[node], None) for node in order if node in by_id][:1]
            if ranked:
                result = {"strategy": "PPR_SPREADING", "resolved_state": ranked[0][0]}
            else:
                ranked = self._rank(visible, tokens, words, now, require_match=True)[:top_k]
                if ranked:
                    result = {"strategy": "DECISION_TEXT_MATCH", "confidence": 0.6, "state": ranked[0][0], "nodes": []}
                else:
                    result = {"strategy": "NO_MATCH", "confidence": 0.0, "state": None, "nodes": []}

        withheld = self._withheld_ids(query, private, tokens, words, now)
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
        if not seeds:
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

        energy = self.ppr.run_ppr(self.graph.get_active_subgraph(), seeds)
        ranking = [{"node_id": node, "energy": score} for node, score in energy.items() if score > 0][:top_k]
        resolved = self._resolve_first_active_state(ranking)
        result = {
            "strategy": "PPR_SPREADING",
            "seed_nodes": seeds,
            "ranking": ranking,
            "resolved_state": resolved,
        }
        return self._finish(result, [(resolved, None)] if resolved else [], [])

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
        facts = [f.get("text", "") for f in metadata.get("facts", []) if isinstance(f, dict)]
        name = (state["display_name"] or "").lower()
        alias_text = "\n".join(aliases).lower()
        body = "\n".join([state["current_decision"] or "", *facts]).lower()
        haystack = "\n".join([name, alias_text, body])

        hits = [t for t in tokens if t.lower() in haystack]
        score = (
            3 * any(t.lower() in name for t in tokens)
            + 2 * any(t.lower() in alias_text for t in tokens)
            + 1 * any(t.lower() in body for t in tokens)
            + 2 * len(hits)
            + _decay(state["updated_at"], now)
        )
        gram_hits = [t for t in hits if t not in words]
        # Issue #44 CTO 红线:单 2-gram 命中卡名/别名严禁放行(「记忆」⊂「白泽记忆」会被闲聊捞出),全词命中走 LIVING_STATE_DIRECT
        qualifies = any(t in words for t in hits) or len(gram_hits) >= 2
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
