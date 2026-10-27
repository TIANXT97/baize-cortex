"""Phase 3 集成测试:慢波睡眠合成、冷归档无损性、图谱边闭合、端点与 MCP 工具"""
import gzip
import json
import os
import sqlite3
import tempfile

from fastapi.testclient import TestClient

from baize.core.consolidator import DreamConsolidator
from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import ScratchpadManager
from baize.core.temporal_graph import BiTemporalGraphManager
from baize.server.app import create_app
from baize.server.mcp_server import BaizeTools
from baize.storage.db import init_db


def _seed(conn):
    graph = BiTemporalGraphManager(conn)
    graph.upsert_node("proj:glass_mold", "模具知识库", "project")
    graph.upsert_node("plan:manual", "人肉拆图方案", "rule")
    graph.upsert_node("plan:paddle", "飞桨切片方案", "rule")
    graph.add_edge("proj:glass_mold", "plan:manual", "decided_as", "2026-10-18 01:00:00", weight=2.0)

    sp = ScratchpadManager(conn)
    for i in range(5):
        sp.append_log("day", "user", f"排障碎屑 {i}")
    sp.append_log("day", "user", "proj:glass_mold 旧方案 人肉拆图 推翻", phase_hint="rejection")
    sp.append_log("day", "user", "proj:glass_mold 定案:飞桨切片", phase_hint="approval")
    return sp


def _raw_rows(conn):
    return [r[0] for r in conn.execute("SELECT raw_text FROM scratchpad_log ORDER BY id")]


def test_consolidation_archives_losslessly_and_updates_state():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "c.db"))
        try:
            sp = _seed(conn)
            original_texts = [r["raw_text"] for r in sp.get_session_logs("day", limit=1000)]

            archive_dir = os.path.join(d, "cold_archive")
            report = DreamConsolidator(conn, archive_dir=archive_dir).run_consolidation()

            assert report["status"] == "ok"
            folded_texts = [t for t in original_texts if "proj:glass_mold" in t]
            assert report["archived_logs"] == len(folded_texts) == 2
            assert "proj:glass_mold" in report["updated_states"]
            remaining = _raw_rows(conn)
            assert len(remaining) == len(original_texts) - len(folded_texts)

            path = report["archive_file"]
            assert os.path.exists(path) and path.endswith(".jsonl.gz")
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                restored = [json.loads(line)["raw_text"] for line in fh if line.strip()]
            assert restored == folded_texts

            state = LivingStateManager(conn).get_state("proj:glass_mold")
            assert state["current_decision"] == "飞桨切片"
            assert any("人肉拆图" in t["plan"] for t in state["tombstones"])

            active = BiTemporalGraphManager(conn).get_active_subgraph()
            assert not any(s == "proj:glass_mold" and d == "plan:manual" for s, d, _ in active)
            assert any(s == "proj:glass_mold" and d.startswith("plan:") and d != "plan:manual" for s, d, _ in active)
        finally:
            conn.close()


def test_rule_fallback_without_llm_does_not_raise():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "f.db"))
        ScratchpadManager(conn).append_log("s", "user", "proj:x 只是讨论", phase_hint="exploration")
        report = DreamConsolidator(conn, archive_dir=os.path.join(d, "a")).run_consolidation()
        assert report["status"] == "ok"
        assert report["updated_states"] == []
        conn.close()


def test_sleep_endpoint_and_mcp_tool():
    # Issue #75:白天 approval 默认只记日志,这里显式放行以覆盖夜间折叠
    os.environ["CORTEX_DAY_JUDGE"] = "apply"
    try:
        _run_sleep_endpoint_and_mcp_tool()
    finally:
        del os.environ["CORTEX_DAY_JUDGE"]


def _run_sleep_endpoint_and_mcp_tool():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "e.db")
        archive = os.path.join(d, "arc")
        app = create_app(db, archive_dir=archive)
        with TestClient(app) as client:
            client.post("/v1/scratchpad/log", json={
                "session_id": "s", "role": "user", "raw_text": "proj:api 定案:方案X",
                "phase_hint": "approval",
            })
            dry = client.post("/v1/sleep/consolidate", json={"dry_run": True}).json()
            assert dry["status"] == "dry_run"

            r = client.post("/v1/sleep/consolidate", json={}).json()
            assert r["status"] == "ok"
            assert r["archived_logs"] == 1
            assert "proj:api" in r["updated_states"]
            assert os.path.exists(r["archive_file"])

        conn = init_db(db)
        tools = BaizeTools(conn)
        out = tools.consolidate_sleep({"archive_dir": archive})
        assert out["status"] == "ok"
        assert out["archived_logs"] == 0
        conn.close()


if __name__ == "__main__":
    test_consolidation_archives_losslessly_and_updates_state()
    test_rule_fallback_without_llm_does_not_raise()
    test_sleep_endpoint_and_mcp_tool()
    print("✅ Phase 3 集成测试全部通过")
