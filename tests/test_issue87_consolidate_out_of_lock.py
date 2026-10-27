"""Issue #87:夜间整理调模型挪出全局锁、整理专用互斥、卡片变更重新检查

用假的慢 System 2(chat 里 sleep),临时库 + TestClient + 线程;不碰线上服务。
"""
import gzip
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager

from fastapi.testclient import TestClient

from baize.server.app import create_app

ENTITY = "proj:cache"
CARD_NAME = "缓存层"
OLD = "使用 Redis 做缓存"
NEW = "Memcached"
TABLES = ("scratchpad_log", "living_states", "living_state_history", "graph_nodes", "graph_edges")
SLOW = 1.5


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


class SlowLLM:
    """假的 System 2:每次调用先 sleep,再按请求类型返回合法 JSON;fail 不为空时 sleep 后抛错。"""

    def __init__(self, delay=SLOW, fail=None):
        self.delay = delay
        self.fail = fail
        self.calls = 0
        self.entered = threading.Event()
        self._guard = threading.Lock()

    def chat(self, system, user):
        with self._guard:
            self.calls += 1
        self.entered.set()
        time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        try:
            payload = json.loads(user)
        except ValueError:
            payload = None
        if isinstance(payload, list):
            return json.dumps([
                {"id": item["id"], "phase": "chitchat", "noul": 0.1, "confidence": 0.9} for item in payload
            ])
        if "候选文本" in user:
            return json.dumps({"is_fact": True, "entity": ""})
        match = re.search(r"定案:(.+)", user)
        decision = match.group(1).strip().splitlines()[0] if match else "示例方案"
        return json.dumps({"decision": decision, "superseded": None, "display_name": None, "aliases": []})


def _app(d):
    return create_app(os.path.join(d, "p.db"), os.path.join(d, "cold"))


def _dump(conn):
    return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in TABLES}


def _card(tc, decision=OLD):
    resp = tc.post("/v1/living-state/decision", json={
        "entity_id": ENTITY, "display_name": CARD_NAME, "new_decision": decision})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _log(tc, text, hint="approval", session="s1"):
    resp = tc.post("/v1/scratchpad/log", json={"session_id": session, "role": "user", "raw_text": text, "phase_hint": hint})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _spawn(fn):
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 测试线程把异常带回主线程
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _consolidate(tc, **body):
    return tc.post("/v1/sleep/consolidate", json=body).json()


def _timed_recall(tc, query=CARD_NAME):
    started = time.monotonic()
    resp = tc.post("/v1/recall", json={"query": query})
    return time.monotonic() - started, resp.json()


def _archived_texts(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line)["raw_text"] for line in fh if line.strip()]


def _start_slow_run(tc, llm, **body):
    thread, box = _spawn(lambda: _consolidate(tc, **body))
    assert llm.entered.wait(10), "整理线程应已进入慢模型调用"
    return thread, box


def test_recall_during_consolidation_returns_fast_and_not_empty():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            llm = SlowLLM()
            tc.app.state.consolidator.llm_client = llm
            _card(tc)
            _log(tc, "proj:other 定案:示例方案甲")
            thread, box = _start_slow_run(tc, llm)
            waited, out = _timed_recall(tc)
            assert waited < SLOW / 2, f"召回不应被整理挡住,实际等待 {waited:.2f}s"
            assert out["strategy"] == "LIVING_STATE_DIRECT" and [s["entity_id"] for s in out["living_states"]] == [ENTITY]
            assert thread.is_alive(), "召回返回时整理仍在进行,说明确实是并发"
            thread.join(15)
            assert box["value"]["status"] == "ok" and box["value"]["updated_states"] == ["proj:other"]


def test_concurrent_writes_are_not_lost_or_archived():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            llm = SlowLLM()
            tc.app.state.consolidator.llm_client = llm
            first = _log(tc, "proj:one 定案:示例方案甲")
            thread, box = _start_slow_run(tc, llm)
            started = time.monotonic()
            late_decision = _log(tc, "proj:late 定案:迟到的方案")
            late_plain = _log(tc, "整理期间写入的一条普通对话", hint=None)
            assert time.monotonic() - started < SLOW / 2, "整理期间写入不应被挡住"
            thread.join(15)
            report = box["value"]
            assert report["archived_logs"] == 1 and report["updated_states"] == ["proj:one"]
            assert "迟到的方案" not in " ".join(_archived_texts(report["archive_file"])), "新写入的行不能被这次整理归档"
            conn = tc.app.state.conn
            remaining = {r[0] for r in conn.execute("SELECT id FROM scratchpad_log")}
            assert first not in remaining and {late_decision, late_plain} <= remaining

            llm.entered.clear()
            second = _consolidate(tc)
            assert second["updated_states"] == ["proj:late"], "迟到的行留到下一次整理,没有丢"


def test_card_changed_during_consolidation_is_not_overwritten():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            llm = SlowLLM()
            tc.app.state.consolidator.llm_client = llm
            _card(tc)
            source = _log(tc, f"{ENTITY} 定案:{NEW}")
            thread, box = _start_slow_run(tc, llm)
            changed = _card(tc, "整理期间手动改成的新定案")
            thread.join(15)
            report = box["value"]
            assert report["status"] == "ok" and report["updated_states"] == [] and report["archive_file"] is None
            assert report["archived_logs"] == 0
            assert report["skipped_changed"] == [{"entity_id": ENTITY, "reason": "card_changed"}]
            card = tc.get(f"/v1/living-state/{ENTITY}").json()
            assert card["current_decision"] == "整理期间手动改成的新定案" and card["version"] == changed["version"]
            row = tc.app.state.conn.execute("SELECT phase_hint FROM scratchpad_log WHERE id = ?", (source,)).fetchone()
            assert row == ("approval",), "来源草稿行留到下次,没有被归档"


def test_two_concurrent_consolidations_run_only_once():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            llm = SlowLLM()
            tc.app.state.consolidator.llm_client = llm
            _log(tc, "proj:one 定案:示例方案甲")
            thread, box = _start_slow_run(tc, llm)
            started = time.monotonic()
            second = _consolidate(tc)
            assert time.monotonic() - started < SLOW / 2, "第二次整理应立即返回,不排队"
            assert second["status"] == "busy"
            assert second["archived_logs"] == 0 and second["updated_states"] == [] and second["archive_file"] is None
            thread.join(15)
            assert box["value"]["status"] == "ok" and box["value"]["updated_states"] == ["proj:one"]
            assert llm.calls == 1, "模型只被调用一次"
            assert _consolidate(tc)["status"] == "ok", "第一次结束后互斥已释放"


def test_dry_run_never_writes_and_does_not_hold_the_mutex():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            tc.app.state.consolidator.llm_client = SlowLLM(delay=0.05)
            _card(tc)
            _log(tc, f"{ENTITY} 定案:{NEW}")
            _log(tc, "proj:fresh 定案:全新的示例方案")
            before = _dump(tc.app.state.conn)

            preview = _consolidate(tc, dry_run=True)
            assert preview["status"] == "dry_run" and preview["would_archive_logs"] == 2
            assert _dump(tc.app.state.conn) == before, "dry_run 前后逐表一致"
            cold = os.path.join(d, "cold")
            assert not os.path.exists(cold) or os.listdir(cold) == [], "dry_run 不写冷归档"
            assert not tc.app.state.conn.in_transaction

            again = _consolidate(tc, dry_run=True)
            assert again["status"] == "dry_run" and again["would_update_states"] == preview["would_update_states"]

            real = _consolidate(tc)
            assert real["status"] == "ok", "dry_run 之后真实整理不受互斥影响"
            assert sorted(real["updated_states"]) == sorted(preview["would_update_states"])
            card = tc.get(f"/v1/living-state/{ENTITY}").json()
            assert card["current_decision"] == NEW and [t["plan"] for t in card["tombstones"]] == [OLD]


def test_errors_release_the_lock_and_the_mutex():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d), raise_server_exceptions=False) as tc:
            consolidator = tc.app.state.consolidator
            conn = tc.app.state.conn

            # 模型报错/超时:各路径都会降级,整理照常完成
            for failure in (RuntimeError("模型报错"), TimeoutError("30s")):
                consolidator.llm_client = SlowLLM(delay=0.05, fail=failure)
                _log(tc, "proj:one 定案:示例方案甲")
                report = _consolidate(tc)
                assert report["status"] == "ok" and report["updated_states"] == ["proj:one"], type(failure)
                assert not conn.in_transaction

            # 分析阶段、回写阶段抛出未捕获异常:接口 500,但锁与互斥都必须释放
            consolidator.llm_client = SlowLLM(delay=0.01)
            for name in ("_analyze", "_apply"):
                original = getattr(consolidator, name)

                def boom(*args, **kwargs):
                    raise RuntimeError("注入故障")

                setattr(consolidator, name, boom)
                _log(tc, "proj:two 定案:示例方案乙")
                assert tc.post("/v1/sleep/consolidate", json={}).status_code == 500, name
                setattr(consolidator, name, original)
                assert not conn.in_transaction, name
                thread, box = _spawn(lambda: _timed_recall(tc))
                thread.join(5)
                assert not thread.is_alive() and box["value"][0] < 1.0, f"{name} 之后全局锁应已释放"

            recovered = _consolidate(tc)
            assert recovered["status"] == "ok" and recovered["updated_states"] == ["proj:two"], "互斥已释放,可再次整理"


def test_batch_judge_model_calls_do_not_hold_the_lock():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply", CORTEX_BATCH_JUDGE="on"):
        with TestClient(_app(d)) as tc:
            llm = SlowLLM()
            tc.app.state.consolidator.llm_client = llm
            _card(tc)
            pending = _log(tc, "待批量判研的一条普通对话", hint=None)
            expired_midway = _log(tc, "整理期间被软删除的一条普通对话", hint=None)
            status = lambda i: tc.app.state.conn.execute(
                "SELECT judge_status, phase_hint, expired_at IS NOT NULL FROM scratchpad_log WHERE id = ?", (i,)).fetchone()
            assert status(pending)[0] == "pending"

            thread, box = _start_slow_run(tc, llm)
            waited, out = _timed_recall(tc)
            assert waited < SLOW / 2 and out["strategy"] == "LIVING_STATE_DIRECT"
            resp = tc.post("/v1/admin/expire-scratchpad", json={"ids": [expired_midway], "dry_run": False})
            assert resp.status_code == 200 and resp.json()["expired"] == 1
            thread.join(15)
            report = box["value"]
            assert report["batch_judge"]["batches"] == 1 and report["batch_judge"]["judged"] == 1
            assert status(pending) == ("done", None, 0)
            assert status(expired_midway) == ("pending", None, 1), "判研期间被软删除的行不写回判研结果"


if __name__ == "__main__":
    test_recall_during_consolidation_returns_fast_and_not_empty()
    test_concurrent_writes_are_not_lost_or_archived()
    test_card_changed_during_consolidation_is_not_overwritten()
    test_two_concurrent_consolidations_run_only_once()
    test_dry_run_never_writes_and_does_not_hold_the_mutex()
    test_errors_release_the_lock_and_the_mutex()
    test_batch_judge_model_calls_do_not_hold_the_lock()
    print("✅ Issue #87 整理挪出全局锁测试全部通过")
