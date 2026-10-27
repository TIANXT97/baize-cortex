"""白泽·Cortex 统一 FastAPI 入口

启动: uvicorn baize.server.app:create_app --factory
"""
import hmac
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from baize.core.consolidator import DreamConsolidator
from baize.core.living_state import LivingStateManager
from baize.core.normalization import idempotency_key as make_idempotency_key
from baize.core.normalization import normalize_content
from baize.core.supersession import supersede
from baize.core.scratchpad import (
    CANDIDATE_DECISION, CANDIDATE_FACT, CANDIDATE_HINT, EXPLICIT_HINT, ScratchpadManager,
    day_judge_mode, screen_day_hint, validate_timestamp,
)
from baize.core.temporal_graph import BiTemporalGraphManager
from baize.core.seeds import seed_initial_assets
from baize.core.visibility import VISIBILITY_GROUP_OK, VISIBILITY_PRIVATE, visibility_of
from baize.intelligence.phase_detector import llm_client_from_env
from baize.intelligence.ppr_engine import PPREngine
from baize.intelligence.recall_router import CognitiveRecallRouter
from baize.storage.db import init_db

VERSION = "2.0.0-alpha"

CATEGORIES = {"fact", "preference", "rule", "lesson"}
CANDIDATE_CATEGORIES = {CANDIDATE_FACT, CANDIDATE_DECISION}
MAX_CONTENT = 2000
MAX_REASON = 500
DEFAULT_AGENT = "default"
MAX_EXPIRE_IDS = 200
MAX_EXPIRE_SESSIONS = 50


def _load_agent_tokens() -> Dict[str, str]:
    tokens: Dict[str, str] = {}
    for pair in os.environ.get("CORTEX_AGENT_TOKENS", "").split(","):
        token, sep, agent = pair.strip().partition(":")
        if sep and token and agent:
            tokens[token] = agent
    return tokens


def admin_seed_allowed() -> bool:
    """Issue #83:seed 会覆盖预置卡的现有内容,默认拒绝;只有显式开关为 1/true/on(忽略大小写与空白)才放行。"""
    return os.environ.get("CORTEX_ALLOW_ADMIN_SEED", "").strip().lower() in ("1", "true", "on")


def _graph_time() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class ScratchpadLogIn(BaseModel):
    session_id: str
    role: str
    raw_text: str
    phase_hint: Optional[str] = None
    noul_score: float = 0.0
    idempotency_key: Optional[str] = None
    category: Optional[str] = None


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
    scope: Optional[str] = None


class RollbackIn(BaseModel):
    entity_id: str
    to_version: Optional[int] = None
    history_id: Optional[int] = None
    dry_run: bool = True


class ExpireIn(BaseModel):
    ids: List[int] = []
    session_ids: List[str] = []
    dry_run: bool = True


class VisibilityIn(BaseModel):
    entity_id: str
    visibility: str
    dry_run: bool = True


class PurgeIn(BaseModel):
    before_id: Optional[int] = None
    role: Optional[str] = None
    before_date: Optional[str] = None
    include_pending_phase: bool = False
    dry_run: bool = True


class BackfillIn(BaseModel):
    dry_run: bool = True


class JudgeBackfillIn(BaseModel):
    after_date: Optional[str] = None
    before_date: Optional[str] = None
    limit: int = 500
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
        app.state.consolidator = DreamConsolidator(conn, archive_dir=archive_dir, llm_client=llm_client_from_env())
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
            "day_judge": day_judge_mode(),
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
        # Issue #56 P2-5:此处 category 只承载 candidate 的来源类型,与 cortex_add 的分类同列,不放开其他组合
        if body.category is not None and (
            body.phase_hint != CANDIDATE_HINT or body.category not in CANDIDATE_CATEGORIES
        ):
            raise HTTPException(
                status_code=422,
                detail=f"category 仅限 phase_hint=candidate 时取 {sorted(CANDIDATE_CATEGORIES)} 之一",
            )
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
            # Issue #75:白天判定器默认只记日志,approval/rejection 降为普通行,原判定写入 reason 列
            phase_hint, reason = screen_day_hint(body.phase_hint)
            try:
                log_id = app.state.scratchpad.append_log(
                    body.session_id, body.role, body.raw_text, phase_hint, body.noul_score,
                    idempotency_key=body.idempotency_key, agent_id=agent_id, category=body.category,
                    reason=reason,
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
    def list_living_states(request: Request):
        authenticate(request)
        with lock:
            return app.state.living.list_active_states()

    @app.get("/v1/living-state/{entity_id}")
    def get_living_state(entity_id: str, request: Request):
        authenticate(request)
        with lock:
            state = app.state.living.get_state(entity_id)
        if state is None:
            raise HTTPException(status_code=404, detail="living state not found")
        return state

    @app.post("/v1/living-state/decision")
    def post_decision(body: DecisionIn, request: Request):
        authenticate(request)
        with lock:
            return app.state.living.update_decision(
                body.entity_id,
                body.display_name,
                body.new_decision,
                body.superseded_plan,
                body.killed_reason,
                body.metadata,
                source="api:decision",
            )

    @app.get("/v1/living-state/{entity_id}/history")
    def living_state_history(entity_id: str, request: Request, limit: int = 50):
        authenticate(request)
        with lock:
            if app.state.living.get_state(entity_id) is None:
                raise HTTPException(status_code=404, detail="living state not found")
            return {"entity_id": entity_id, "history": app.state.living.history(entity_id, min(max(limit, 1), 200))}

    @app.post("/v1/graph/node")
    def graph_node(body: GraphNodeIn, request: Request):
        authenticate(request)
        with lock:
            app.state.graph.upsert_node(body.node_id, body.label, body.node_type, body.properties)
        return {"node_id": body.node_id}

    @app.post("/v1/graph/edge")
    def graph_edge(body: GraphEdgeIn, request: Request):
        authenticate(request)
        with lock:
            try:
                app.state.graph.add_edge(
                    body.src_id, body.dst_id, body.rel_type, body.valid_at, body.weight, body.invalid_at
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "created"}

    @app.post("/v1/graph/invalidate")
    def graph_invalidate(body: GraphInvalidateIn, request: Request):
        authenticate(request)
        with lock:
            app.state.graph.invalidate_edge(body.src_id, body.dst_id, body.rel_type, body.invalid_at)
        return {"status": "invalidated"}

    @app.get("/v1/graph/active-subgraph")
    def active_subgraph(request: Request):
        authenticate(request)
        with lock:
            edges = app.state.graph.get_active_subgraph()
        return {"edges": [{"src_id": s, "dst_id": d, "weight": w} for s, d, w in edges]}

    @app.post("/v1/recall/ppr")
    def recall_ppr(body: PPRIn, request: Request):
        authenticate(request)
        with lock:
            edges = app.state.graph.get_active_subgraph()
        energy = ppr.run_ppr(edges, body.seed_nodes)
        return {"ranking": [{"node_id": node, "energy": score} for node, score in energy.items()]}

    @app.post("/v1/sleep/consolidate")
    def sleep_consolidate(body: SleepIn, request: Request):
        authenticate(request)
        with lock:
            return app.state.consolidator.run_consolidation(body.target_date, dry_run=body.dry_run)

    @app.post("/v1/recall")
    def recall(body: RecallIn, request: Request):
        # Issue #39:读路径与写路径使用同一个 agent 身份,否则跨代理召回没有隔离依据。
        agent_id = authenticate(request)
        with lock:
            return app.state.router.recall(body.query, body.top_k, agent_id=agent_id, scope=body.scope)

    @app.post("/v1/admin/backfill-display-names")
    def admin_backfill(body: BackfillIn, request: Request):
        authenticate(request)
        with lock:
            return app.state.consolidator.backfill_display_names(body.dry_run)

    @app.post("/v1/admin/purge-scratchpad")
    def admin_purge(body: PurgeIn, request: Request):
        authenticate(request)
        try:
            before_date = validate_timestamp(body.before_date) if body.before_date is not None else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        with lock:
            return app.state.consolidator.purge_scratchpad(
                body.before_id, body.role, body.dry_run, before_date=before_date,
                include_pending_phase=body.include_pending_phase,
            )

    @app.post("/v1/admin/backfill-judge")
    def admin_backfill_judge(body: JudgeBackfillIn, request: Request):
        authenticate(request)
        try:
            after_date = validate_timestamp(body.after_date) if body.after_date is not None else None
            before_date = validate_timestamp(body.before_date) if body.before_date is not None else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        with lock:
            try:
                return app.state.scratchpad.backfill_judge(
                    after_date, before_date, max(1, body.limit), body.dry_run
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/v1/admin/living-state/visibility")
    def admin_set_visibility(body: VisibilityIn, request: Request):
        authenticate(request)
        if body.visibility not in (VISIBILITY_GROUP_OK, VISIBILITY_PRIVATE):
            raise HTTPException(
                status_code=422, detail=f"visibility 必须是 {VISIBILITY_GROUP_OK} 或 {VISIBILITY_PRIVATE}"
            )
        with lock:
            state = app.state.living.get_state(body.entity_id)
            if state is None:
                raise HTTPException(status_code=404, detail="living state not found")
            before = visibility_of(state)
            if not body.dry_run:
                app.state.living.set_visibility(body.entity_id, body.visibility)
        return {
            "entity_id": body.entity_id, "before": before, "after": body.visibility, "dry_run": body.dry_run,
        }

    @app.post("/v1/admin/living-state/rollback")
    def admin_rollback_living_state(body: RollbackIn, request: Request):
        authenticate(request)
        with lock:
            try:
                return app.state.living.rollback(
                    body.entity_id, body.to_version, body.history_id, body.dry_run, source="rollback"
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/v1/admin/expire-scratchpad")
    def admin_expire_scratchpad(body: ExpireIn, request: Request):
        authenticate(request)
        if not body.ids and not body.session_ids:
            raise HTTPException(status_code=422, detail="必须提供 ids 或 session_ids")
        if len(body.ids) > MAX_EXPIRE_IDS or len(body.session_ids) > MAX_EXPIRE_SESSIONS:
            raise HTTPException(
                status_code=422,
                detail=f"单次最多 {MAX_EXPIRE_IDS} 个 id、{MAX_EXPIRE_SESSIONS} 个会话",
            )
        with lock:
            return app.state.scratchpad.expire_rows(body.ids, body.session_ids, body.dry_run)

    @app.post("/v1/admin/seed")
    def admin_seed(request: Request):
        authenticate(request)
        if not admin_seed_allowed():
            raise HTTPException(
                status_code=403,
                detail="admin/seed 默认关闭:它会把预置示例卡重置为初始内容。确需调用时设置 CORTEX_ALLOW_ADMIN_SEED=1 后重启服务",
            )
        with lock:
            seed_initial_assets(app.state.conn)
        return {"status": "seeded"}

    return app
