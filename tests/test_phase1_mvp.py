"""Phase 1 MVP 集成测试:存储初始化、草稿流、活体卡、FastAPI 端到端"""
import os
import sqlite3
import tempfile
import threading

from fastapi.testclient import TestClient

from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import ScratchpadManager
from baize.server.app import create_app
from baize.storage.db import get_db_connection, init_db


def test_init_db_memory_and_file():
    conn = sqlite3.connect(":memory:")
    with open("baize/storage/schema.sql", encoding="utf-8") as f:
        conn.executescript(f.read())
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"scratchpad_log", "living_states", "graph_nodes", "graph_edges"} <= tables

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        with get_db_connection(path) as c:
            mode = c.execute("PRAGMA journal_mode").fetchone()[0]
            fk = c.execute("PRAGMA foreign_keys").fetchone()[0]
            assert mode.lower() == "wal"
            assert fk == 1


def test_scratchpad_concurrent_writes_do_not_touch_main_tables():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "concurrent.db")
        init_db(path).close()

        errors = []

        def writer(i):
            try:
                conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
                sp = ScratchpadManager(conn)
                for j in range(50):
                    sp.append_log(f"s{i}", "user", f"ping {j}")
                conn.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        check = init_db(path)
        sp = ScratchpadManager(check)
        for i in range(8):
            assert len(sp.get_session_logs(f"s{i}", limit=1000)) == 50
        assert check.execute("SELECT COUNT(*) FROM scratchpad_log").fetchone()[0] == 400
        assert check.execute("SELECT COUNT(*) FROM living_states").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0] == 0
        check.close()


def test_scratchpad_phase_export():
    conn = init_db(":memory:")
    sp = ScratchpadManager(conn)
    sp.append_log("s", "user", "普通碎屑")
    sp.append_log("s", "user", "这个方案可以", phase_hint="approval")
    pending = sp.export_pending_phase_logs()
    assert len(pending) == 1
    assert pending[0]["phase_hint"] == "approval"


def test_living_state_supersede_keeps_tombstone():
    conn = init_db(":memory:")
    ls = LivingStateManager(conn)
    ls.update_decision("proj:glass_mold", "模具知识库", "方案A")
    state = ls.update_decision(
        "proj:glass_mold", "模具知识库", "方案B",
        superseded_plan="方案A", killed_reason="成本过高",
    )
    assert state["current_decision"] == "方案B"
    assert state["version"] == 2
    assert state["tombstones"][0]["plan"] == "方案A"
    assert state["tombstones"][0]["killed_reason"] == "成本过高"


def test_living_state_malformed_json_falls_back():
    conn = init_db(":memory:")
    conn.execute(
        "INSERT INTO living_states (entity_id, display_name, status, current_decision, metadata_json, tombstones_json) "
        "VALUES ('proj:x', 'x', 'ACTIVE', 'd', '{bad', 'not-json')"
    )
    conn.commit()
    ls = LivingStateManager(conn)
    state = ls.get_state("proj:x")
    assert state["metadata"] == {}
    assert state["tombstones"] == []
    assert [s["entity_id"] for s in ls.list_active_states()] == ["proj:x"]


def test_api_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "api.db"))
        with TestClient(app) as client:
            health = client.get("/health").json()
            assert health["version"] == "2.0.0-alpha"

            r = client.post("/v1/scratchpad/log", json={
                "session_id": "s1", "role": "user", "raw_text": "阿里云跳板", "phase_hint": None,
            })
            assert r.status_code == 200

            client.post("/v1/living-state/decision", json={
                "entity_id": "proj:glass_mold", "display_name": "模具知识库",
                "new_decision": "人肉拆图",
            })
            r = client.post("/v1/living-state/decision", json={
                "entity_id": "proj:glass_mold", "display_name": "模具知识库",
                "new_decision": "飞桨切片", "superseded_plan": "人肉拆图", "killed_reason": "不可扩展",
            })
            assert r.json()["current_decision"] == "飞桨切片"
            assert r.json()["tombstones"][0]["plan"] == "人肉拆图"

            got = client.get("/v1/living-state/proj:glass_mold").json()
            assert got["current_decision"] == "飞桨切片"
            assert client.get("/v1/living-state/proj:none").status_code == 404

            r = client.post("/v1/recall/ppr", json={"seed_nodes": {"topic:aliyun": 1.0}})
            assert r.status_code == 200
            assert "ranking" in r.json()


if __name__ == "__main__":
    test_init_db_memory_and_file()
    test_scratchpad_concurrent_writes_do_not_touch_main_tables()
    test_scratchpad_phase_export()
    test_living_state_supersede_keeps_tombstone()
    test_living_state_malformed_json_falls_back()
    test_api_end_to_end()
    print("✅ Phase 1 MVP 集成测试全部通过")
