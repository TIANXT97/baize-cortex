"""白泽·Cortex 统一 FastAPI 入口

启动: uvicorn baize.server.app:create_app --factory
"""
import hmac
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from baize.core.consolidator import DreamConsolidator
from baize.core.living_state import LivingStateManager
from baize.core.normalization import idempotency_key as make_idempotency_key
from baize.core.normalization import normalize_content
from baize.core.supersession import supersede
from baize.core.scratchpad import EXPLICIT_HINT, ScratchpadManager
from baize.core.temporal_graph import BiTemporalGraphManager
from baize.core.seeds import seed_initial_assets
from baize.intelligence.phase_detector import OpenAICompatClient, PhaseDetector, llm_settings_from_env
from baize.intelligence.ppr_engine import PPREngine
from baize.intelligence.recall_router import CognitiveRecallRouter
from baize.storage.db import init_db

VERSION = "2.0.0-alpha"

CATEGORIES = {"fact", "preference", "rule", "lesson"}
MAX_CONTENT = 2000
MAX_REASON = 500
DEFAULT_AGENT = "default"


def _load_agent_tokens() -> Dict[str, str]:
    tokens: Dict[str, str] = {}
    for pair in os.environ.get("CORTEX_AGENT_TOKENS", "").split(","):
        token, sep, agent = pair.strip().partition(":")
        if sep and token and agent:
            tokens[token] = agent
    return tokens


def _graph_time() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class ScratchpadLogIn(BaseModel):
    session_id: str
    role: str
    raw_text: str
    phase_hint: Optional[str] = None
    noul_score: float = 0.0
    idempotency_key: Optional[str] = None


class CortexAddIn(BaseModel):
    session_id: str
    content: str
    category: str
    reason: str = ""
    idempotency_key: Optional[str] = None
    supersedes_id: Optional[int] = None


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


class PurgeIn(BaseModel):
    before_id: Optional[int] = None
    role: Optional[str] = None
    dry_run: bool = True


class BackfillIn(BaseModel):
    dry_run: bool = True


def create_app(db_path: str = "baize.db", archive_dir: str = "cold_archive") -> FastAPI:
    lock = threading.Lock()
    ppr = PPREngine()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = init_db(db_path)
        app.state.conn = conn
        app.state.agent_tokens = _load_agent_tokens()
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

    def authenticate(request: Request) -> Optional[str]:
        tokens = app.state.agent_tokens
        if not tokens:
            return DEFAULT_AGENT
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="缺少凭据", headers={"WWW-Authenticate": "Bearer"})
        for known, agent in tokens.items():
            if hmac.compare_digest(known.encode("utf-8"), token.encode("utf-8")):
                return agent
        raise HTTPException(status_code=403, detail="凭据无效")

    def deduplicated(existing, agent_id: Optional[str]) -> Dict[str, Any]:
        if (existing[1] or DEFAULT_AGENT) != agent_id:
            raise HTTPException(status_code=409, detail="幂等键已被其他 agent 占用")
        return {"id": existing[0], "deduplicated": True}

    @app.post("/v1/scratchpad/log")
    def scratchpad_log(body: ScratchpadLogIn, request: Request):
        agent_id = authenticate(request)
        if body.phase_hint == EXPLICIT_HINT:
            raise HTTPException(status_code=422, detail="phase_hint=explicit_addition 仅限 /v1/cortex/add 内部使用")
        if body.idempotency_key:
            if not body.raw_text.strip():
                raise HTTPException(status_code=422, detail="content 不能为空")
            if len(body.raw_text) > MAX_CONTENT:
                raise HTTPException(status_code=413, detail=f"content 超过 {MAX_CONTENT} 字符")
        with lock:
            if body.idempotency_key:
                existing = app.state.scratchpad.find_by_idempotency_key(body.idempotency_key)
                if existing:
                    return deduplicated(existing, agent_id)
            try:
                log_id = app.state.scratchpad.append_log(
                    body.session_id, body.role, body.raw_text, body.phase_hint, body.noul_score,
                    idempotency_key=body.idempotency_key, agent_id=agent_id,
                )
            except sqlite3.IntegrityError:
                app.state.conn.rollback()
                existing = app.state.scratchpad.find_by_idempotency_key(body.idempotency_key or "")
                if existing:
                    return deduplicated(existing, agent_id)
                raise
        return {"id": log_id}

    @app.post("/v1/cortex/add")
    def cortex_add(body: CortexAddIn, request: Request):
        agent_id = authenticate(request)
        if len(body.content) > MAX_CONTENT:
            raise HTTPException(status_code=413, detail=f"content 超过 {MAX_CONTENT} 字符")
        content = normalize_content(body.content)
        if not content:
            raise HTTPException(status_code=422, detail="content 不能为空")
        if body.category not in CATEGORIES:
            raise HTTPException(status_code=422, detail=f"category 必须是 {sorted(CATEGORIES)} 之一")
        if len(body.reason) > MAX_REASON:
            raise HTTPException(status_code=422, detail=f"reason 超过 {MAX_REASON} 字符")
        key = make_idempotency_key(body.session_id, content)
        if body.idempotency_key and body.idempotency_key != key:
            raise HTTPException(status_code=422, detail="idempotency_key 与规范化结果不一致")

        conn = app.state.conn
        with lock:
            existing = app.state.scratchpad.find_by_idempotency_key(key)
            if existing:
                return {**deduplicated(existing, agent_id), "idempotency_key": key}
            try:
                new_id = app.state.scratchpad.add_explicit(
                    body.session_id, content, body.category, body.reason, key, agent_id, commit=False
                )
                if body.supersedes_id is not None:
                    supersede(conn, "scratchpad_log", new_id, body.supersedes_id, commit=False)
                    old_node, new_node = f"mem:{body.supersedes_id}", f"mem:{new_id}"
                    app.state.graph.upsert_node(old_node, old_node, "concept", commit=False)
                    app.state.graph.upsert_node(new_node, new_node, "concept", commit=False)
                    app.state.graph.add_edge(
                        old_node, new_node, "superseded_by", valid_at=_graph_time(), commit=False
                    )
                conn.commit()
            except ValueError as exc:
                conn.rollback()
                raise HTTPException(status_code=422, detail=str(exc))
            except sqlite3.IntegrityError:
                conn.rollback()
                existing = app.state.scratchpad.find_by_idempotency_key(key)
                if existing:
                    return {**deduplicated(existing, agent_id), "idempotency_key": key}
                raise
            except Exception:
                conn.rollback()
                raise
        return {"id": new_id, "deduplicated": False, "idempotency_key": key}

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

    @app.post("/v1/admin/backfill-display-names")
    def admin_backfill(body: BackfillIn):
        with lock:
            return app.state.consolidator.backfill_display_names(body.dry_run)

    @app.post("/v1/admin/purge-scratchpad")
    def admin_purge(body: PurgeIn):
        with lock:
            return app.state.consolidator.purge_scratchpad(body.before_id, body.role, body.dry_run)

    @app.post("/v1/admin/seed")
    def admin_seed():
        with lock:
            seed_initial_assets(app.state.conn)
        return {"status": "seeded"}

    return app
