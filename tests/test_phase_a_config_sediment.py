"""Issue #44 PR2(E 项):配置改动事后沉淀 —— 工具动作成功后写 explicit_addition,写入前脱敏

验收标准:配置变更后无需人工 cortex_add 即可被后续召回(SCRATCHPAD_EXPLICIT_FALLBACK)。
"""
import os
import tempfile

from fastapi.testclient import TestClient

from baize.client.outbox import Outbox
from baize.client.plugin import CortexClient
from baize.server.app import create_app


def _transport(tc):
    def transport(url, body, headers, timeout):
        resp = tc.post(url.split("testserver", 1)[1], json=body, headers=headers)
        return resp.status_code, resp.json(), dict(resp.headers)

    return transport


def _client(d, transport):
    ob = Outbox(os.path.join(d, "outbox.jsonl"), clock=lambda: 1000.0, rand=lambda: 1.0)
    return CortexClient("http://testserver", None, "sess", ob, transport=transport)


def test_record_config_change_sediments_rule_row():
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "c.db"), os.path.join(d, "cold"))
        with TestClient(app) as tc:
            cc = _client(d, _transport(tc))
            r = cc.record_config_change(
                "shell", "CORTEX_REASONING_MODEL", "deepseek-a", "deepseek-b",
                stamp="2026-10-24 08:00:00",
            )
            assert r["status"] == "ok" and r["deduplicated"] is False and r["idempotency_key"]
            row = app.state.conn.execute(
                "SELECT raw_text, category, reason, phase_hint, agent_id FROM scratchpad_log WHERE id = ?",
                (r["id"],),
            ).fetchone()
            assert row[1] == "rule" and row[2] == "config_change:shell"
            assert row[3] == "explicit_addition" and row[4] == "default"
            for part in ("CORTEX_REASONING_MODEL", "deepseek-a", "deepseek-b", "2026-10-24 08:00:00"):
                assert part in row[0]


def test_secret_key_and_value_shapes_are_masked_before_write():
    captured = []

    def spy(url, body, headers, timeout):
        captured.append(body)
        return 200, {"id": len(captured), "deduplicated": False}, {}

    with tempfile.TemporaryDirectory() as d:
        cc = _client(d, spy)
        cc.record_config_change("shell", "CORTEX_SYSTEMONE_DREX_KEY", "sk-oldsecret123", "sk-newsecret456")
        content = captured[0]["content"]
        assert "sk-oldsecret123" not in content and "sk-newsecret456" not in content
        assert content.count("<REDACTED>") == 2

        hex_blob = "9f8d7c" * 7
        cc.record_config_change("shell", "mirror_payload", "model-a", hex_blob)
        content = captured[1]["content"]
        assert hex_blob not in content and "<REDACTED>" in content and "model-a" in content


def test_unset_and_removed_values_render_markers():
    captured = []

    def spy(url, body, headers, timeout):
        captured.append(body)
        return 200, {"id": len(captured), "deduplicated": False}, {}

    with tempfile.TemporaryDirectory() as d:
        cc = _client(d, spy)
        cc.record_config_change("shell", "FEATURE_FLAG", None, "on", stamp="2026-10-24 08:00:00")
        cc.record_config_change("shell", "FEATURE_FLAG", "on", None, stamp="2026-10-24 08:01:00")
        assert "(未设置)" in captured[0]["content"]
        assert "(已移除)" in captured[1]["content"]


def test_network_failure_queues_and_flushes_later():
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "c.db"), os.path.join(d, "cold"))
        with TestClient(app) as tc:
            ob = Outbox(os.path.join(d, "outbox.jsonl"), clock=lambda: 1000.0, rand=lambda: 1.0)

            def down(url, body, headers, timeout):
                raise TimeoutError("3s")

            down_client = CortexClient("http://testserver", None, "sess", ob, transport=down)
            r = down_client.record_config_change("shell", "压缩阈值", "50%", "30%")
            assert r["status"] == "queued" and ob.pending() == 1

            CortexClient("http://testserver", None, "sess", ob, transport=_transport(tc))
            ob.flush()
            assert ob.pending() == 0
            assert app.state.conn.execute(
                "SELECT COUNT(*) FROM scratchpad_log WHERE phase_hint = 'explicit_addition'"
            ).fetchone()[0] == 1


def test_sedimented_config_is_recallable_without_manual_add():
    # #44 E 项验收:沉淀后无需固化,立即可被 /v1/recall 的显式事实兜底召回
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "c.db"), os.path.join(d, "cold"))
        with TestClient(app) as tc:
            cc = _client(d, _transport(tc))
            cc.record_config_change("shell", "压缩阈值", "50%", "30%", stamp="2026-10-24 08:00:00")
            out = app.state.router.recall("压缩阈值")
            assert out["strategy"] == "SCRATCHPAD_EXPLICIT_FALLBACK", out
            assert out["provisional"] is True and out["source_log_ids"]
            assert any("30%" in f["text"] for f in out["facts"])


if __name__ == "__main__":
    test_record_config_change_sediments_rule_row()
    test_secret_key_and_value_shapes_are_masked_before_write()
    test_unset_and_removed_values_render_markers()
    test_network_failure_queues_and_flushes_later()
    test_sedimented_config_is_recallable_without_manual_add()
    print("✅ 配置改动事后沉淀测试全部通过")
