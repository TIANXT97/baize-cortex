"""Issue #105 PR2:请求里顺手量字数的触发点、不拖慢请求、群聊不量不写、写失败兜底(客户端 + 服务端)、管理接口"""
import json
import os
import tempfile
import threading
import time
from unittest import mock

from fastapi.testclient import TestClient

from baize.client.outbox import Outbox
from baize.client.plugin import MIRROR_ORIGIN, WRITE_FAILED_ORIGIN, CortexClient, SessionScopes, failed_memory_writes
from baize.core import memory_migrator as mm
from baize.core.resident import ResidentManager
from baize.server.app import create_app
from tests._mm_helpers import FakeLLM, know_entry, pad, rule_entry, workspace

OVER = [pad("【底线】回复先给结论。", 200)] + [rule_entry(i) for i in range(1, 4)] + [know_entry(i) for i in range(1, 7)]


class Env:
    def __init__(self, ws, tc, app):
        self.ws, self.tc, self.app = ws, tc, app

    @property
    def migrator(self):
        return self.app.state.migrator

    def runs(self):
        return self.ws.conn.execute(
            "SELECT trigger_source, mode, status FROM memory_migration_runs ORDER BY id").fetchall()

    def wait(self):
        assert self.migrator.wait_idle(15)


def with_app(memory=None, user=None, mode="apply", llm=None):
    """返回上下文管理器:临时 Hermes 主目录 + 真 FastAPI 应用(TestClient)。"""
    import contextlib

    @contextlib.contextmanager
    def cm():
        with workspace(memory=memory, user=user) as ws:
            if mode is not None:
                os.environ["CORTEX_MEMORY_MIGRATION"] = mode
            app = create_app(ws.db_path, os.path.join(ws.root, "cold"))
            with TestClient(app) as tc:
                app.state.migrator.llm_client = llm or FakeLLM()
                yield Env(ws, tc, app)
    return cm()


def recall(env, scope="dm"):
    body = {"query": "任意查询内容", "top_k": 3}
    if scope is not None:
        body["scope"] = scope
    return env.tc.post("/v1/recall", json=body)


# ---------------------------------------------------------------- 触发点
def test_recall_in_dm_scope_triggers_background_migration():
    with with_app(memory=OVER) as env:
        before = env.ws.read("MEMORY.md")
        assert recall(env).status_code == 200
        env.wait()
        assert env.runs() == [("recall", "apply", "done")]
        assert env.ws.read("MEMORY.md") != before and mm.count_chars(env.ws.read("MEMORY.md")) < 1320


def test_default_mode_is_dry_run_and_leaves_everything_alone():
    with with_app(memory=OVER, mode=None) as env:
        digest, counts = env.ws.digest("MEMORY.md"), env.ws.table_counts()
        assert os.environ.get("CORTEX_MEMORY_MIGRATION") is None
        recall(env)
        env.wait()
        assert env.runs() == [("recall", "dry_run", "dry_run")]
        assert env.ws.digest("MEMORY.md") == digest and env.ws.table_counts() == counts


def test_off_mode_recall_does_not_measure_or_record():
    with with_app(memory=OVER, mode="off") as env:
        with mock.patch.object(env.migrator, "_count_for") as count:
            assert recall(env).status_code == 200
            assert not count.called
        assert env.runs() == []


def test_group_and_undeclared_scope_recall_never_measures():
    with with_app(memory=OVER) as env:
        with mock.patch.object(env.migrator, "_count_for", return_value=None) as count:
            assert recall(env, "group").status_code == 200
            assert recall(env, "forum").status_code == 200
            assert recall(env, None).status_code == 200
            assert not count.called, "群聊、认不出的 scope 不量字数"
            assert recall(env, "dm").status_code == 200
            assert count.call_count == 2, "私聊才量(MEMORY.md 与 USER.md 各一次)"
        env.wait()
        assert env.runs() == []


def test_sync_turn_log_triggers_but_plain_cortex_add_does_not():
    with with_app(memory=OVER) as env:
        assert env.tc.post("/v1/cortex/add", json={
            "session_id": "s", "content": "普通显式写入", "category": "fact", "reason": "手动"}).status_code == 200
        env.wait()
        assert env.runs() == [], "cortex_add 的行为不变,不触发"
        assert env.tc.post("/v1/scratchpad/log", json={
            "session_id": "s", "role": "user", "raw_text": "一轮对话"}).status_code == 200
        env.wait()
        assert [r[0] for r in env.runs()] == ["sync_turn"]


def test_mirror_write_and_write_failed_origin_trigger():
    for origin, trigger, extra in ((MIRROR_ORIGIN, "on_memory_write", {}),
                                   (WRITE_FAILED_ORIGIN, "write_failed", {"moved_from": "MEMORY.md"})):
        with with_app(memory=OVER) as env:
            response = env.tc.post("/v1/cortex/add", json={
                "session_id": "s", "content": "刚写成功的一条", "category": "fact",
                "reason": "x", "origin": origin, **extra})
            assert response.status_code == 200, response.text
            env.wait()
            assert [r[0] for r in env.runs()] == [trigger], origin
    with with_app(memory=OVER) as env:
        again = {"session_id": "s", "content": "重复的一条", "category": "fact", "reason": "x", "origin": MIRROR_ORIGIN}
        env.tc.post("/v1/cortex/add", json=again)
        env.wait()
        assert env.tc.post("/v1/cortex/add", json=again).json()["deduplicated"] is True


def test_mirror_through_client_outbox_reaches_server_with_origin():
    with with_app(memory=OVER) as env:
        client, outbox = make_client(env, "sess-m")
        client.on_memory_write("add", "MEMORY.md", "镜像过来的写入", None)
        assert outbox.flush()
        env.wait()
        assert [r[0] for r in env.runs()] == ["on_memory_write"]


# ---------------------------------------------------------------- 请求不被拖慢、异常不让请求失败
def test_recall_stays_fast_when_migration_is_slow():
    gate, started = threading.Event(), threading.Event()

    def slow():
        started.set()
        gate.wait(15)

    with with_app(memory=OVER, llm=FakeLLM(delay=slow)) as env:
        begin = time.monotonic()
        response = recall(env)
        elapsed = time.monotonic() - begin
        assert response.status_code == 200 and elapsed < 1.0, f"外迁很慢时 recall 照样快速返回({elapsed:.2f}s)"
        assert started.wait(5) and env.migrator._thread.is_alive(), "外迁在后台线程里跑"
        begin = time.monotonic()
        for _ in range(5):
            assert recall(env).status_code == 200
        assert time.monotonic() - begin < 2.0
        gate.set()
        env.wait()
        assert [r[2] for r in env.runs()] == ["done"]


def test_measure_failures_never_fail_the_request():
    with with_app(memory=OVER) as env:
        for target in ("_count_for", "_maybe_start"):
            with mock.patch.object(env.migrator, target, side_effect=RuntimeError("boom")):
                begin = time.monotonic()
                assert recall(env).status_code == 200
                assert env.tc.post("/v1/scratchpad/log", json={
                    "session_id": "s", "role": "user", "raw_text": "x"}).status_code == 200
                assert env.tc.post("/v1/cortex/add", json={
                    "session_id": "s", "content": "y", "category": "fact", "reason": "r",
                    "origin": MIRROR_ORIGIN}).status_code == 200
                assert time.monotonic() - begin < 1.0
        with mock.patch.object(env.migrator, "on_request", side_effect=RuntimeError("boom")):
            assert recall(env).status_code == 200
        with mock.patch.object(mm.MemoryMigrator, "run_file", side_effect=RuntimeError("late")):
            assert recall(env).status_code == 200
            env.wait()
    with with_app(memory=OVER) as env:
        os.unlink(env.ws.path("MEMORY.md"))
        assert recall(env).status_code == 200, "记忆文件不存在也不报错"
        env.ws.write("MEMORY.md", b"\xff\xfe\xfa".decode("latin-1"))
        os.chmod(env.ws.path("MEMORY.md"), 0)
        try:
            assert recall(env).status_code == 200, "读不了文件也不报错"
        finally:
            os.chmod(env.ws.path("MEMORY.md"), 0o600)


def test_memory_db_has_no_migrator_and_requests_still_work():
    saved = os.environ.get("CORTEX_HERMES_HOME")
    app = create_app(":memory:", tempfile.mkdtemp())
    with TestClient(app) as tc:
        assert app.state.migrator is None
        assert tc.post("/v1/recall", json={"query": "x y", "scope": "dm"}).status_code == 200
        assert tc.post("/v1/admin/memory-migration/rollback", json={"batch_id": "b"}).status_code == 422
    assert os.environ.get("CORTEX_HERMES_HOME") == saved


# ---------------------------------------------------------------- 写失败兜底:解析
def call(call_id, args, name="memory"):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


def result(call_id, payload):
    return {"role": "tool", "tool_call_id": call_id, "content": payload if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False)}


ADD_FAIL = {"success": False, "error": "Memory at 2,100/2,200 chars; adding this entry (150 chars) would exceed the limit by 50 chars."}
PUT_FAIL = {"success": False, "error": "Replacement would put memory at 2,300/2,200 chars: free at least 100 chars"}
BATCH_FAIL = {"success": False, "error": "Applying all 2 operations would put memory at 2,250/2,200 chars: free at least 50 chars more."}


def test_failed_writes_parsed_from_turn_messages():
    msgs = [
        {"role": "user", "content": "旧的一轮"},
        call("old", {"action": "add", "content": "上一轮的失败不取"}), result("old", ADD_FAIL),
        {"role": "user", "content": "这一轮"},
        call("a", {"action": "add", "target": "memory", "content": " 加一条 "}), result("a", ADD_FAIL),
        call("b", {"action": "replace", "target": "user", "old_text": "旧", "content": "新的整条"}), result("b", PUT_FAIL),
        call("c", {"target": "memory", "operations": [
            {"action": "remove", "old_text": "x"}, {"action": "add", "content": "批量里的新增"},
            {"action": "replace", "old_text": "y", "new_text": "批量里的替换"}, {"action": "patch", "old_text": "p", "new_text": "片段"}]}),
        result("c", BATCH_FAIL),
        call("d", {"action": "add", "content": "写成功的不取"}), result("d", {"success": True, "message": "Entry added."}),
        call("e", {"action": "add", "content": "不是超限的失败不取"}), result("e", {"success": False, "error": "No entry matched"}),
        call("f", {"action": "patch", "old_text": "p", "new_text": "补丁片段"}),
        result("f", {"success": False, "error": "Patch would put memory at 2,300/2,200 chars."}),
        call("g", {"action": "add", "content": "别的工具"}, name="terminal"), result("g", ADD_FAIL),
        call("h", {"action": "add", "content": "结果不是 JSON"}), result("h", "Error: boom"),
        call("i", {"action": "add", "content": "加一条"}), result("i", ADD_FAIL),
        result("zzz", ADD_FAIL),
        {"role": "assistant", "content": "好的", "tool_calls": [{"id": "bad", "function": {"name": "memory", "arguments": "{oops"}}]},
        result("bad", ADD_FAIL),
    ]
    assert failed_memory_writes(msgs) == [
        ("MEMORY.md", "加一条"), ("USER.md", "新的整条"), ("MEMORY.md", "批量里的新增"), ("MEMORY.md", "批量里的替换"),
    ], "只取本轮超上限失败的 add/replace;成功的、别的失败、patch、别的工具、坏数据都不取;重复只取一次"
    assert failed_memory_writes(None) == [] and failed_memory_writes([]) == [] and failed_memory_writes("x") == []
    parts = [call("p", {"action": "add", "content": "分段内容"}), {"role": "tool", "tool_call_id": "p", "content": [
        {"type": "text", "text": json.dumps(ADD_FAIL)}]}]
    assert failed_memory_writes(parts) == [("MEMORY.md", "分段内容")]


# ---------------------------------------------------------------- 写失败兜底:客户端 + 服务端
def make_client(env, session="sess-1", chat_type="dm", transport=None):
    def to_app(url, body, headers, timeout):
        path = url.split("//", 1)[1].split("/", 1)[1]
        response = env.tc.post("/" + path, json=body)
        return response.status_code, response.json(), dict(response.headers)

    outbox = Outbox(os.path.join(env.ws.root, f"outbox-{session}.jsonl"))
    scopes = SessionScopes()
    scopes.bind(session, chat_type)
    client = CortexClient("http://app", None, session, outbox, transport=transport or to_app, scopes=scopes)
    client._detector = mock.Mock(detect_phase_rich=mock.Mock(return_value=("none", 0.0, [], 0.0)))
    return client, outbox


def failed_rows(env):
    return env.ws.conn.execute(
        "SELECT raw_text, reason, phase_hint, category FROM scratchpad_log WHERE reason LIKE '%write_failed%' ORDER BY id"
    ).fetchall()


MSGS = [{"role": "user", "content": "记一下"}, call("a", {"action": "add", "target": "memory", "content": "超限的一条,含全角,逗号"}),
        result("a", ADD_FAIL)]


def test_write_failure_saved_as_explicit_addition_with_moved_from_and_idempotent():
    with with_app(memory=OVER) as env:
        client, _ = make_client(env)
        assert client.store_failed_memory_writes(MSGS) == 1
        rows = failed_rows(env)
        assert rows == [("超限的一条,含全角,逗号", "moved_from=MEMORY.md;write_failed", "explicit_addition", "fact")], \
            "原文不做 NFKC,全角逗号保留"
        client.store_failed_memory_writes(MSGS)
        client.sync_turn("u", "a", messages=MSGS)
        assert len(failed_rows(env)) == 1, "同一条重复失败只存一次(进程内去重)"
        other, _ = make_client(env, "sess-2")
        other.store_failed_memory_writes(MSGS)
        fresh, _ = make_client(env, "sess-1")
        fresh.store_failed_memory_writes(MSGS)
        assert len(failed_rows(env)) == 1, "换会话、重启后重复失败,服务端按内容去重"
        env.wait()
        assert "write_failed" in [r[0] for r in env.runs()], "同时触发了一次外迁检查"


def test_sync_turn_without_messages_behaves_as_before():
    with with_app(memory=OVER) as env:
        client, _ = make_client(env)
        client.sync_turn("你好", "你好呀")
        client.sync_turn("你好", "你好呀", session_id="sess-1", messages=None)
        assert failed_rows(env) == [], "入口没打补丁(不传 messages)时其余功能照常,只是没有写失败兜底"
        assert env.ws.conn.execute("SELECT COUNT(*) FROM scratchpad_log WHERE role='user' AND session_id='sess-1'").fetchone()[0] == 2


def test_group_and_unknown_scope_sessions_do_not_store_or_call_network():
    with with_app(memory=OVER) as env:
        def never(*a):
            raise AssertionError("群聊不能有任何网络调用")
        for chat_type in ("group", "forum", None, "weird"):
            client, _ = make_client(env, "g-" + str(chat_type), chat_type, transport=never)
            assert client.store_failed_memory_writes(MSGS) == 0
            client.sync_turn("", "", messages=MSGS)
        unbound = CortexClient("http://app", None, "nobody", Outbox(os.path.join(env.ws.root, "o.jsonl")), transport=never)
        assert unbound.store_failed_memory_writes(MSGS) == 0
        assert failed_rows(env) == []


def test_user_target_failure_goes_to_private_profile_card_only():
    user_fail = [{"role": "user", "content": "记"}, call("u", {"action": "add", "target": "user", "content": "用户的一条背景"}),
                 result("u", ADD_FAIL)]
    with with_app(user=[know_entry(1, 100)]) as env:
        env.ws.add_card("proj:user_profile", "private")
        client, _ = make_client(env)
        client.store_failed_memory_writes(user_fail)
        assert failed_rows(env)[0][:2] == ("proj:user_profile 用户的一条背景", "moved_from=USER.md;write_failed")
    with with_app(user=[know_entry(1, 100)]) as env:
        env.ws.add_card("proj:user_profile", "group_ok")
        client, _ = make_client(env)
        client.store_failed_memory_writes(user_fail)
        assert failed_rows(env)[0][0] == "用户的一条背景", "卡不是私有就不带锚点"


def test_write_failed_endpoint_validation_and_transport_failure_queues():
    with with_app(memory=OVER) as env:
        bad = env.tc.post("/v1/cortex/add", json={"session_id": "s", "content": "x", "category": "fact", "reason": "r",
                                                   "origin": WRITE_FAILED_ORIGIN, "moved_from": "NOTES.md"})
        assert bad.status_code == 422
        missing = env.tc.post("/v1/cortex/add", json={"session_id": "s", "content": "x", "category": "fact",
                                                       "reason": "r", "origin": WRITE_FAILED_ORIGIN})
        assert missing.status_code == 422
        plain = env.tc.post("/v1/cortex/add", json={"session_id": "s", "content": "普通", "category": "rule",
                                                     "reason": "原因", "moved_from": "MEMORY.md"})
        assert plain.status_code == 200
        row = env.ws.conn.execute("SELECT reason FROM scratchpad_log WHERE id = ?", (plain.json()["id"],)).fetchone()
        assert row[0] == "原因", "没带 write_failed 的写入,moved_from 不起作用、reason 原样"

        def down(*args):
            raise ConnectionError("down")
        client, outbox = make_client(env, "sess-q", transport=down)
        client.store_failed_memory_writes(MSGS)
        assert outbox.pending() == 1, "连不上 cortex 时入队重试,内容不丢"
        payload = json.loads(open(outbox.path, encoding="utf-8").readline())["payload"]
        assert payload["origin"] == WRITE_FAILED_ORIGIN and payload["moved_from"] == "MEMORY.md"


# ---------------------------------------------------------------- 管理接口
def test_admin_endpoints_runs_and_rollback():
    with with_app(memory=OVER) as env:
        recall(env)
        env.wait()
        runs = env.tc.get("/v1/admin/memory-migration/runs?limit=5").json()["runs"]
        assert len(runs) == 1 and runs[0]["status"] == "done" and runs[0]["report"]["items"]
        batch = runs[0]["batch_id"]
        assert env.tc.get(f"/v1/admin/memory-migration/runs?batch_id={batch}").json()["runs"][0]["batch_id"] == batch
        assert env.tc.get("/v1/admin/memory-migration/runs?file=USER.md").json()["runs"] == []
        moved_texts = [i["text"] for i in runs[0]["report"]["items"] if i["status"] == "moved"]
        preview = env.tc.post("/v1/admin/memory-migration/rollback", json={"batch_id": batch})
        assert preview.status_code == 200 and preview.json()["dry_run"] is True, "默认 dry_run"
        assert set(env.ws.entries("MEMORY.md")).isdisjoint(moved_texts)
        done = env.tc.post("/v1/admin/memory-migration/rollback", json={"batch_id": batch, "dry_run": False})
        assert done.status_code == 200 and done.json()["status"] == "rolled_back"
        assert all(t in env.ws.entries("MEMORY.md") for t in moved_texts)
        assert ResidentManager(env.ws.conn).list() == []
        assert env.tc.post("/v1/admin/memory-migration/rollback", json={"batch_id": batch, "dry_run": False}).status_code == 409
        assert env.tc.post("/v1/admin/memory-migration/rollback", json={"batch_id": "nope"}).status_code == 404


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"✅ Issue #105 PR2 触发点与写失败兜底测试全部通过({len(tests)} 项)")
