"""Issue #39：RECENT_TURNS_CONTEXT 三层收口。

验收口径见 issue #39：代理隔离、零信息量/纯语气噪声不得注入、命中数优先排序。
四个断言的共同目标——末级兜底只在"真的接得上上一轮"时触发，不把闲聊灌进上下文。
"""
import os
import sqlite3
import tempfile

from fastapi.testclient import TestClient

from baize.server.app import create_app

RECENT = "RECENT_TURNS_CONTEXT"
AUTH_A = {"Authorization": "Bearer tok-a"}
AUTH_B = {"Authorization": "Bearer tok-b"}


def _app(d):
    return create_app(os.path.join(d, "r.db"), os.path.join(d, "cold"))


def _turn(tc, text, headers=None):
    resp = tc.post("/v1/scratchpad/log",
                   json={"session_id": "s1", "role": "user", "raw_text": text},
                   headers=headers or {})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _recall(tc, query, headers=None):
    resp = tc.post("/v1/recall", json={"query": query}, headers=headers or {})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _texts(out):
    return " ".join(s["current_decision"] or "" for s in out.get("living_states", []))


def test_agent_isolation_between_tokens():
    """配置了多代理 token 后，甲代理的普通对话不得出现在乙代理的召回里。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            tc.app.state.agent_tokens = {"tok-a": "agent-a", "tok-b": "agent-b"}
            _turn(tc, "ZQX8801 甲代理的私记内容", AUTH_A)
            _turn(tc, "ZQX8801 乙代理的私记内容", AUTH_B)

            out_a = _recall(tc, "ZQX8801", AUTH_A)
            assert out_a["strategy"] == RECENT, out_a
            assert "甲代理" in _texts(out_a)
            assert "乙代理" not in _texts(out_a), out_a

            out_b = _recall(tc, "ZQX8801", AUTH_B)
            assert "乙代理" in _texts(out_b)
            assert "甲代理" not in _texts(out_b), out_b


def test_legacy_rows_without_agent_id_remain_recallable():
    """tokens 未配置时期的历史行 agent_id 为 NULL，不能被隔离一刀切砍掉。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            db = os.path.join(d, "r.db")
            with sqlite3.connect(db) as c:
                c.execute(
                    "INSERT INTO scratchpad_log (session_id, role, raw_text, created_at, agent_id)"
                    " VALUES ('legacy', 'user', 'ZQX8802 迁移前写下的历史对话', datetime('now'), NULL)"
                )
            out = _recall(tc, "ZQX8802")
            assert out["strategy"] == RECENT, out
            assert "迁移前写下" in _texts(out)


def test_zero_information_rows_are_skipped():
    """整行只有两三个字的应答（「可以」）不是记忆，不得注入。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _turn(tc, "ZZ9")
            keep = _turn(tc, "ZZ9 这条才是有实质内容的完整对话")
            out = _recall(tc, "ZZ9")
            assert out["strategy"] == RECENT, out
            assert out["source_log_ids"] == [keep], out


def test_pure_filler_queries_do_not_trigger_recent_turns():
    """「可以」「这个」「然后」「好的」这类纯语气词不得拉起历史对话。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _turn(tc, "可以可以，这样就可以了。")
            _turn(tc, "然后我们就继续做那个事情。")
            for query in ("可以", "这个", "然后", "好的"):
                out = _recall(tc, query)
                assert out["strategy"] != RECENT, f"{query} -> {out}"


def test_filler_phrase_does_not_drag_in_unrelated_history():
    """老板口头禅「按你说的办」不得把只共享「你说」的无关旧对话拉进来（实测缺陷形态）。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _turn(tc, "我还没把你放群里呢，你说得铁律可以先给你自己打上")
            _turn(tc, "本尊哪里已经有个现成的方案，就是懒加载")
            out = _recall(tc, "好，按你说的办")
            assert out["strategy"] != RECENT, out


def test_substantive_short_query_still_recalls():
    """收口不能把核心用例打死：一句口语化的实质 query 仍要接上上一轮。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            lid = _turn(tc, "继续吧，把工具懒加载做完。")
            out = _recall(tc, "继续测")
            assert out["strategy"] == RECENT, out
            assert out["source_log_ids"] == [lid], out


def test_more_token_hits_rank_before_newer_rows():
    """命中 token 多的行优先于更新的行，否则一个高频词就能把无关旧对话顶上来。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            older = _turn(tc, "AAA111 和 BBB222 两条都提到了")
            _turn(tc, "只有 AAA111 这一条提到")
            db = os.path.join(d, "r.db")
            with sqlite3.connect(db) as c:
                # 仍在 72h 窗口内（60 小时前），只是比另一条旧
                c.execute("UPDATE scratchpad_log SET created_at=datetime('now','-60 hours') WHERE id=?", (older,))
            out = _recall(tc, "AAA111 BBB222")
            assert out["strategy"] == RECENT, out
            assert out["source_log_ids"][0] == older, out
