"""Phase 2 集成测试:图谱 CRUD 与失效过滤、PhaseDetector 降级、图谱端点、MCP 工具与 stdio 协议"""
import io
import json
import os
import sqlite3
import tempfile

from fastapi.testclient import TestClient

from baize.core.temporal_graph import BiTemporalGraphManager
from baize.intelligence.phase_detector import PhaseDetector
from baize.server.app import create_app
from baize.server.mcp_server import BaizeTools, handle_message, serve
from baize.storage.db import init_db


def _schema_conn():
    return init_db(":memory:")


def test_graph_invalidated_edge_excluded_from_view():
    conn = _schema_conn()
    g = BiTemporalGraphManager(conn)
    g.upsert_node("a", "A", "concept")
    g.upsert_node("b", "B", "rule")
    g.add_edge("a", "b", "decided_as", "2026-10-18 00:00:00")
    assert len(g.get_active_subgraph()) == 1
    g.invalidate_edge("a", "b", "decided_as", "2026-10-19 00:00:00")
    assert g.get_active_subgraph() == []
    view_rows = conn.execute("SELECT COUNT(*) FROM v_active_graph_edges").fetchone()[0]
    assert view_rows == 0
    row = conn.execute("SELECT invalid_at FROM graph_edges").fetchone()
    assert row[0] == "2026-10-19 00:00:00"


def test_phase_detector_local_fallback():
    det = PhaseDetector()
    assert det.detect_phase("好的,就按这个做,明天开搞")[0] == "decision_approved"
    assert det.detect_phase("不对,我记错了")[0] == "rejection"
    assert det.detect_phase("这个能不能用 PPR 做?")[0] == "exploration"
    assert det.detect_phase("哈哈今天天气不错")[0] == "chitchat"


def test_phase_detector_falls_back_when_remote_fails():
    det = PhaseDetector(api_url="http://127.0.0.1:9/none", api_key="k", timeout=0.5)
    phase, conf, category = det.detect_phase("就这么定")
    assert phase == "decision_approved"
    assert category == "approval"


def test_graph_and_list_endpoints():
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "g.db"))
        with TestClient(app) as client:
            assert client.post("/v1/graph/node", json={
                "node_id": "proj:x", "label": "X", "node_type": "project",
            }).status_code == 200
            assert client.post("/v1/graph/node", json={
                "node_id": "plan:a", "label": "A", "node_type": "rule",
            }).status_code == 200
            assert client.post("/v1/graph/edge", json={
                "src_id": "proj:x", "dst_id": "plan:a", "rel_type": "decided_as",
                "valid_at": "2026-10-18 00:00:00",
            }).status_code == 200

            sub = client.get("/v1/graph/active-subgraph").json()["edges"]
            assert len(sub) == 1

            client.post("/v1/graph/invalidate", json={
                "src_id": "proj:x", "dst_id": "plan:a", "rel_type": "decided_as",
                "invalid_at": "2026-10-19 00:00:00",
            })
            assert client.get("/v1/graph/active-subgraph").json()["edges"] == []

            client.post("/v1/living-state/decision", json={
                "entity_id": "proj:x", "display_name": "X", "new_decision": "方案A",
            })
            states = client.get("/v1/living-states").json()
            assert [s["entity_id"] for s in states] == ["proj:x"]


def test_mcp_tools_round_trip():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "m.db"))
        try:
            tools = BaizeTools(conn)

            out = tools.update_decision({"entity_id": "proj:m", "display_name": "M", "new_decision": "A"})
            assert out["current_decision"] == "A"
            out = tools.update_decision({
                "entity_id": "proj:m", "display_name": "M", "new_decision": "B",
                "superseded_plan": "A", "killed_reason": "慢",
            })
            assert out["tombstones"][0]["plan"] == "A"
            assert tools.get_living_state({"entity_id": "proj:m"})["current_decision"] == "B"
            assert [s["entity_id"] for s in tools.list_active_states({})] == ["proj:m"]
            assert tools.scratchpad_log({"session_id": "s", "role": "user", "raw_text": "hi"})["id"] > 0
            ranking = tools.recall_ppr({"seed_nodes": {"x": 1.0}})
            assert ranking == [{"node_id": "x", "energy": 1.0}]
        finally:
            conn.close()


def test_stdio_jsonrpc_protocol():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "baize_update_decision",
                "arguments": {"entity_id": "proj:s", "display_name": "S", "new_decision": "方案"},
            }},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "baize_get_living_state", "arguments": {"entity_id": "proj:s"},
            }},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}},
            {"jsonrpc": "2.0", "id": 6, "method": "unknown/method"},
        ]
        stdin = io.StringIO("\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n")
        stdout = io.StringIO()
        serve(path, stdin, stdout)

        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        assert len(replies) == 6
        assert replies[0]["result"]["serverInfo"]["name"] == "baize-cortex"
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        assert names == {
            "baize_get_living_state", "baize_list_active_states", "baize_update_decision",
            "baize_scratchpad_log", "baize_recall_ppr",
        }
        call = json.loads(replies[2]["result"]["content"][0]["text"])
        assert call["current_decision"] == "方案"
        assert replies[3]["result"]["isError"] is False
        assert replies[4]["error"]["code"] == -32602
        assert replies[5]["error"]["code"] == -32601


def test_mcp_invalid_request_returns_error():
    conn = _schema_conn()
    reply = handle_message({"jsonrpc": "2.0", "id": 9, "method": 123}, BaizeTools(conn))
    assert reply["error"]["code"] == -32600
    conn.close()


if __name__ == "__main__":
    test_graph_invalidated_edge_excluded_from_view()
    test_phase_detector_local_fallback()
    test_phase_detector_falls_back_when_remote_fails()
    test_graph_and_list_endpoints()
    test_mcp_tools_round_trip()
    test_stdio_jsonrpc_protocol()
    test_mcp_invalid_request_returns_error()
    print("✅ Phase 2 集成测试全部通过")
