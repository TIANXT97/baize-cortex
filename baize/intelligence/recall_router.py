"""Cognitive Recall Router: Level 1 活体卡直达 / Level 2 PPR 能量游走的统一召回入口"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional

from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import ScratchpadManager
from baize.core.temporal_graph import BiTemporalGraphManager
from baize.intelligence.ppr_engine import PPREngine


_PARTICLES = ("的", "什么", "怎么", "定的", "呢", "吗", "吧", "啊")
_TOKEN_RE = re.compile(r"[一-鿿A-Za-z0-9_]{2,}")


def _strip_particles(text: str) -> str:
    for p in _PARTICLES:
        text = text.replace(p, "")
    return text.strip()


def _tokens(text: str) -> List[str]:
    return _TOKEN_RE.findall(text)


def _with_living_states(
    result: Dict[str, Any], state: Optional[Dict[str, Any]], provisional_rows: List[Dict[str, Any]]
) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    if state is not None:
        entries.append({**state, "provisional": False, "authoritative": True})
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
        state = self._match_living_state(query)
        if state is not None:
            result = {"strategy": "LIVING_STATE_DIRECT", "confidence": 1.0, "state": state, "nodes": []}
            return _with_living_states(result, state, [])

        seeds = self._match_seed_nodes(query)
        if not seeds:
            state = self._match_decision_text(query)
            if state is not None:
                result = {"strategy": "DECISION_TEXT_MATCH", "confidence": 0.6, "state": state, "nodes": []}
                fallback = self._explicit_fallback(query, top_k)
                rows = fallback["facts"] if fallback else []
                if rows:
                    result["provisional_facts"] = rows
                    result["source_log_ids"] = [r["id"] for r in rows]
                return _with_living_states(result, state, rows)
            provisional = self._explicit_fallback(query, top_k)
            if provisional is not None:
                return _with_living_states(provisional, None, provisional["facts"])
            result = {"strategy": "NO_MATCH", "confidence": 0.0, "state": None, "nodes": []}
            return _with_living_states(result, None, [])

        energy = self.ppr.run_ppr(self.graph.get_active_subgraph(), seeds)
        ranking = [{"node_id": node, "energy": score} for node, score in energy.items() if score > 0][:top_k]
        resolved = self._resolve_first_active_state(ranking)
        result = {
            "strategy": "PPR_SPREADING",
            "seed_nodes": seeds,
            "ranking": ranking,
            "resolved_state": resolved,
        }
        return _with_living_states(result, resolved, [])

    def _match_living_state(self, query: str) -> Optional[Dict[str, Any]]:
        stripped = _strip_particles(query)
        for state in self.living.list_active_states():
            names = [state["display_name"], *state["metadata"].get("aliases", [])]
            for name in names:
                if not name:
                    continue
                if name in query or name in stripped:
                    return state
                if len(name) >= 4 and stripped and stripped in name:
                    return state
        return None

    def _match_decision_text(self, query: str) -> Optional[Dict[str, Any]]:
        stripped = _strip_particles(query)
        if len(stripped) < 2:
            return None
        for state in self.living.list_active_states():
            facts = state["metadata"].get("facts", [])
            texts = [state["current_decision"] or ""]
            texts += [f.get("text", "") for f in facts if isinstance(f, dict)]
            haystack = "\n".join(texts)
            if any(len(tok) >= 2 and tok in haystack for tok in _tokens(stripped)):
                return state
        return None

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
