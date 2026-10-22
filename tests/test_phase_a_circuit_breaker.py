"""Phase A:错误签名归一化与熔断,含内存溢出端到端场景"""
import os
import tempfile

from fastapi.testclient import TestClient

from baize.client.breaker import GUIDANCE, ToolCircuitBreaker, error_signature, normalize_error
from baize.client.outbox import Outbox
from baize.client.plugin import CortexClient
from baize.server.app import create_app


def test_variants_normalize_to_same_signature():
    variants = [
        "Memory at 2426/2200 chars. Cannot write /home/u/.hermes/memories/MEMORY.md id 3f2b8c1e-aaaa-bbbb-cccc-123456789abc at 2026-10-22T06:01:00Z",
        "Memory at 2301/2200 chars. Cannot write C:\\Users\\dell\\.hermes\\memories\\MEMORY.md id 9a8b7c6d-1111-2222-3333-444455556666 at 2026-10-23 11:59:59",
        "Memory at 9999/2200 chars. Cannot write ~/other/MEMORY.md id deadbeef00ff at 1760000000",
    ]
    sigs = {error_signature("memory", "ToolError", v.replace("id deadbeef00ff", "id 3f2b8c1e-aaaa-bbbb-cccc-123456789abc")) for v in variants}
    assert len(sigs) == 1, sigs
    assert normalize_error("2426/2200") == "<N>/<N>"
    assert "<HEX>" in normalize_error("token deadbeef12345678 x")
    assert error_signature("memory", "ToolError", "a 1") != error_signature("other", "ToolError", "a 1")
    assert error_signature("memory", "ToolError", "磁盘已满") != error_signature("memory", "ToolError", "权限不足")


def test_second_identical_failure_trips_and_short_circuits():
    br = ToolCircuitBreaker()
    calls = []

    def tool(n):
        calls.append(n)
        return {"success": False, "error": f"Memory at {2400 + n}/2200"}

    assert br.call("s", "memory", tool, 1)["error"].startswith("Memory at")
    tripped = br.call("s", "memory", tool, 2)
    assert tripped["circuit_open"] and tripped["error"] == GUIDANCE
    assert br.call("s", "memory", tool, 3)["circuit_open"]
    assert calls == [1, 2]
    assert br.call("other-session", "memory", tool, 4)["error"].startswith("Memory at")


def test_different_signatures_do_not_trip():
    br = ToolCircuitBreaker()
    outcomes = iter(["磁盘已满", "权限不足", "磁盘已满", "权限不足"])
    for _ in range(4):
        result = br.call("s", "memory", lambda: {"success": False, "error": next(outcomes)})
        assert "circuit_open" not in result


def test_success_and_new_turn_reset():
    br = ToolCircuitBreaker()
    fail = lambda: {"success": False, "error": "Memory at 1/2"}
    br.call("s", "memory", fail)
    assert br.call("s", "memory", lambda: {"success": True}) == {"success": True}
    assert "circuit_open" not in br.call("s", "memory", fail)

    assert br.call("s", "memory", fail)["circuit_open"]
    br.new_turn("s")
    assert br.before_call("s", "memory") is None
    assert "circuit_open" not in br.call("s", "memory", fail)


def test_exceptions_count_and_propagate():
    br = ToolCircuitBreaker()

    def raising():
        raise OSError("disk 123 full")

    for _ in range(2):
        try:
            br.call("s", "memory", raising)
        except OSError:
            pass
    assert br.before_call("s", "memory") == GUIDANCE


def test_memory_overflow_end_to_end_switches_to_cortex_add():
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "e.db"), os.path.join(d, "cold"))
        with TestClient(app) as tc:
            def transport(url, body, headers, timeout):
                resp = tc.post(url.split("testserver", 1)[1], json=body, headers=headers)
                return resp.status_code, resp.json(), dict(resp.headers)

            cc = CortexClient("http://testserver", None, "sess", Outbox(os.path.join(d, "o.jsonl")), transport=transport)
            calls = []

            def memory_tool(content):
                calls.append(content)
                return {"success": False, "error": f"Memory at {2426 + len(calls)}/2200 chars at /home/u/.hermes/MEMORY.md"}

            br = ToolCircuitBreaker()
            result = None
            for _ in range(80):
                result = br.call("sess", "memory", memory_tool, "要记住的长内容")
                if result.get("circuit_open"):
                    break
            assert len(calls) <= 2
            assert result["error"] == GUIDANCE

            saved = cc.cortex_add("要记住的长内容", "lesson", "memory 写满")
            assert saved["status"] == "ok"
            assert app.state.conn.execute(
                "SELECT raw_text FROM scratchpad_log WHERE phase_hint = 'explicit_addition'"
            ).fetchone()[0] == "要记住的长内容"


if __name__ == "__main__":
    test_variants_normalize_to_same_signature()
    test_second_identical_failure_trips_and_short_circuits()
    test_different_signatures_do_not_trip()
    test_success_and_new_turn_reset()
    test_exceptions_count_and_propagate()
    test_memory_overflow_end_to_end_switches_to_cortex_add()
    print("✅ Phase A 熔断器测试全部通过")
