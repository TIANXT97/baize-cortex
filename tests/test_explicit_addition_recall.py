"""Issue #25:explicit_addition 在慢波固化前后的召回可见性,末级兜底纯只读"""
import os
import tempfile

from fastapi.testclient import TestClient

from baize.core.living_state import LivingStateManager
from baize.core.normalization import idempotency_key
from baize.core.scratchpad import ScratchpadManager
from baize.intelligence.recall_router import CognitiveRecallRouter
from baize.server.app import create_app
from baize.storage.db import init_db

FALLBACK = "SCRATCHPAD_EXPLICIT_FALLBACK"


def _app(d):
    return create_app(os.path.join(d, "r.db"), os.path.join(d, "cold"))


def _add_http(tc, text, **extra):
    resp = tc.post("/v1/cortex/add", json={"session_id": "s", "content": text, "category": "fact", "reason": "r", **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _recall(tc, query, **extra):
    return tc.post("/v1/recall", json={"query": query, **extra}).json()


def _add(sp, text):
    return sp.add_explicit("s", text, "fact", "r", idempotency_key("s", text), "default")


def test_fact_is_visible_before_consolidation_as_provisional():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            lid = _add_http(tc, "夭夭验收口令是北斗七号")
            out = _recall(tc, "北斗七号")
            assert out["strategy"] == FALLBACK, out
            assert out["provisional"] is True and out["authoritative"] is False
            assert out["confidence"] == 0.5
            assert out["source_log_ids"] == [lid]


def test_after_consolidation_the_living_card_answers_instead_of_fallback():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _add_http(tc, "夭夭验收口令是北斗七号")
            report = tc.post("/v1/sleep/consolidate", json={}).json()
            assert report["archived_logs"] == 1, report
            out = _recall(tc, "北斗七号")
            assert out["strategy"] != FALLBACK, out
            assert out["state"]["entity_id"] == "proj:general_decisions"


def test_living_card_hit_never_triggers_fallback():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            tc.post("/v1/living-state/decision", json={
                "entity_id": "proj:alpha", "display_name": "阿尔法项目", "new_decision": "方案A"})
            _add_http(tc, "阿尔法项目口令是北斗七号")
            out = _recall(tc, "阿尔法项目")
            assert out["strategy"] == "LIVING_STATE_DIRECT", out
            assert "provisional" not in out


def test_fallback_is_strictly_read_only():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "ro.db"))
        try:
            sp = ScratchpadManager(conn)
            _add(sp, "口令是北斗七号")
            router = CognitiveRecallRouter(conn)
            tables = ("scratchpad_log", "living_states", "graph_nodes", "graph_edges")
            before_changes = conn.total_changes
            before_rows = conn.execute("SELECT * FROM scratchpad_log").fetchall()
            before_counts = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables]
            out = router.recall("北斗七号")
            assert out["strategy"] == FALLBACK
            assert conn.total_changes == before_changes
            assert not conn.in_transaction
            assert conn.execute("SELECT * FROM scratchpad_log").fetchall() == before_rows
            assert [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables] == before_counts
        finally:
            conn.close()


def test_reproduces_field_queries_and_removes_no_match():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "field.db"))
        try:
            living = LivingStateManager(conn)
            sp = ScratchpadManager(conn)
            router = CognitiveRecallRouter(conn)
            living.update_decision("proj:e2e_probe", "探针项目", "对照组 定案")
            ids = [_add(sp, f"白泽验收 夭夭-美西01 E2E 事实{i}") for i in range(6)]

            control = router.recall("e2e_probe 对照组 定案")
            assert control["strategy"] == "DECISION_TEXT_MATCH"

            for query in ("白泽验收 夭夭-美西01 验证", "E2E"):
                out = router.recall(query)
                assert out["strategy"] == FALLBACK, (query, out)
                assert out["source_log_ids"] == ids[::-1][:5], (query, out)

            assert router.recall("E2E", top_k=3)["source_log_ids"] == ids[::-1][:3]
        finally:
            conn.close()


def test_like_wildcards_are_escaped():
    from baize.core.scratchpad import _escape_like

    assert _escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "like.db"))
        try:
            sp = ScratchpadManager(conn)
            wanted = _add(sp, "alpha_beta 配置")
            _add(sp, "alphaXbeta 配置")
            out = CognitiveRecallRouter(conn).recall("alpha_beta")
            assert out["strategy"] == FALLBACK
            assert out["source_log_ids"] == [wanted]
        finally:
            conn.close()


def test_superseded_fact_is_not_recalled():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            old = _add_http(tc, "旧阈值甲方案")
            new = _add_http(tc, "新阈值乙方案", supersedes_id=old)
            assert _recall(tc, "甲方案")["strategy"] == "NO_MATCH"
            out = _recall(tc, "乙方案")
            assert out["strategy"] == FALLBACK and out["source_log_ids"] == [new]


def test_no_match_when_nothing_matches():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _add_http(tc, "完全无关的内容")
            assert _recall(tc, "星际航行")["strategy"] == "NO_MATCH"


if __name__ == "__main__":
    test_fact_is_visible_before_consolidation_as_provisional()
    test_after_consolidation_the_living_card_answers_instead_of_fallback()
    test_living_card_hit_never_triggers_fallback()
    test_fallback_is_strictly_read_only()
    test_reproduces_field_queries_and_removes_no_match()
    test_like_wildcards_are_escaped()
    test_superseded_fact_is_not_recalled()
    test_no_match_when_nothing_matches()
    print("✅ explicit_addition 召回可见性测试全部通过")
