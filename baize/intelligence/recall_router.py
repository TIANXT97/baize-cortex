"""Cognitive Recall Router: Level 1 活体卡直达 / Level 2 PPR 能量游走的统一召回入口"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import TIMESTAMP_FORMAT, ScratchpadManager
from baize.core.temporal_graph import BiTemporalGraphManager
from baize.intelligence.ppr_engine import PPREngine


_PARTICLES = ("的", "什么", "怎么", "定的", "呢", "吗", "吧", "啊")
_TOKEN_RE = re.compile(r"[一-鿿A-Za-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]{2,}")
_DECAY_PER_DAY = 0.05

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

    def recall(self, query: str, top_k: int = 5) -> Dict[str, Any]:
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

    def _match_living_states(self, query: str) -> List[Dict[str, Any]]:
        stripped = _strip_particles(query)
        matched = []
        for state in self.living.list_active_states():
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
