"""Issue #83:/v1/admin/seed 默认拒绝;HTTP、命令行、MCP 三个整理入口使用同一套 System 2 配置"""
import io
import json
import os
import tempfile
from contextlib import contextmanager, redirect_stdout
from unittest import mock

from fastapi.testclient import TestClient

from baize.core import consolidator as consolidator_module
from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import ScratchpadManager
from baize.core.seeds import PROJ_ID
from baize.intelligence.phase_detector import OpenAICompatClient, llm_client_from_env
from baize.server import mcp_server
from baize.server.app import admin_seed_allowed, create_app
from baize.server.mcp_server import BaizeTools
from baize.storage.db import init_db

FAKE_URL = "http://llm.example.invalid/v1/chat/completions"
FAKE_KEY = "fake-key-for-tests"
FAKE_MODEL = "fake/model-for-tests"
SYSTEM2_KEYS = ("CORTEX_LLM_URL", "CORTEX_LLM_KEY", "CORTEX_REASONING_MODEL")


@contextmanager
def _env(**values):
    saved = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _app(d):
    return create_app(os.path.join(d, "p.db"), os.path.join(d, "cold"))


def _describe(client):
    return None if client is None else (client.api_url, client.api_key, client.model)


def test_switch_values_are_case_and_whitespace_insensitive():
    for value, expected in (
        (None, False), ("", False), ("0", False), ("false", False), ("off", False), ("yes", False), ("11", False),
        ("1", True), ("true", True), ("TRUE", True), ("True", True), (" on ", True), ("ON", True), ("\t1\n", True),
    ):
        with _env(CORTEX_ALLOW_ADMIN_SEED=value):
            assert admin_seed_allowed() is expected, repr(value)


def test_seed_is_forbidden_by_default_and_does_not_touch_the_database():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_ALLOW_ADMIN_SEED=None, CORTEX_AGENT_TOKENS=None):
        with TestClient(_app(d)) as tc:
            conn = tc.app.state.conn
            before = conn.execute("SELECT COUNT(*) FROM living_states").fetchone()[0]
            resp = tc.post("/v1/admin/seed")
            assert resp.status_code == 403 and "CORTEX_ALLOW_ADMIN_SEED" in resp.json()["detail"]
            assert conn.execute("SELECT COUNT(*) FROM living_states").fetchone()[0] == before
            assert LivingStateManager(conn).get_state(PROJ_ID) is None, "被拒绝时不能建出种子卡"

            for bad in ("0", "off", "false", "yes"):
                with _env(CORTEX_ALLOW_ADMIN_SEED=bad):
                    assert tc.post("/v1/admin/seed").status_code == 403, bad


def test_seed_is_allowed_when_switch_is_on_and_is_checked_per_request():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_ALLOW_ADMIN_SEED=None, CORTEX_AGENT_TOKENS=None):
        with TestClient(_app(d)) as tc:
            for value in ("1", "true", "On", " TRUE "):
                with _env(CORTEX_ALLOW_ADMIN_SEED=value):
                    resp = tc.post("/v1/admin/seed")
                    assert resp.status_code == 200 and resp.json() == {"status": "seeded"}, value
            assert LivingStateManager(tc.app.state.conn).get_state(PROJ_ID) is not None


def test_seed_still_requires_credentials_when_tokens_are_configured():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_ALLOW_ADMIN_SEED="1", CORTEX_AGENT_TOKENS="tokA:agentA"):
        with TestClient(_app(d)) as tc:
            assert tc.post("/v1/admin/seed").status_code == 401
            assert tc.post("/v1/admin/seed", headers={"Authorization": "Bearer nope"}).status_code == 403
            assert tc.post("/v1/admin/seed", headers={"Authorization": "Bearer tokA"}).status_code == 200

    with tempfile.TemporaryDirectory() as d, _env(CORTEX_ALLOW_ADMIN_SEED=None, CORTEX_AGENT_TOKENS="tokA:agentA"):
        with TestClient(_app(d)) as tc:
            ok = {"Authorization": "Bearer tokA"}
            assert tc.post("/v1/admin/seed", headers=ok).status_code == 403, "带对 token 但开关关闭,仍然拒绝"
            assert tc.post("/v1/admin/seed").status_code == 401, "鉴权先于开关判断"


def test_other_admin_endpoints_are_not_affected_by_the_switch():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_ALLOW_ADMIN_SEED=None, CORTEX_AGENT_TOKENS=None):
        with TestClient(_app(d)) as tc:
            assert tc.post("/v1/admin/purge-scratchpad", json={"dry_run": True}).status_code == 200
            assert tc.post("/v1/admin/backfill-display-names", json={"dry_run": True}).status_code == 200
            assert tc.post("/v1/sleep/consolidate", json={"dry_run": True}).status_code == 200


def test_llm_client_from_env_matches_settings_and_is_none_when_unset():
    with _env(CORTEX_LLM_URL=None, CORTEX_LLM_KEY=None, CORTEX_REASONING_MODEL=None):
        assert llm_client_from_env() is None
    with _env(CORTEX_LLM_URL=FAKE_URL, CORTEX_LLM_KEY=None, CORTEX_REASONING_MODEL=None):
        assert llm_client_from_env() is None, "只配了地址、没配密钥 = 未配置"
    with _env(CORTEX_LLM_URL=None, CORTEX_LLM_KEY=FAKE_KEY, CORTEX_REASONING_MODEL=FAKE_MODEL):
        assert llm_client_from_env() is None
    with _env(CORTEX_LLM_URL=FAKE_URL, CORTEX_LLM_KEY=FAKE_KEY, CORTEX_REASONING_MODEL=FAKE_MODEL):
        client = llm_client_from_env()
        assert isinstance(client, OpenAICompatClient) and _describe(client) == (FAKE_URL, FAKE_KEY, FAKE_MODEL)
    with _env(CORTEX_LLM_URL=FAKE_URL, CORTEX_LLM_KEY=FAKE_KEY, CORTEX_REASONING_MODEL=None):
        default_model = _describe(llm_client_from_env())[2]
        assert default_model, "未设置模型名时使用代码内置默认值"


def _cli_client(db_path, d):
    """跑命令行 main(),截获它传给 DreamConsolidator 的 llm_client。"""
    captured = {}
    real = consolidator_module.DreamConsolidator

    class Spy(real):
        def __init__(self, conn, archive_dir="cold_archive", llm_client=None):
            captured["client"] = llm_client
            super().__init__(conn, archive_dir=archive_dir, llm_client=llm_client)

    argv = ["consolidator", "--db", db_path, "--archive-dir", os.path.join(d, "cold")]
    out = io.StringIO()
    with mock.patch.object(consolidator_module, "DreamConsolidator", Spy), mock.patch("sys.argv", argv):
        with redirect_stdout(out):
            consolidator_module.main()
    return captured["client"], json.loads(out.getvalue())


def _mcp_client(db_path):
    """走 MCP 的 serve() 路径:构造 BaizeTools 时取到的 llm_client。"""
    captured = {}
    real = mcp_server.BaizeTools

    class Spy(real):
        def __init__(self, conn, llm_client=None):
            captured["client"] = llm_client
            super().__init__(conn, llm_client=llm_client)

    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
    with mock.patch.object(mcp_server, "BaizeTools", Spy):
        mcp_server.serve(db_path, io.StringIO(request), io.StringIO())
    return captured["client"]


def test_three_entry_points_get_the_same_system2_configuration():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "p.db")
        with _env(CORTEX_LLM_URL=FAKE_URL, CORTEX_LLM_KEY=FAKE_KEY, CORTEX_REASONING_MODEL=FAKE_MODEL):
            with TestClient(create_app(db, os.path.join(d, "cold"))) as tc:
                http_client = tc.app.state.consolidator.llm_client
            cli_client, _ = _cli_client(db, d)
            mcp_client = _mcp_client(db)
            expected = (FAKE_URL, FAKE_KEY, FAKE_MODEL)
            assert _describe(http_client) == _describe(cli_client) == _describe(mcp_client) == expected
            assert all(isinstance(c, OpenAICompatClient) for c in (http_client, cli_client, mcp_client))

        with _env(CORTEX_LLM_URL=FAKE_URL, CORTEX_LLM_KEY=FAKE_KEY, CORTEX_REASONING_MODEL=None):
            models = set()
            with TestClient(create_app(db, os.path.join(d, "cold"))) as tc:
                models.add(_describe(tc.app.state.consolidator.llm_client)[2])
            models.add(_describe(_cli_client(db, d)[0])[2])
            models.add(_describe(_mcp_client(db))[2])
            assert len(models) == 1, "默认模型名三处一致"


def test_without_system2_all_entry_points_run_rule_folding_without_errors():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_LLM_URL=None, CORTEX_LLM_KEY=None, CORTEX_REASONING_MODEL=None, CORTEX_DAY_JUDGE="apply"):
        db = os.path.join(d, "p.db")
        conn = init_db(db)
        ScratchpadManager(conn).append_log("s", "user", "proj:alpha 定案:方案甲", phase_hint="approval")
        conn.close()

        with TestClient(create_app(db, os.path.join(d, "cold"))) as tc:
            assert tc.app.state.consolidator.llm_client is None
            http_report = tc.post("/v1/sleep/consolidate", json={"dry_run": True}).json()
            assert http_report["status"] == "dry_run" and http_report["would_update_states"] == ["proj:alpha"]

        cli_client, cli_report = _cli_client(db, d)
        assert cli_client is None and cli_report["status"] == "ok" and cli_report["updated_states"] == ["proj:alpha"]

        conn = init_db(db)
        try:
            ScratchpadManager(conn).append_log("s", "user", "proj:beta 定案:方案乙", phase_hint="approval")
        finally:
            conn.close()
        assert _mcp_client(db) is None
        conn = init_db(db)
        try:
            report = BaizeTools(conn).consolidate_sleep({"archive_dir": os.path.join(d, "cold")})
            assert report["status"] == "ok" and report["updated_states"] == ["proj:beta"]
        finally:
            conn.close()


class _RecordingLLM:
    def __init__(self):
        self.calls = 0

    def chat(self, system, user):
        self.calls += 1
        return json.dumps({"decision": "由注入的假模型给出的定案", "superseded": None, "display_name": "假名", "aliases": []})


def test_mcp_consolidate_uses_the_injected_system2_and_http_behaviour_is_unchanged():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        conn = init_db(os.path.join(d, "m.db"))
        try:
            ScratchpadManager(conn).append_log("s", "user", "proj:gamma 定案:原始措辞", phase_hint="approval")
            llm = _RecordingLLM()
            report = BaizeTools(conn, llm_client=llm).consolidate_sleep({"archive_dir": os.path.join(d, "cold")})
            assert llm.calls >= 1 and report["updated_states"] == ["proj:gamma"]
            assert LivingStateManager(conn).get_state("proj:gamma")["current_decision"] == "由注入的假模型给出的定案"
        finally:
            conn.close()

    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply", CORTEX_LLM_URL=None, CORTEX_LLM_KEY=None):
        with TestClient(_app(d)) as tc:
            tc.post("/v1/scratchpad/log", json={
                "session_id": "s", "role": "user", "raw_text": "proj:delta 定案:原始措辞", "phase_hint": "approval"})
            report = tc.post("/v1/sleep/consolidate", json={}).json()
            assert report["status"] == "ok" and report["updated_states"] == ["proj:delta"]
            assert report["batch_judge"] == {"status": "disabled"}


def test_no_seed_entry_exists_outside_the_http_endpoint():
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run(
        ["git", "-C", root, "grep", "-l", "-E", "seed_initial_assets", "--", "baize"], capture_output=True, text=True,
    ).stdout.split()
    assert sorted(out) == ["baize/core/seeds.py", "baize/server/app.py"], out


if __name__ == "__main__":
    test_switch_values_are_case_and_whitespace_insensitive()
    test_seed_is_forbidden_by_default_and_does_not_touch_the_database()
    test_seed_is_allowed_when_switch_is_on_and_is_checked_per_request()
    test_seed_still_requires_credentials_when_tokens_are_configured()
    test_other_admin_endpoints_are_not_affected_by_the_switch()
    test_llm_client_from_env_matches_settings_and_is_none_when_unset()
    test_three_entry_points_get_the_same_system2_configuration()
    test_without_system2_all_entry_points_run_rule_folding_without_errors()
    test_mcp_consolidate_uses_the_injected_system2_and_http_behaviour_is_unchanged()
    test_no_seed_entry_exists_outside_the_http_endpoint()
    print("✅ Issue #83 seed 保护与三入口 System 2 配置测试全部通过")
