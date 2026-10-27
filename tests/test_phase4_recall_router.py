"""Phase 4 集成测试:统一认知召回路由 (活体卡直达 / PPR 游走)、预置资产、端点与 MCP 工具"""
import io
import json
import os
import tempfile
import time

from fastapi.testclient import TestClient

from baize.core.seeds import PROJ_ID, seed_initial_assets
from baize.intelligence.recall_router import CognitiveRecallRouter
from baize.server.app import create_app
from baize.server.mcp_server import serve
from baize.storage.db import init_db

FUZZY_QUERY = "继续说说咱们那个阿里云知识库的问题吧,那天商议的方案你还记得吧"


def test_level1_living_state_direct_hit():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "r1.db"))
        try:
            seed_initial_assets(conn)
            router = CognitiveRecallRouter(conn)
            started = time.perf_counter()
            out = router.recall("帮我看看模具知识库现在定的是什么")
            elapsed_ms = (time.perf_counter() - started) * 1000

            assert out["strategy"] == "LIVING_STATE_DIRECT"
            assert out["state"]["entity_id"] == PROJ_ID
            assert "飞桨" in out["state"]["current_decision"]
            assert "人肉拆425张图做特征字典" in [t["plan"] for t in out["state"]["tombstones"]]
            assert elapsed_ms < 50
        finally:
            conn.close()


def test_level2_ppr_spreading_converges_to_active_decision():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "r2.db"))
        try:
            seed_initial_assets(conn)
            out = CognitiveRecallRouter(conn).recall(FUZZY_QUERY)

            assert out["strategy"] == "PPR_SPREADING"
            assert "topic:aliyun" in out["seed_nodes"]
            ranked = [item["node_id"] for item in out["ranking"]]
            assert PROJ_ID in ranked
            assert "plan:manual_dict" not in ranked
            assert out["resolved_state"]["entity_id"] == PROJ_ID
            assert "飞桨" in out["resolved_state"]["current_decision"]
        finally:
            conn.close()


def test_recall_no_match_returns_empty_strategy():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "r3.db"))
        try:
            seed_initial_assets(conn)
            out = CognitiveRecallRouter(conn).recall("今天天气不错")
            assert out["strategy"] == "NO_MATCH"
            assert out["state"] is None
        finally:
            conn.close()


def test_recall_endpoint_end_to_end():
    os.environ["CORTEX_ALLOW_ADMIN_SEED"] = "1"  # Issue #83:seed 默认 403
    try:
        _run_recall_endpoint_end_to_end()
    finally:
        del os.environ["CORTEX_ALLOW_ADMIN_SEED"]

def _run_recall_endpoint_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "e.db"), archive_dir=os.path.join(d, "arc"))
        with TestClient(app) as client:
            assert client.post("/v1/admin/seed").json() == {"status": "seeded"}

            direct = client.post("/v1/recall", json={"query": "模具知识库"}).json()
            assert direct["strategy"] == "LIVING_STATE_DIRECT"

            fuzzy = client.post("/v1/recall", json={"query": FUZZY_QUERY, "top_k": 3}).json()
            assert fuzzy["strategy"] == "PPR_SPREADING"
            assert fuzzy["resolved_state"]["entity_id"] == PROJ_ID
            assert len(fuzzy["ranking"]) <= 3


def test_mcp_baize_recall_round_trip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mcp.db")
        conn = init_db(path)
        try:
            seed_initial_assets(conn)
        finally:
            conn.close()

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "baize_recall", "arguments": {"query": "模具知识库"},
            }},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "baize_recall", "arguments": {"query": FUZZY_QUERY},
            }},
        ]
        stdin = io.StringIO("\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n")
        stdout = io.StringIO()
        serve(path, stdin, stdout)

        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        assert replies[0]["result"]["isError"] is False
        direct = json.loads(replies[0]["result"]["content"][0]["text"])
        assert direct["strategy"] == "LIVING_STATE_DIRECT"
        fuzzy = json.loads(replies[1]["result"]["content"][0]["text"])
        assert fuzzy["resolved_state"]["entity_id"] == PROJ_ID


if __name__ == "__main__":
    test_level1_living_state_direct_hit()
    test_level2_ppr_spreading_converges_to_active_decision()
    test_recall_no_match_returns_empty_strategy()
    test_recall_endpoint_end_to_end()
    test_mcp_baize_recall_round_trip()
    print("✅ Phase 4 集成测试全部通过")
