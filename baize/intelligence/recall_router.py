"""Cognitive Recall Router: Level 1 活体卡直达 / Level 2 PPR 能量游走的统一召回入口"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from baize.core.living_state import LivingStateManager
from baize.core.temporal_graph import BiTemporalGraphManager
from baize.intelligence.ppr_engine import PPREngine


class CognitiveRecallRouter:
    def __init__(self, conn: sqlite3.Connection, ppr_engine: Optional[PPREngine] = None):
        self.conn = conn
        self.living = LivingStateManager(conn)
        self.graph = BiTemporalGraphManager(conn)
        self.ppr = ppr_engine or PPREngine()

    def recall(self, query: str, top_k: int = 5) -> Dict[str, Any]:
        state = self._match_living_state(query)
        if state is not None:
            return {"strategy": "LIVING_STATE_DIRECT", "confidence": 1.0, "state": state, "nodes": []}

        seeds = self._match_seed_nodes(query)
        if not seeds:
            return {"strategy": "NO_MATCH", "confidence": 0.0, "state": None, "nodes": []}

        energy = self.ppr.run_ppr(self.graph.get_active_subgraph(), seeds)
        ranking = [{"node_id": node, "energy": score} for node, score in energy.items() if score > 0][:top_k]
        return {
            "strategy": "PPR_SPREADING",
            "seed_nodes": seeds,
            "ranking": ranking,
            "resolved_state": self._resolve_first_active_state(ranking),
        }

    def _match_living_state(self, query: str) -> Optional[Dict[str, Any]]:
        for state in self.living.list_active_states():
            names = [state["display_name"], *state["metadata"].get("aliases", [])]
            if any(name and name in query for name in names):
                return state
        return None

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
