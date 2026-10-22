"""Phase A:cortex_add 幂等、超时降级、注入字段防护、认证"""
import os
import tempfile

from fastapi.testclient import TestClient

from baize.client.outbox import Outbox
from baize.client.plugin import QUEUED_MESSAGE, CortexClient
from baize.server.app import create_app


def _transport(tc):
    def transport(url, body, headers, timeout):
        resp = tc.post(url.split("testserver", 1)[1], json=body, headers=headers)
        return resp.status_code, resp.json(), dict(resp.headers)

    return transport


def _rows(app, where="1=1"):
    return app.state.conn.execute(f"SELECT COUNT(*) FROM scratchpad_log WHERE {where}").fetchone()[0]


def _setup(d):
    app = create_app(os.path.join(d, "c.db"), os.path.join(d, "cold"))
    ob = Outbox(os.path.join(d, "outbox.jsonl"), clock=lambda: 1000.0, rand=lambda: 1.0)
    return app, ob


def test_duplicate_calls_store_one_row():
    with tempfile.TemporaryDirectory() as d:
        app, ob = _setup(d)
        with TestClient(app) as tc:
            cc = CortexClient("http://testserver", None, "sess", ob, transport=_transport(tc))
            r1 = cc.cortex_add("记住  这条", "rule", "测试")
            r2 = cc.cortex_add("记住 这条", "rule", "测试")
            assert r1["status"] == "ok" and r1["deduplicated"] is False
            assert r2["status"] == "ok" and r2["deduplicated"] is True and r2["id"] == r1["id"]
            assert app.state.conn.execute("SELECT agent_id FROM scratchpad_log WHERE id = ?", (r1["id"],)).fetchone()[0] == "default"
            assert _rows(app, "phase_hint = 'explicit_addition'") == 1
            row = app.state.conn.execute(
                "SELECT category, reason, idempotency_key, ingested_at FROM scratchpad_log WHERE id = ?", (r1["id"],)
            ).fetchone()
            assert row[0] == "rule" and row[1] == "测试" and row[2] == r1["idempotency_key"] and len(row[3]) == 19 and "T" not in row[3]


def test_timeout_falls_back_to_outbox_and_syncs_later():
    with tempfile.TemporaryDirectory() as d:
        app, ob = _setup(d)
        with TestClient(app) as tc:
            def timing_out(url, body, headers, timeout):
                raise TimeoutError("3s")

            cc = CortexClient("http://testserver", None, "sess", ob, transport=timing_out)
            result = cc.cortex_add("断网时写入", "lesson", "r")
            assert result["status"] == "queued" and result["message"] == QUEUED_MESSAGE
            assert result["idempotency_key"] and ob.pending() == 1

            good = CortexClient("http://testserver", None, "sess", ob, transport=_transport(tc))
            assert good.cortex_add("断网时写入", "lesson", "r")["deduplicated"] is False
            ob.flush()
            assert ob.pending() == 0
            assert _rows(app, "phase_hint = 'explicit_addition'") == 1


def test_server_error_also_queues_and_bad_content_is_rejected():
    with tempfile.TemporaryDirectory() as d:
        app, ob = _setup(d)
        with TestClient(app):
            cc = CortexClient("http://testserver", None, "sess", ob, transport=lambda *a: (503, {}, {}))
            assert cc.cortex_add("x", "fact", "r")["status"] == "queued"
            assert cc.cortex_add("   ", "fact", "r")["status"] == "error"
            assert cc.cortex_add("x", "bogus", "r")["status"] == "error"
            assert cc.cortex_add("x" * 2001, "fact", "r")["status"] == "error"
            assert cc.cortex_add("x", "fact", "r" * 501)["status"] == "error"
            reject = CortexClient("http://testserver", None, "sess", ob, transport=lambda *a: (422, {"detail": "no"}, {}))
            assert reject.cortex_add("y", "fact", "r")["status"] == "rejected"


def test_model_cannot_inject_trust_fields():
    with tempfile.TemporaryDirectory() as d:
        app, ob = _setup(d)
        with TestClient(app) as tc:
            cc = CortexClient("http://testserver", None, "sess", ob, transport=_transport(tc))
            try:
                cc.cortex_add("x", "fact", "r", agent_id="evil")
                raise AssertionError("注入参数应被拒绝")
            except TypeError:
                pass


def test_server_ignores_body_trust_fields_and_requires_auth():
    with tempfile.TemporaryDirectory() as d:
        app, _ = _setup(d)
        os.environ["CORTEX_AGENT_TOKENS"] = "tokA:agentA,tokB:agentB"
        try:
            with TestClient(app) as tc:
                body = {"session_id": "s", "content": "内容", "category": "fact", "reason": "r",
                        "agent_id": "evil", "source": "admin", "trust": "high"}
                assert tc.post("/v1/cortex/add", json=body).status_code == 401
                assert tc.post("/v1/cortex/add", json=body, headers={"Authorization": "Bearer nope"}).status_code == 403
                assert tc.post("/v1/scratchpad/log", json={"session_id": "s", "role": "user", "raw_text": "x"}).status_code == 401

                ok = tc.post("/v1/cortex/add", json=body, headers={"Authorization": "Bearer tokA"})
                assert ok.status_code == 200
                assert app.state.conn.execute(
                    "SELECT agent_id FROM scratchpad_log WHERE id = ?", (ok.json()["id"],)
                ).fetchone()[0] == "agentA"
                other = tc.post("/v1/cortex/add", json=body, headers={"Authorization": "Bearer tokB"})
                assert other.status_code == 409
        finally:
            del os.environ["CORTEX_AGENT_TOKENS"]


def test_server_validation_status_codes():
    with tempfile.TemporaryDirectory() as d:
        app, _ = _setup(d)
        with TestClient(app) as tc:
            base = {"session_id": "s", "content": "内容", "category": "fact", "reason": "r"}
            assert tc.post("/v1/cortex/add", json={**base, "content": "  "}).status_code == 422
            assert tc.post("/v1/cortex/add", json={**base, "category": "x"}).status_code == 422
            assert tc.post("/v1/cortex/add", json={**base, "reason": "r" * 501}).status_code == 422
            assert tc.post("/v1/cortex/add", json={**base, "content": "x" * 2001}).status_code == 413
            assert tc.post("/v1/cortex/add", json={**base, "idempotency_key": "wrong"}).status_code == 422
            assert _rows(app) == 0


def test_supersedes_id_expires_old_record_and_adds_edge():
    with tempfile.TemporaryDirectory() as d:
        app, _ = _setup(d)
        with TestClient(app) as tc:
            base = {"session_id": "s", "category": "fact", "reason": "r"}
            old = tc.post("/v1/cortex/add", json={**base, "content": "旧版本"}).json()["id"]
            new = tc.post("/v1/cortex/add", json={**base, "content": "新版本", "supersedes_id": old}).json()["id"]
            conn = app.state.conn
            assert conn.execute("SELECT supersedes_id FROM scratchpad_log WHERE id = ?", (new,)).fetchone()[0] == old
            expired, invalid = conn.execute(
                "SELECT expired_at, invalid_at FROM scratchpad_log WHERE id = ?", (old,)).fetchone()
            assert expired and invalid
            assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE rel_type = 'superseded_by'").fetchone()[0] == 1

            before = _rows(app)
            bad = tc.post("/v1/cortex/add", json={**base, "content": "无效推翻", "supersedes_id": 9999})
            assert bad.status_code == 422 and _rows(app) == before


if __name__ == "__main__":
    test_duplicate_calls_store_one_row()
    test_timeout_falls_back_to_outbox_and_syncs_later()
    test_server_error_also_queues_and_bad_content_is_rejected()
    test_model_cannot_inject_trust_fields()
    test_server_ignores_body_trust_fields_and_requires_auth()
    test_server_validation_status_codes()
    test_supersedes_id_expires_old_record_and_adds_edge()
    print("✅ Phase A cortex_add 测试全部通过")
