"""白泽·Cortex 统一 FastAPI 入口

启动: uvicorn baize.server.app:create_app --factory
"""
import sqlite3
import threading
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from baize.core.consolidator import DreamConsolidator
from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import ScratchpadManager
from baize.core.temporal_graph import BiTemporalGraphManager
from baize.core.seeds import seed_initial_assets
from baize.intelligence.phase_detector import OpenAICompatClient, PhaseDetector, llm_settings_from_env
from baize.intelligence.ppr_engine import PPREngine
from baize.intelligence.recall_router import CognitiveRecallRouter
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


class GraphNodeIn(BaseModel):
    node_id: str
    label: str
    node_type: str
    properties: Optional[Dict[str, Any]] = None


class GraphEdgeIn(BaseModel):
    src_id: str
    dst_id: str
    rel_type: str
    valid_at: str
    weight: float = 1.0
    invalid_at: Optional[str] = None


class GraphInvalidateIn(BaseModel):
    src_id: str
    dst_id: str
    rel_type: str
    invalid_at: str


class SleepIn(BaseModel):
    target_date: Optional[str] = None
    dry_run: bool = False


class RecallIn(BaseModel):
    query: str
    top_k: int = 5


def create_app(db_path: str = "baize.db", archive_dir: str = "cold_archive") -> FastAPI:
    lock = threading.Lock()
    ppr = PPREngine()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = init_db(db_path)
        app.state.conn = conn
        app.state.scratchpad = ScratchpadManager(conn)
        app.state.living = LivingStateManager(conn)
        app.state.graph = BiTemporalGraphManager(conn)
        settings = llm_settings_from_env()
        llm_client = None
        if settings is not None:
            url, key, reasoning_model = settings
            llm_client = OpenAICompatClient(url, key, reasoning_model)
        app.state.detector = PhaseDetector.from_env()
        app.state.consolidator = DreamConsolidator(conn, archive_dir=archive_dir, llm_client=llm_client)
        app.state.router = CognitiveRecallRouter(conn, ppr)
        try:
            yield
        finally:
            conn.close()

    app = FastAPI(title="白泽·Cortex", version=VERSION, lifespan=lifespan)

    @app.get("/health")
    def health():
        with lock:
            app.state.conn.execute("SELECT 1").fetchone()
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
            log_id = app.state.scratchpad.append_log(
                body.session_id, body.role, body.raw_text, body.phase_hint, body.noul_score
            )
        return {"id": log_id}

    @app.get("/v1/living-states")
    def list_living_states():
        with lock:
            return app.state.living.list_active_states()

    @app.get("/v1/living-state/{entity_id}")
    def get_living_state(entity_id: str):
        with lock:
            state = app.state.living.get_state(entity_id)
        if state is None:
            raise HTTPException(status_code=404, detail="living state not found")
        return state

    @app.post("/v1/living-state/decision")
    def post_decision(body: DecisionIn):
        with lock:
            return app.state.living.update_decision(
                body.entity_id,
                body.display_name,
                body.new_decision,
                body.superseded_plan,
                body.killed_reason,
                body.metadata,
            )

    @app.post("/v1/graph/node")
    def graph_node(body: GraphNodeIn):
        with lock:
            app.state.graph.upsert_node(body.node_id, body.label, body.node_type, body.properties)
        return {"node_id": body.node_id}

    @app.post("/v1/graph/edge")
    def graph_edge(body: GraphEdgeIn):
        with lock:
            try:
                app.state.graph.add_edge(
                    body.src_id, body.dst_id, body.rel_type, body.valid_at, body.weight, body.invalid_at
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "created"}

    @app.post("/v1/graph/invalidate")
    def graph_invalidate(body: GraphInvalidateIn):
        with lock:
            app.state.graph.invalidate_edge(body.src_id, body.dst_id, body.rel_type, body.invalid_at)
        return {"status": "invalidated"}

    @app.get("/v1/graph/active-subgraph")
    def active_subgraph():
        with lock:
            edges = app.state.graph.get_active_subgraph()
        return {"edges": [{"src_id": s, "dst_id": d, "weight": w} for s, d, w in edges]}

    @app.post("/v1/recall/ppr")
    def recall_ppr(body: PPRIn):
        with lock:
            edges = app.state.graph.get_active_subgraph()
        energy = ppr.run_ppr(edges, body.seed_nodes)
        return {"ranking": [{"node_id": node, "energy": score} for node, score in energy.items()]}

    @app.post("/v1/sleep/consolidate")
    def sleep_consolidate(body: SleepIn):
        if body.dry_run:
            return {"status": "dry_run", "archived_logs": 0, "updated_states": [], "archive_file": None}
        with lock:
            report = app.state.consolidator.run_consolidation(body.target_date)
        return report

    @app.post("/v1/recall")
    def recall(body: RecallIn):
        with lock:
            return app.state.router.recall(body.query, body.top_k)

    @app.post("/v1/admin/seed")
    def admin_seed():
        with lock:
            seed_initial_assets(app.state.conn)
        return {"status": "seeded"}

    return app
