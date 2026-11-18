"""Issue #105 PR1:常驻层——校验、增查改删与历史、预算裁剪、群聊只给 group_ok、插件取块"""
import logging
import os
import sqlite3
import tempfile

from fastapi.testclient import TestClient

from baize.client.outbox import Outbox
from baize.client.plugin import CortexClient
from baize.cortex_resident_import import load_manifest, run as run_import
from baize.core.resident import (
    MAX_TEXT_CHARS, ResidentManager, ResidentError, TITLE, find_group_unsafe, normalize_text, select_for_budget,
    validate_rule,
)
from baize.server.app import create_app
from baize.storage.db import init_db

# 假数据在运行时拼出,仓库里不留形似真实 IP / 凭据的字面量
IP4 = ".".join(["192", "0", "2", "10"])
IP6 = ":".join(["2001", "db8"]) + "::1"
IP6_FULL = ":".join(["2001", "0db8"] + ["0000"] * 5 + ["0001"])
KEY_SK = "s" + "k-" + "abcdefgh12345678"
KEY_GH = "gh" + "p_" + "abcdefgh12345678"
KEY_GHO = "gh" + "o_" + "abcdefgh12345678"
KEY_OU = "o" + "u_" + "0a1b2c3d4e5f6a7b"
ASSIGN = "tok" + "en=" + "abc"
LONG_HEX = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
VERSION4 = ".".join(["1", "2", "3", "4"])

RESIDENT_ENV = ("CORTEX_RESIDENT_DM_BUDGET", "CORTEX_RESIDENT_GROUP_BUDGET", "CORTEX_AGENT_TOKENS")


def _clean_env():
    for name in RESIDENT_ENV:
        os.environ.pop(name, None)


# ---------------------------------------------------------------- 校验(纯函数)
def test_unsafe_detects_ip_path_host_url_credential():
    unsafe = {
        "ip": [f"服务在 {IP4} 上", f"地址 {IP6} 不要说", f"full {IP6_FULL}"],
        "path": ["看 /opt/app/config", "放在 ~/notes", "盘符 C:\\Users\\x", "相对 ./run.sh", "改 docs/guide/x.md",
                 "见 /etc", "参考data/x.db"],
        "host_port": ["连 example.internal:8080", "连 localhost:8768", "node-a.lan:22"],
        "url": ["打开 https://example.com/a", "www.example.org/x"],
        "credential": [f"key 是 {KEY_SK}", KEY_GH, KEY_GHO, KEY_OU, ASSIGN, "the password is x", "密码是 hunter",
                       "Bearer abcdefghijklmn", LONG_HEX],
        "account": ["联系 someone@example.com"],
    }
    for code, texts in unsafe.items():
        for text in texts:
            assert code in find_group_unsafe(text), (code, text, find_group_unsafe(text))


def test_unsafe_no_false_positive_on_ordinary_chinese():
    safe = [
        "私聊/群聊 的规则要分开", "回复先给结论,再给依据", "(/群聊/私聊) 两种", "A/B 两种写法", "dm/group_ok 二选一",
        "时间 12:30:45 之前", "比例 3:2", "10:30 开会", "不要在群里说密码", "不要提 token 的事", f"版本 v{VERSION4}",
        "MEMORY.md 只留底线", "std::vector 与 Foo::bar", "喜欢简洁,不要客套", "先查再答,查不到就说查不到。",
        "运行 python3 脚本前先确认", "job-management 工具", "https 与 http 都不要明文传",
    ]
    for text in safe:
        assert find_group_unsafe(text) == [], (text, find_group_unsafe(text))


def test_validate_rule_group_ok_and_limits():
    assert validate_rule("普通规则", "group_ok", "normal") == []
    assert validate_rule(f"含 {IP4} 的规则", "dm", "normal") == [], "dm 不查内容"
    problems = validate_rule(f"含 {IP4} 的规则", "group_ok", "normal")
    assert problems and problems[0][0] == 422 and "ip" in problems[0][2]
    user_md = validate_rule("普通规则", "group_ok", "normal", moved_from="USER.md")
    assert user_md and "moved_from_user_md" in user_md[0][2]
    assert validate_rule("普通规则", "group_ok", "normal", moved_from="MEMORY.md") == []
    assert validate_rule("普通规则", "dm", "normal", moved_from="USER.md") == []
    assert validate_rule("字" * MAX_TEXT_CHARS, "dm", "normal") == []
    too_long = validate_rule("字" * (MAX_TEXT_CHARS + 1), "dm", "normal")
    assert too_long[0][0] == 413 and too_long[0][2] == ["too_long"]
    assert validate_rule("x", "everyone", "normal")[0][0] == 422
    assert validate_rule("x", "dm", "urgent")[0][0] == 422
    assert validate_rule("", "dm", "normal")[0][0] == 422
    assert normalize_text("  第一行\r\n  第二行\n\n第三行 ") == "第一行 第二行 第三行"


# ---------------------------------------------------------------- 预算(纯函数)
def _rule(i, text, scope="dm", priority="normal", updated="2026-11-18 00:00:00"):
    return {"id": i, "text": text, "scope": scope, "priority": priority, "updated_at": updated}


def test_select_for_budget_orders_by_priority_then_recency():
    rules = [
        _rule(1, "老的普通", updated="2026-01-01 00:00:00"),
        _rule(2, "新的普通", updated="2026-06-01 00:00:00"),
        _rule(3, "低优先级", priority="low", updated="2026-12-01 00:00:00"),
        _rule(4, "硬规则", priority="hard", updated="2026-01-01 00:00:00"),
    ]
    injected, omitted = select_for_budget(rules, 10_000)
    assert [r["id"] for r in injected] == [4, 2, 1, 3] and omitted == []
    cost = len("- 硬规则") + 1 + len("- 新的普通")
    injected, omitted = select_for_budget(rules, cost)
    assert [r["id"] for r in injected] == [4, 2]
    assert [(r["id"], r["reason"]) for r in omitted] == [(1, "budget"), (3, "budget")], "放不下就停,低优先级不插队"
    assert len(render_len(injected)) <= cost


def render_len(rules):
    return "\n".join(f"- {r['text']}" for r in rules)


def test_select_for_budget_group_cap_does_not_block_dm_rules():
    rules = [
        _rule(1, "群A" * 10, scope="group_ok", priority="hard"),
        _rule(2, "群B" * 10, scope="group_ok", priority="hard", updated="2026-11-17 00:00:00"),
        _rule(3, "私聊规则", scope="dm"),
    ]
    one = len("- " + "群A" * 10)
    injected, omitted = select_for_budget(rules, 10_000, group_cap=one)
    assert [r["id"] for r in injected] == [1, 3]
    assert [(r["id"], r["reason"]) for r in omitted] == [(2, "group_cap")]


# ---------------------------------------------------------------- 存储与迁移
def test_old_db_gets_tables_without_touching_existing_rows():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "old.db")
        conn = init_db(path)
        try:
            conn.execute("INSERT INTO scratchpad_log (session_id, role, raw_text) VALUES ('s','user','旧数据')")
            conn.execute("DROP TABLE resident_rules")
            conn.execute("DROP TABLE resident_rule_history")
            conn.commit()
        finally:
            conn.close()
        conn = init_db(path)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"resident_rules", "resident_rule_history"} <= names
            assert conn.execute("SELECT COUNT(*), MIN(raw_text) FROM scratchpad_log").fetchone() == (1, "旧数据")
            assert conn.execute("SELECT COUNT(*) FROM resident_rules").fetchone()[0] == 0
            cols = [r[1] for r in conn.execute("PRAGMA table_info(resident_rules)")]
            assert cols == ["id", "text", "scope", "priority", "source", "moved_from", "batch_id",
                            "created_at", "updated_at", "status"]
        finally:
            conn.close()


# ---------------------------------------------------------------- 管理器:增查改删与历史
def test_manager_crud_history_and_dry_run():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "c.db"))
        try:
            mgr = ResidentManager(conn)
            dry = mgr.create("先查再答", dry_run=True)
            assert dry["dry_run"] and mgr.list(include_retired=True) == []
            created = mgr.create("先查再答", source="t", batch_id="b1")["rule"]
            assert created["scope"] == "dm" and created["priority"] == "normal" and created["status"] == "active"
            assert created["created_at"] == created["updated_at"] and "T" not in created["created_at"]
            rid = created["id"]
            try:
                mgr.create("先查再答")
                raise AssertionError("重复文本应 409")
            except ResidentError as exc:
                assert exc.status == 409

            preview = mgr.update(rid, dry_run=True, text="改过的", priority="hard")
            assert preview["rule"]["text"] == "改过的" and mgr.get(rid)["text"] == "先查再答"
            updated = mgr.update(rid, text="改过的", priority="hard", scope="group_ok")["rule"]
            assert (updated["text"], updated["priority"], updated["scope"]) == ("改过的", "hard", "group_ok")
            try:
                mgr.update(rid, text="见 /opt/app/x")
                raise AssertionError("修改也要拦")
            except ResidentError as exc:
                assert exc.status == 422 and "path" in exc.reasons
            try:
                mgr.update(rid, moved_from="USER.md")
                raise AssertionError("改成 USER.md 来源时 group_ok 要拦")
            except ResidentError as exc:
                assert exc.status == 422 and "moved_from_user_md" in exc.reasons
            assert mgr.get(rid)["text"] == "改过的" and mgr.get(rid)["moved_from"] is None

            assert mgr.retire(rid, dry_run=True)["rule"]["status"] == "retired" and mgr.get(rid)["status"] == "active"
            retired = mgr.retire(rid)["rule"]
            assert retired["status"] == "retired"
            assert mgr.list() == [] and [r["id"] for r in mgr.list(include_retired=True)] == [rid]
            assert conn.execute("SELECT COUNT(*) FROM resident_rules").fetchone()[0] == 1, "软删除不物理删"
            for call in (lambda: mgr.retire(rid), lambda: mgr.update(rid, text="x")):
                try:
                    call()
                    raise AssertionError("已停用应 409")
                except ResidentError as exc:
                    assert exc.status == 409
            try:
                mgr.get(999) is None or (_ for _ in ()).throw(AssertionError)
                mgr.retire(999)
            except ResidentError as exc:
                assert exc.status == 404

            history = mgr.history(rid)
            assert [h["action"] for h in history] == ["create", "update", "retire"]
            assert history[0]["text"] is None
            assert history[1]["text"] == "先查再答" and history[1]["scope"] == "dm" and history[1]["priority"] == "normal"
            assert history[2]["text"] == "改过的" and history[2]["scope"] == "group_ok" and history[2]["status"] == "active"
        finally:
            conn.close()


# ---------------------------------------------------------------- 预算与 report(管理器)
def test_render_budget_report_and_scope_rules():
    _clean_env()
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "c.db"))
        try:
            mgr = ResidentManager(conn)
            g1 = mgr.create("群规则一", scope="group_ok", priority="hard")["rule"]["id"]
            p1 = mgr.create("私聊规则一")["rule"]["id"]
            dm = mgr.render("dm")
            assert dm["scope"] == "dm" and dm["title"] == TITLE
            assert [r["id"] for r in dm["rules"]] == [g1, p1]
            assert dm["text"] == "- 群规则一\n- 私聊规则一"
            assert dm["report"]["total_chars"] == len(TITLE) + 1 + len(dm["text"])
            assert dm["report"]["budget"] == 1500 and dm["report"]["omitted_ids"] == []
            for scope in ("group", None, "weird", "", "DM "):
                got = mgr.render(scope)
                if scope == "DM ":
                    assert got["scope"] == "dm"
                    continue
                assert got["scope"] == "group" and [r["id"] for r in got["rules"]] == [g1], scope
                assert "私聊规则一" not in got["text"] and got["report"]["budget"] == 600
            assert mgr.render("group")["rules"][0].keys() == {"id", "text", "scope", "priority", "updated_at"}
            assert mgr.render("dm")["report"]["total_chars"] <= 1500
            empty = ResidentManager(init_db(os.path.join(d, "e.db"))).render("group")
            assert empty["text"] == "" and empty["rules"] == [] and empty["report"]["total_chars"] == 0
        finally:
            conn.close()


def test_render_over_budget_trims_and_warns():
    _clean_env()
    os.environ["CORTEX_RESIDENT_DM_BUDGET"] = "120"
    os.environ["CORTEX_RESIDENT_GROUP_BUDGET"] = "60"
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _H()
    log = logging.getLogger("baize.resident")
    log.addHandler(handler)
    try:
        with tempfile.TemporaryDirectory() as d:
            conn = init_db(os.path.join(d, "c.db"))
            try:
                mgr = ResidentManager(conn)
                ids = {}
                ids["hard"] = mgr.create("硬" * 40, priority="hard")["rule"]["id"]
                ids["g_new"] = mgr.create("群" * 30, scope="group_ok")["rule"]["id"]
                ids["g_old"] = mgr.create("旧" * 30, scope="group_ok")["rule"]["id"]
                ids["low"] = mgr.create("低" * 20, priority="low")["rule"]["id"]
                conn.execute("UPDATE resident_rules SET updated_at='2026-01-01 00:00:00' WHERE id=?", (ids["g_old"],))
                conn.commit()
                dm = mgr.render("dm")
                assert dm["report"]["total_chars"] <= 120, dm["report"]
                assert [r["id"] for r in dm["rules"]] == [ids["hard"], ids["g_new"], ids["low"]]
                assert dm["report"]["omitted"] == [{"id": ids["g_old"], "reason": "group_cap"}]
                assert dm["report"]["eligible"] == 4
                assert any(r.levelno == logging.WARNING for r in records), "超预算要打 warning"
                records.clear()
                os.environ["CORTEX_RESIDENT_DM_BUDGET"] = "60"
                tight = mgr.render("dm")
                assert tight["report"]["total_chars"] <= 60 and [r["id"] for r in tight["rules"]] == [ids["hard"]]
                assert [o["reason"] for o in tight["report"]["omitted"]] == ["budget"] * 3
                os.environ["CORTEX_RESIDENT_DM_BUDGET"] = "120"
                records.clear()
                group = mgr.render("group")
                assert group["report"]["total_chars"] <= 60
                assert [r["id"] for r in group["rules"]] == [ids["g_new"]]
                assert group["report"]["omitted_ids"] == [ids["g_old"]]
                assert any(r.levelno == logging.WARNING for r in records)
            finally:
                conn.close()
    finally:
        log.removeHandler(handler)
        _clean_env()


def test_group_cap_inside_dm_budget():
    _clean_env()
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "c.db"))
        try:
            mgr = ResidentManager(conn)
            for i in range(3):
                mgr.create(f"{i}" + "群" * 280, scope="group_ok", priority="hard")
            mgr.create("私聊规则")
            dm = mgr.render("dm")
            group_used = [r for r in dm["rules"] if r["scope"] == "group_ok"]
            assert len(group_used) == 2 and any(r["text"] == "私聊规则" for r in dm["rules"])
            assert sum(len(r["text"]) + 3 for r in group_used) <= 600 + 2
            assert [o["reason"] for o in dm["report"]["omitted"]] == ["group_cap"]
        finally:
            conn.close()


# ---------------------------------------------------------------- 接口
def _app(d):
    return create_app(os.path.join(d, "c.db"), os.path.join(d, "cold"))


def test_api_crud_validation_and_render():
    _clean_env()
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            r = tc.post("/v1/resident/rules", json={"text": f"含 {IP4}", "scope": "group_ok"})
            assert r.status_code == 422 and "ip" in r.json()["reasons"] and "IP" in r.json()["detail"]
            r = tc.post("/v1/resident/rules", json={"text": "普通", "scope": "group_ok", "moved_from": "USER.md"})
            assert r.status_code == 422 and "moved_from_user_md" in r.json()["reasons"]
            assert tc.post("/v1/resident/rules", json={"text": "字" * 301}).status_code == 413
            assert tc.post("/v1/resident/rules", json={"text": "x", "scope": "all"}).status_code == 422
            dry = tc.post("/v1/resident/rules", json={"text": "群里也要守", "scope": "group_ok", "dry_run": True})
            assert dry.status_code == 200 and dry.json()["dry_run"] is True
            assert tc.get("/v1/resident/rules").json()["rules"] == []

            a = tc.post("/v1/resident/rules", json={"text": "群里也要守", "scope": "group_ok", "priority": "hard",
                                                     "batch_id": "b"}).json()["rule"]
            b = tc.post("/v1/resident/rules", json={"text": "只在私聊"}).json()["rule"]
            assert tc.post("/v1/resident/rules", json={"text": "只在私聊"}).status_code == 409
            assert tc.get(f"/v1/resident/rules/{a['id']}").json()["batch_id"] == "b"
            assert tc.get("/v1/resident/rules/999").status_code == 404

            dm = tc.get("/v1/resident", params={"scope": "dm"}).json()
            assert [x["id"] for x in dm["rules"]] == [a["id"], b["id"]]
            for params in ({"scope": "group"}, {"scope": "weird"}, {}):
                g = tc.get("/v1/resident", params=params).json()
                assert g["scope"] == "group" and [x["id"] for x in g["rules"]] == [a["id"]], params
                assert "只在私聊" not in g["text"]

            bad = tc.patch(f"/v1/resident/rules/{b['id']}", json={"scope": "group_ok", "text": "看 ~/x"})
            assert bad.status_code == 422 and "path" in bad.json()["reasons"]
            assert tc.patch(f"/v1/resident/rules/{b['id']}", json={"text": None}).status_code == 422
            pv = tc.patch(f"/v1/resident/rules/{b['id']}", json={"priority": "low", "dry_run": True}).json()
            assert pv["dry_run"] and tc.get(f"/v1/resident/rules/{b['id']}").json()["priority"] == "normal"
            ok = tc.patch(f"/v1/resident/rules/{b['id']}", json={"priority": "low"}).json()
            assert ok["rule"]["priority"] == "low" and ok["before"]["priority"] == "normal"
            assert tc.patch("/v1/resident/rules/999", json={"priority": "low"}).status_code == 404

            assert tc.delete(f"/v1/resident/rules/{b['id']}", params={"dry_run": "true"}).json()["dry_run"] is True
            assert tc.get(f"/v1/resident/rules/{b['id']}").json()["status"] == "active"
            assert tc.delete(f"/v1/resident/rules/{b['id']}").json()["rule"]["status"] == "retired"
            assert [x["id"] for x in tc.get("/v1/resident/rules").json()["rules"]] == [a["id"]]
            assert len(tc.get("/v1/resident/rules", params={"include_retired": "true"}).json()["rules"]) == 2
            assert [x["id"] for x in tc.get("/v1/resident", params={"scope": "dm"}).json()["rules"]] == [a["id"]]
            hist = tc.get(f"/v1/resident/rules/{b['id']}/history").json()["history"]
            assert [h["action"] for h in hist] == ["create", "update", "retire"]
            assert tc.get("/v1/resident/rules/999/history").status_code == 404


def test_api_requires_token_when_configured():
    _clean_env()
    os.environ["CORTEX_AGENT_TOKENS"] = "tokA:agentA"
    try:
        with tempfile.TemporaryDirectory() as d:
            with TestClient(_app(d)) as tc:
                good = {"Authorization": "Bearer tokA"}
                for method, path, body in [
                    ("get", "/v1/resident?scope=dm", None), ("get", "/v1/resident/rules", None),
                    ("post", "/v1/resident/rules", {"text": "x", "dry_run": True}),
                ]:
                    call = getattr(tc, method)
                    kwargs = {"json": body} if body is not None else {}
                    assert call(path, **kwargs).status_code == 401, path
                    assert call(path, headers={"Authorization": "Bearer nope"}, **kwargs).status_code == 403, path
                    assert call(path, headers=good, **kwargs).status_code == 200, path
                created = tc.post("/v1/resident/rules", json={"text": "x"}, headers=good).json()["rule"]
                for method, path in [("get", f"/v1/resident/rules/{created['id']}"), ("patch", f"/v1/resident/rules/{created['id']}"),
                                     ("delete", f"/v1/resident/rules/{created['id']}"),
                                     ("get", f"/v1/resident/rules/{created['id']}/history")]:
                    kwargs = {"json": {"priority": "low"}} if method == "patch" else {}
                    assert getattr(tc, method)(path, **kwargs).status_code == 401, (method, path)
                history = tc.get(f"/v1/resident/rules/{created['id']}/history", headers=good).json()["history"]
                assert history[0]["note"] == "api:agentA"
    finally:
        _clean_env()


# ---------------------------------------------------------------- 插件
def _plugin_client(d, get_transport, chat_type, url="http://cortex.invalid"):
    client = CortexClient(url, "tok", "sess", Outbox(os.path.join(d, "outbox.jsonl")), get_transport=get_transport)
    client.bind_session("sess", chat_type)
    return client


def test_plugin_block_dm_and_group_and_freeze():
    calls = []
    body = {
        "scope": "dm", "title": TITLE, "text": "- 群里也要守\n- 只在私聊",
        "rules": [{"id": 1, "text": "群里也要守", "scope": "group_ok"}, {"id": 2, "text": "只在私聊", "scope": "dm"}],
    }

    def transport(url, headers, timeout):
        calls.append((url, headers.get("Authorization"), timeout))
        if "scope=group" in url:
            leaked = {"scope": "group", "title": TITLE, "text": "- x",
                      "rules": [body["rules"][0], body["rules"][1], {"id": 3, "text": "见 /opt/app/x", "scope": "group_ok"}]}
            return 200, leaked, {}
        return 200, body, {}

    with tempfile.TemporaryDirectory() as d:
        dm = _plugin_client(d, transport, "dm")
        block = dm.resident_block()
        assert block == f"{TITLE}\n- 群里也要守\n- 只在私聊"
        assert dm.resident_block() == block and len(calls) == 1, "每个会话只取一次"
        assert calls[0][0].endswith("/v1/resident?scope=dm") and calls[0][1] == "Bearer tok" and calls[0][2] <= 2.0

        for chat_type in ("group", None, "weird"):
            calls.clear()
            group = _plugin_client(d, transport, chat_type).resident_block()
            assert calls[0][0].endswith("scope=group"), chat_type
            assert group == f"{TITLE}\n- 群里也要守", (chat_type, group)


def test_plugin_block_group_fail_closed_without_echo_and_empty_rules():
    with tempfile.TemporaryDirectory() as d:
        no_echo = _plugin_client(d, lambda u, h, t: (200, {"text": "- 私聊规则", "rules": []}, {}), "group")
        assert no_echo.resident_block() == ""
        empty = _plugin_client(d, lambda u, h, t: (200, {"scope": "dm", "text": "", "rules": []}, {}), "dm")
        assert empty.resident_block() == ""


def test_plugin_block_returns_empty_when_unreachable_or_broken():
    def boom(url, headers, timeout):
        raise OSError("down")

    with tempfile.TemporaryDirectory() as d:
        assert _plugin_client(d, boom, "dm").resident_block() == ""
        assert _plugin_client(d, lambda u, h, t: (500, {}, {}), "dm").resident_block() == ""
        assert _plugin_client(d, lambda u, h, t: (404, {"detail": "nope"}, {}), "dm").resident_block() == ""
        assert _plugin_client(d, lambda u, h, t: (200, {"text": 5}, {}), "dm").resident_block() == ""
        assert _plugin_client(d, lambda u, h, t: (200, ["x"], {}), "dm").resident_block() == ""
        retry = _plugin_client(d, boom, "dm")
        assert retry.resident_block() == "" and retry.resident_block() == "", "失败不缓存,可重试"


def test_plugin_block_real_transport_unreachable_port():
    import socket
    with socket.socket() as s:
        s.bind(("localhost", 0))
        port = s.getsockname()[1]
    with tempfile.TemporaryDirectory() as d:
        client = CortexClient(f"http://localhost:{port}", None, "sess", Outbox(os.path.join(d, "o.jsonl")))
        client.bind_session("sess", "dm")
        assert client.resident_block(timeout=1.0) == ""


def test_plugin_block_against_real_app_end_to_end():
    _clean_env()
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            tc.post("/v1/resident/rules", json={"text": "群里也要守", "scope": "group_ok", "priority": "hard"})
            tc.post("/v1/resident/rules", json={"text": "只在私聊"})

            def transport(url, headers, timeout):
                resp = tc.get(url.split("cortex.invalid", 1)[1])
                return resp.status_code, resp.json(), {}

            assert _plugin_client(d, transport, "dm").resident_block() == f"{TITLE}\n- 群里也要守\n- 只在私聊"
            assert _plugin_client(d, transport, "group").resident_block() == f"{TITLE}\n- 群里也要守"
            assert _plugin_client(d, transport, None).resident_block() == f"{TITLE}\n- 群里也要守"


# ---------------------------------------------------------------- 导入脚本
EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "examples", "resident_rules.example.json")


def _sender(tc):
    def send(path, body):
        resp = tc.post(path, json=body)
        return resp.status_code, resp.json()
    return send


def test_import_script_dry_run_execute_idempotent_and_all_or_nothing():
    _clean_env()
    batch_id, rules = load_manifest(EXAMPLE)
    assert batch_id == "example-batch-001" and len(rules) == 3
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            send = _sender(tc)
            dry = run_import(send, batch_id, rules, execute=False)
            assert dry["ok"] and dry["mode"] == "dry_run" and "applied" not in dry
            assert [r["outcome"] for r in dry["preview"]] == ["ok"] * 3
            assert tc.get("/v1/resident/rules").json()["rules"] == [], "默认 dry_run 不写库"

            done = run_import(send, batch_id, rules, execute=True)
            assert done["ok"] and [r["outcome"] for r in done["applied"]] == ["ok"] * 3
            stored = tc.get("/v1/resident/rules").json()["rules"]
            assert len(stored) == 3 and {r["batch_id"] for r in stored} == {batch_id}
            assert [r["scope"] for r in stored] == ["group_ok", "dm", "dm"]

            again = run_import(send, batch_id, rules, execute=True)
            assert again["ok"] and [r["outcome"] for r in again["applied"]] == ["exists"] * 3
            assert len(tc.get("/v1/resident/rules").json()["rules"]) == 3

            bad = [{"text": "新的好规则", "scope": "group_ok"}, {"text": f"连 {IP4}", "scope": "group_ok"}]
            blocked = run_import(send, "b2", bad, execute=True)
            assert not blocked["ok"] and "applied" not in blocked and "aborted" in blocked
            assert blocked["preview"][1]["outcome"] == "rejected" and "ip" in blocked["preview"][1]["reasons"]
            assert len(tc.get("/v1/resident/rules").json()["rules"]) == 3, "有一条被拒就整批不写"


def test_import_manifest_validation():
    with tempfile.TemporaryDirectory() as d:
        def load(content):
            path = os.path.join(d, "m.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            try:
                load_manifest(path)
            except ValueError:
                return False
            return True
        assert load('{"batch_id": "b", "rules": [{"text": "x"}]}')
        assert not load('{"rules": [{"text": "x"}]}')
        assert not load('{"batch_id": "b", "rules": []}')
        assert not load('{"batch_id": "b", "rules": [{"text": "x", "status": "retired"}]}')
        assert not load('{"batch_id": "b", "rules": [{"scope": "dm"}]}')


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"✅ Issue #105 常驻层测试全部通过({len(tests)} 项)")
