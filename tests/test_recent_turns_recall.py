"""Issue #37：普通对话轮次只写不读 —— RECENT_TURNS_CONTEXT 末级召回层。

验收口径见 issue #37：窗口内 user 行可召回、assistant 行不参与、超窗口排除、
显式事实优先、召回只读、返回文本裁剪。
"""
import os
import tempfile

from fastapi.testclient import TestClient

from baize.core.scratchpad import ScratchpadManager, TIMESTAMP_FORMAT
from baize.intelligence.recall_router import CognitiveRecallRouter
from baize.server.app import create_app

RECENT = "RECENT_TURNS_CONTEXT"
EXPLICIT = "SCRATCHPAD_EXPLICIT_FALLBACK"
TOKEN = "ZQX7742"  # 只存在于本测试的哨兵 token，绝不与种子数据碰撞


def _app(d):
    return create_app(os.path.join(d, "r.db"), os.path.join(d, "cold"))


def _turn(tc, role, text, **extra):
    resp = tc.post("/v1/scratchpad/log", json={"session_id": "s1", "role": role, "raw_text": text, **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _recall(tc, query, **extra):
    resp = tc.post("/v1/recall", json={"query": query, **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_recent_user_turn_is_recalled_when_nothing_else_matches():
    """窗口内的普通 user 行必须能被召回（这正是「上一轮老板让我测什么」的场景）。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            lid = _turn(tc, "user", f"重启开新会话了，把 {TOKEN} 那个懒加载再测一遍")
            out = _recall(tc, TOKEN)
            assert out["strategy"] == RECENT, out
            assert out["provisional"] is True and out["authoritative"] is False
            assert out["confidence"] <= 0.35
            assert out["source_log_ids"] == [lid]
            assert any(TOKEN in (s["current_decision"] or "") for s in out["living_states"])
            assert out["state"] is None


def test_assistant_rows_are_not_recalled():
    """助手自己的回复不进召回（否则等于用自说自话喂自己）。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _turn(tc, "assistant", f"老板，{TOKEN} 已经测过了")
            out = _recall(tc, TOKEN)
            assert out["strategy"] == "NO_MATCH", out
            assert out.get("source_log_ids", []) == []


def test_rows_outside_window_are_excluded():
    """超过 max_age_hours 的旧对话不再参与（短期连续性，不做长期记忆替代）。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            lid = _turn(tc, "user", f"很久以前说过 {TOKEN}")
            # 直接把该行的时间戳推到 200 小时前（窗口外）
            import sqlite3
            db = os.path.join(d, "r.db")
            with sqlite3.connect(db) as c:
                c.execute("UPDATE scratchpad_log SET created_at=? WHERE id=?",
                          ("2020-01-01 00:00:00", lid))
            out = _recall(tc, TOKEN)
            assert out["strategy"] == "NO_MATCH", out


def test_explicit_facts_still_win_over_recent_turns():
    """显式事实层优先级高于对话层，不得被抢占。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _turn(tc, "user", f"闲聊里提到了 {TOKEN}")
            resp = tc.post("/v1/cortex/add", json={
                "session_id": "s1", "content": f"验收口令 {TOKEN} 是北斗七号",
                "category": "fact", "reason": "r"})
            assert resp.status_code == 200, resp.text
            out = _recall(tc, TOKEN)
            assert out["strategy"] == EXPLICIT, out


def test_recent_turns_recall_is_read_only():
    """召回不得写库：行数不变、hit_count 不变。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _turn(tc, "user", f"只读检查 {TOKEN}")
            db = os.path.join(d, "r.db")
            import sqlite3
            with sqlite3.connect(db) as c:
                before = c.execute("select count(*) from scratchpad_log").fetchone()[0]
                hits_before = c.execute("select coalesce(sum(hit_count),0) from living_states").fetchone()[0]
            for _ in range(3):
                assert _recall(tc, TOKEN)["strategy"] == RECENT
            with sqlite3.connect(db) as c:
                after = c.execute("select count(*) from scratchpad_log").fetchone()[0]
                hits_after = c.execute("select coalesce(sum(hit_count),0) from living_states").fetchone()[0]
            assert before == after
            assert hits_before == hits_after


def test_long_turn_text_is_clipped():
    """超长对话行返回时裁剪到 400 字符，避免把整篇战报塞回上下文。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _turn(tc, "user", f"{TOKEN} " + "很长的战报内容。" * 200)
            out = _recall(tc, TOKEN)
            assert out["strategy"] == RECENT, out
            text = out["living_states"][0]["current_decision"]
            assert len(text) <= 401, len(text)
            assert text.startswith(TOKEN)


def test_unmatched_query_still_returns_no_match():
    """没有 token 命中时依旧是 NO_MATCH，不能把任何旧对话都倒出来。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _turn(tc, "user", "今天天气不错")
            out = _recall(tc, "谢谢")
            assert out["strategy"] == "NO_MATCH", out
