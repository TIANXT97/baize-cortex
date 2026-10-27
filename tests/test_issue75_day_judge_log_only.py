"""Issue #75:白天判定器默认只记日志(CORTEX_DAY_JUDGE),夜间整理跳过已过期行,测试数据软清理端点"""
import os
import tempfile
from contextlib import contextmanager

from fastapi.testclient import TestClient

from baize.client.outbox import Outbox
from baize.client.plugin import CortexClient
from baize.core.scratchpad import ScratchpadManager, day_judge_mode, screen_day_hint
from baize.server.app import create_app
from baize.server.mcp_server import BaizeTools
from baize.storage.db import init_db

TEXT = "拍板 采用北斗七号方案 全量上线"
QUERY = "北斗七号方案"


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


def _post(tc, text, hint=None, role="user", session="s1", **extra):
    resp = tc.post("/v1/scratchpad/log", json={
        "session_id": session, "role": role, "raw_text": text, "phase_hint": hint, **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _row(conn, row_id):
    return conn.execute(
        "SELECT phase_hint, reason, noul_score, category, role, judge_status, expired_at, invalid_at "
        "FROM scratchpad_log WHERE id = ?", (row_id,)
    ).fetchone()


def _recall(tc, query):
    resp = tc.post("/v1/recall", json={"query": query})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_mode_only_exact_apply_enables_formal_tags():
    for value, expected in (
        (None, "log_only"), ("", "log_only"), ("apply", "apply"), (" APPLY ", "apply"), ("Apply", "apply"),
        ("on", "log_only"), ("true", "log_only"), ("1", "log_only"), ("yes", "log_only"), ("off", "log_only"),
        ("applyy", "log_only"),
    ):
        with _env(CORTEX_DAY_JUDGE=value):
            assert day_judge_mode() == expected, repr(value)

    with _env(CORTEX_DAY_JUDGE=None):
        assert screen_day_hint("approval") == (None, "day_judge:approval")
        assert screen_day_hint("rejection") == (None, "day_judge:rejection")
        for untouched in ("candidate", "explicit_addition", None):
            assert screen_day_hint(untouched) == (untouched, None)
    with _env(CORTEX_DAY_JUDGE="apply"):
        assert screen_day_hint("approval") == ("approval", None)
        assert screen_day_hint("rejection") == ("rejection", None)


def test_health_reports_mode():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            with _env(CORTEX_DAY_JUDGE=None):
                assert tc.get("/health").json()["day_judge"] == "log_only"
            with _env(CORTEX_DAY_JUDGE="apply"):
                assert tc.get("/health").json()["day_judge"] == "apply"
            with _env(CORTEX_DAY_JUDGE="on"):
                assert tc.get("/health").json()["day_judge"] == "log_only"


def test_default_downgrades_approval_and_rejection_but_keeps_candidate_and_plain_rows():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc, _env(CORTEX_DAY_JUDGE=None):
            conn = tc.app.state.conn
            approval = _post(tc, TEXT, "approval", noul_score=0.9)
            rejection = _post(tc, "不对 我说的是另一台机器 北斗七号", "rejection", noul_score=0.4)
            cand = _post(tc, "好,那就这样做", "candidate", noul_score=0.2, category="decision")
            plain = _post(tc, "随便聊聊今天的天气")
            assistant = _post(tc, "收到", role="assistant")

            assert _row(conn, approval)[:3] == (None, "day_judge:approval", 0.9)
            assert _row(conn, rejection)[:3] == (None, "day_judge:rejection", 0.4)
            assert _row(conn, cand)[:4] == ("candidate", None, 0.2, "decision"), "方案 A:candidate 照常写入"
            assert _row(conn, plain)[:2] == (None, None)
            assert _row(conn, assistant)[:2] == (None, None)

            bad = tc.post("/v1/scratchpad/log", json={
                "session_id": "s", "role": "user", "raw_text": "伪造", "phase_hint": "explicit_addition"})
            assert bad.status_code == 422, "explicit_addition 仍仅限 cortex_add"


def test_apply_mode_keeps_previous_behavior():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc, _env(CORTEX_DAY_JUDGE="apply"):
            conn = tc.app.state.conn
            approval = _post(tc, TEXT, "approval")
            rejection = _post(tc, "不对 北斗七号作废", "rejection")
            assert _row(conn, approval)[:2] == ("approval", None)
            assert _row(conn, rejection)[:2] == ("rejection", None)
            assert _row(conn, approval)[5] == "done"


def test_downgraded_row_is_ordinary_talk_recallable_and_not_consolidated():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc, _env(CORTEX_DAY_JUDGE=None):
            _post(tc, TEXT, "approval")
            out = _recall(tc, QUERY)
            assert out["strategy"] == "RECENT_TURNS_CONTEXT" and TEXT in str(out), "降级行按普通话被最近对话兜底召回"

            report = tc.post("/v1/sleep/consolidate", json={}).json()
            assert report["status"] == "ok" and report["updated_states"] == [] and report["archived_logs"] == 0
            assert tc.get("/v1/living-states").json() == []

        with TestClient(_app(d)) as tc, _env(CORTEX_DAY_JUDGE="apply"):
            _post(tc, TEXT + "二", "approval")
            assert "二" not in str(_recall(tc, QUERY + "二")).replace(TEXT, ""), "对照:放行模式下打标行不走最近对话层"


def test_mcp_scratchpad_log_is_not_affected():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "m.db"))
        try:
            with _env(CORTEX_DAY_JUDGE=None):
                row_id = BaizeTools(conn).scratchpad_log(
                    {"session_id": "s", "role": "user", "raw_text": "proj:api 定案:方案X", "phase_hint": "approval"}
                )["id"]
                assert _row(conn, row_id)[0] == "approval", "MCP 由调用方 agent 显式传入,不属于白天判定器"
        finally:
            conn.close()


class _FakeDetector:
    def __init__(self, verdicts):
        self.verdicts = verdicts

    def detect_phase_rich(self, text):
        phase, noul, confidence = self.verdicts[text]
        return phase, noul, "", confidence


def test_plugin_sync_turn_end_to_end_default_mode():
    verdicts = {
        "拍板:用方案A": ("decision_approved", 0.1, 0.97),
        "不对,昨天那个结论作废": ("rejection", 0.9, 0.95),
        "好,那就这样做": ("decision_approved", 0.2, 0.85),
        "就按这个记:服务器端口是8768": ("exploration", 0.95, 0.7),
    }
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc, _env(CORTEX_DAY_JUDGE=None):
            def transport(url, body, headers, timeout):
                resp = tc.post(url.split("testserver", 1)[1], json=body, headers=headers)
                return resp.status_code, resp.json(), dict(resp.headers)

            client = CortexClient(
                "http://testserver", None, "sess", Outbox(os.path.join(d, "o.jsonl")),
                transport=transport, phase_detector=_FakeDetector(verdicts),
            )
            for text in verdicts:
                client.sync_turn(text, "", session_id="s1")
            rows = tc.app.state.conn.execute(
                "SELECT raw_text, phase_hint, reason, category FROM scratchpad_log ORDER BY id"
            ).fetchall()
            assert rows == [
                ("拍板:用方案A", None, "day_judge:approval", None),
                ("不对,昨天那个结论作废", None, "day_judge:rejection", None),
                ("好,那就这样做", "candidate", None, "decision"),
                ("就按这个记:服务器端口是8768", "candidate", None, "fact"),
            ]


def test_consolidation_skips_expired_decision_rows():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc, _env(CORTEX_DAY_JUDGE="apply"):
            conn = tc.app.state.conn
            expired = _post(tc, "proj:alpha 定案:方案A", "approval")
            live = _post(tc, "proj:beta 定案:方案B", "approval")
            conn.execute("UPDATE scratchpad_log SET expired_at = '2026-10-27 00:00:00' WHERE id = ?", (expired,))
            conn.commit()

            report = tc.post("/v1/sleep/consolidate", json={}).json()
            assert report["updated_states"] == ["proj:beta"], report
            assert report["archived_logs"] == 1
            assert _row(conn, expired)[0] == "approval", "已过期行留在热表,不被折叠也不被归档删除"
            assert conn.execute("SELECT 1 FROM scratchpad_log WHERE id = ?", (live,)).fetchone() is None


def test_expire_endpoint_dry_run_then_soft_delete_is_idempotent_and_never_physical():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc, _env(CORTEX_DAY_JUDGE="apply"):
            conn = tc.app.state.conn
            a = _post(tc, "测试会话里的定案 好,那就这样做", "approval", session="eval_x")
            b = _post(tc, "assistant test", role="assistant", session="eval_x")
            c = _post(tc, "另一条定时任务文本 北斗七号方案", "approval", session="cron_y")
            keep = _post(tc, "真实会话的一句话 不能动", session="real_z")
            total = conn.execute("SELECT COUNT(*) FROM scratchpad_log").fetchone()[0]

            dry = tc.post("/v1/admin/expire-scratchpad", json={"ids": [c, 99999], "session_ids": ["eval_x", "nope"]}).json()
            assert dry["dry_run"] is True and dry["expired"] == 0
            assert dry["ids"] == [a, b, c] and dry["to_expire"] == 3 and dry["already_expired"] == 0
            assert dry["missing_ids"] == [99999] and dry["unmatched_sessions"] == ["nope"]
            assert {s["id"] for s in dry["sample"]} == {a, b, c}
            assert all(set(s) == {"id", "session_id", "role", "phase_hint", "created_at", "summary"} for s in dry["sample"])
            assert _row(conn, a)[6] is None, "dry_run 不落库"

            done = tc.post("/v1/admin/expire-scratchpad", json={
                "ids": [c], "session_ids": ["eval_x"], "dry_run": False}).json()
            assert done["expired"] == 3 and done["dry_run"] is False
            for row_id in (a, b, c):
                row = _row(conn, row_id)
                assert row[6] is not None and row[7] is not None, "设置 expired_at 与 invalid_at"
            assert _row(conn, a)[0] == "approval", "不改 phase_hint"
            assert _row(conn, keep)[6] is None
            assert conn.execute("SELECT COUNT(*) FROM scratchpad_log").fetchone()[0] == total, "不物理删除"

            again = tc.post("/v1/admin/expire-scratchpad", json={"ids": [a, b, c], "dry_run": False}).json()
            assert again["expired"] == 0 and again["to_expire"] == 0 and again["already_expired"] == 3

            report = tc.post("/v1/sleep/consolidate", json={}).json()
            assert report["updated_states"] == [], "软删除后的定案行不会被夜间折叠"


def test_expired_rows_are_no_longer_recalled():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc, _env(CORTEX_DAY_JUDGE=None):
            row_id = _post(tc, TEXT)
            assert TEXT in str(_recall(tc, QUERY))
            tc.post("/v1/admin/expire-scratchpad", json={"ids": [row_id], "dry_run": False})
            assert TEXT not in str(_recall(tc, QUERY))


def test_expire_endpoint_validation_and_limits():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            assert tc.post("/v1/admin/expire-scratchpad", json={}).status_code == 422
            assert tc.post("/v1/admin/expire-scratchpad", json={"ids": [], "session_ids": []}).status_code == 422
            assert tc.post("/v1/admin/expire-scratchpad", json={"ids": list(range(1, 202))}).status_code == 422
            assert tc.post("/v1/admin/expire-scratchpad", json={"session_ids": [f"s{i}" for i in range(51)]}).status_code == 422
            assert tc.post("/v1/admin/expire-scratchpad", json={"ids": ["x"]}).status_code == 422
            ok = tc.post("/v1/admin/expire-scratchpad", json={"ids": list(range(1, 201))})
            assert ok.status_code == 200 and ok.json()["dry_run"] is True


def test_expire_requires_credentials_when_tokens_configured():
    with tempfile.TemporaryDirectory() as d:
        app = _app(d)
        with _env(CORTEX_AGENT_TOKENS="tokA:agentA"):
            with TestClient(app) as tc:
                body = {"ids": [1]}
                assert tc.post("/v1/admin/expire-scratchpad", json=body).status_code == 401
                assert tc.post("/v1/admin/expire-scratchpad", json=body, headers={"Authorization": "Bearer no"}).status_code == 403
                assert tc.post("/v1/admin/expire-scratchpad", json=body, headers={"Authorization": "Bearer tokA"}).status_code == 200


def test_expire_rows_leaves_manager_usable_for_scripts():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "s.db"))
        try:
            sp = ScratchpadManager(conn)
            row_id = sp.append_log("s", "user", "一条普通的话 用来测试")
            assert sp.expire_rows([], [], dry_run=False)["matched"] == 0
            assert sp.expire_rows([row_id], [])["expired"] == 0
            assert sp.expire_rows([row_id], [], dry_run=False)["expired"] == 1
        finally:
            conn.close()


if __name__ == "__main__":
    test_mode_only_exact_apply_enables_formal_tags()
    test_health_reports_mode()
    test_default_downgrades_approval_and_rejection_but_keeps_candidate_and_plain_rows()
    test_apply_mode_keeps_previous_behavior()
    test_downgraded_row_is_ordinary_talk_recallable_and_not_consolidated()
    test_mcp_scratchpad_log_is_not_affected()
    test_plugin_sync_turn_end_to_end_default_mode()
    test_consolidation_skips_expired_decision_rows()
    test_expire_endpoint_dry_run_then_soft_delete_is_idempotent_and_never_physical()
    test_expired_rows_are_no_longer_recalled()
    test_expire_endpoint_validation_and_limits()
    test_expire_requires_credentials_when_tokens_configured()
    test_expire_rows_leaves_manager_usable_for_scripts()
    print("✅ Issue #75 白天判定器只记日志与软清理测试全部通过")
