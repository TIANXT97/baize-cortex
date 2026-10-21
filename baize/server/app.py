"""白泽·Cortex 统一 FastAPI 入口与 MCP 工具包装

启动: uvicorn baize.server.app:create_app --factory
"""
import threading
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import ScratchpadManager
from baize.core.temporal_graph import BiTemporalGraphManager
from baize.intelligence.ppr_engine import PPREngine
from baize.storage.db import init_db

VERSION = "2.0.0-alpha"


class ScratchpadLogIn(BaseModel):
    session_id: str
    role: str
    raw_text: str
    phase_hint: Optional[str] = None
    noul_score: float = 0.0


class DecisionIn(BaseModel):
    entity_id: str
    display_name: str
    new_decision: str
    superseded_plan: Optional[str] = None
    killed_reason: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class PPRIn(BaseModel):
    seed_nodes: Dict[str, float]


def create_app(db_path: str = "baize.db") -> FastAPI:
    conn = init_db(db_path)
    lock = threading.Lock()
    scratchpad = ScratchpadManager(conn)
    living = LivingStateManager(conn)
    graph = BiTemporalGraphManager(conn)
    ppr = PPREngine()

    app = FastAPI(title="白泽·Cortex", version=VERSION)
    app.state.conn = conn
    app.state.graph = graph

    @app.get("/health")
    def health():
        with lock:
            conn.execute("SELECT 1").fetchone()
        return {
            "status": "ok",
            "version": VERSION,
            "modules": {
                "storage": "ready",
                "scratchpad": "ready",
                "living_state": "ready",
                "graph": "ready",
                "ppr": "ready",
            },
        }

    @app.post("/v1/scratchpad/log")
    def scratchpad_log(body: ScratchpadLogIn):
        with lock:
            log_id = scratchpad.append_log(
                body.session_id, body.role, body.raw_text, body.phase_hint, body.noul_score
            )
        return {"id": log_id}

    @app.get("/v1/living-state/{entity_id}")
    def get_living_state(entity_id: str):
        with lock:
            state = living.get_state(entity_id)
        if state is None:
            raise HTTPException(status_code=404, detail="living state not found")
        return state

    @app.post("/v1/living-state/decision")
    def post_decision(body: DecisionIn):
        with lock:
            return living.update_decision(
                body.entity_id,
                body.display_name,
                body.new_decision,
                body.superseded_plan,
                body.killed_reason,
                body.metadata,
            )

    @app.post("/v1/recall/ppr")
    def recall_ppr(body: PPRIn):
        with lock:
            edges = graph.get_active_subgraph()
        energy = ppr.run_ppr(edges, body.seed_nodes)
        return {"ranking": [{"node_id": node, "energy": score} for node, score in energy.items()]}

    app.state.mcp_tools = {
        "baize_scratchpad_log": lambda args: scratchpad_log(ScratchpadLogIn(**args)),
        "baize_get_living_state": lambda args: get_living_state(args["entity_id"]),
        "baize_decision": lambda args: post_decision(DecisionIn(**args)),
        "baize_recall_ppr": lambda args: recall_ppr(PPRIn(**args)),
    }

    return app
