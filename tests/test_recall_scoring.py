"""Issue #32 遗留 1:2-gram 分词、加权打分 Top-K、时效排序与 hit_count / last_recalled_at 回写"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

from baize.core.living_state import LivingStateManager
from baize.core.seeds import seed_initial_assets
from baize.intelligence.recall_router import CognitiveRecallRouter, _scoring_tokens
from baize.storage.db import init_db

MOLD_DECISION = "整页切片 + 飞桨描述强耦合 + 飞书折叠抽屉按需出图"


def _ts(days_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _put(conn, entity_id, name, decision, aliases=None, facts=None, days_ago=0.0):
    metadata = {}
    if aliases:
        metadata["aliases"] = aliases
    if facts:
        metadata["facts"] = [{"id": i, "text": text} for i, text in enumerate(facts, 1)]
    LivingStateManager(conn).update_decision(entity_id, name, decision, metadata=metadata or None)
    conn.execute("UPDATE living_states SET updated_at = ? WHERE entity_id = ?", (_ts(days_ago), entity_id))
    conn.commit()


def _env(d):
    conn = init_db(os.path.join(d, "s.db"))
    return conn, CognitiveRecallRouter(conn)


def _ids(out):
    return [s["entity_id"] for s in out["living_states"]]


def _hits(conn, entity_id):
    return conn.execute(
        "SELECT hit_count, last_recalled_at FROM living_states WHERE entity_id = ?", (entity_id,)
    ).fetchone()


def test_tokenizer_uses_original_words_plus_2grams():
    assert _scoring_tokens("飞书卡片") == ["飞书卡片", "飞书", "书卡", "卡片"]
    assert _scoring_tokens("白泽 Cortex 0b8b352") == ["白泽", "Cortex", "0b8b352"]
    assert _scoring_tokens("e2e探针项目") == ["e2e探针项目", "探针", "针项", "项目"]
    assert _scoring_tokens("飞书 飞书") == ["飞书"]
    assert _scoring_tokens("飞 书") == []


def test_chinese_query_without_spaces_hits_living_card():
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            seed_initial_assets(conn)
            _put(conn, "proj:feishu_streaming", "飞书流式卡片", "飞书消息流式输出,用卡片 patch 更新")
            out = router.recall("飞书卡片")
            assert out["strategy"] == "DECISION_TEXT_MATCH", out
            assert out["state"]["entity_id"] == "proj:feishu_streaming"
            assert _ids(out)[0] == "proj:feishu_streaming"
        finally:
            conn.close()


def test_spaced_query_prefers_stronger_match_over_first_row():
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:a_mold", "示例工程知识库", MOLD_DECISION, aliases=["示例知识库", "示例别名"])
            _put(conn, "proj:z_feishu_streaming", "飞书流式卡片", "飞书消息流式输出,用卡片 patch 更新")
            out = router.recall("飞书 流式")
            assert out["state"]["entity_id"] == "proj:z_feishu_streaming", out
            assert _ids(out) == ["proj:z_feishu_streaming", "proj:a_mold"]
            assert out["living_states"][0]["score"] > out["living_states"][1]["score"]
        finally:
            conn.close()


def test_same_name_entities_return_most_recently_updated_first():
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:alpha_a_old", "同名项目", "旧方案", days_ago=8)
            _put(conn, "proj:alpha_b_new", "同名项目", "新方案", days_ago=1)
            direct = router.recall("同名项目")
            assert direct["strategy"] == "LIVING_STATE_DIRECT"
            assert direct["state"]["entity_id"] == "proj:alpha_b_new"
            assert _ids(direct) == ["proj:alpha_b_new", "proj:alpha_a_old"]

            _put(conn, "proj:p_old", "甲项目", "方案细节说明", days_ago=10)
            _put(conn, "proj:q_new", "乙项目", "方案细节说明", days_ago=1)
            loose = router.recall("方案细节")
            assert loose["strategy"] == "DECISION_TEXT_MATCH"
            assert loose["state"]["entity_id"] == "proj:q_new"
            assert _ids(loose) == ["proj:q_new", "proj:p_old"]
        finally:
            conn.close()


def test_score_formula_weights_and_decay_floor():
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:x", "飞书流式卡片", "流式输出方案")
            _put(conn, "proj:y", "其他项目", "飞书消息使用卡片展示")
            out = router.recall("飞书卡片")
            scores = {s["entity_id"]: s["score"] for s in out["living_states"]}
            assert abs(scores["proj:x"] - 8.0) < 0.05, scores
            assert abs(scores["proj:y"] - 6.0) < 0.05, scores
            assert _ids(out) == ["proj:x", "proj:y"]

            _put(conn, "proj:m_40d", "丙项目", "衰减测试词条", days_ago=40)
            _put(conn, "proj:n_100d", "丁项目", "衰减测试词条", days_ago=100)
            old = router.recall("衰减测试")
            old_scores = {s["entity_id"]: s["score"] for s in old["living_states"]}
            assert old_scores["proj:m_40d"] == old_scores["proj:n_100d"]
            assert old_scores["proj:m_40d"] >= 0
            assert _ids(old) == ["proj:m_40d", "proj:n_100d"]
        finally:
            conn.close()


def test_top_k_is_respected_and_ordered_by_score():
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:k1", "排序样本组", "召回排序样本 强相关 排序样本")
            _put(conn, "proj:k2", "己项目", "召回排序样本")
            _put(conn, "proj:k3", "庚项目", "仅含排序")
            full = router.recall("召回排序样本", top_k=5)
            assert _ids(full)[:2] == ["proj:k1", "proj:k2"]
            scores = [s["score"] for s in full["living_states"]]
            assert scores == sorted(scores, reverse=True)

            assert len(router.recall("召回排序样本", top_k=2)["living_states"]) == 2
            assert len(router.recall("召回排序样本", top_k=1)["living_states"]) == 1
            assert len(router.recall("召回排序样本", top_k=0)["living_states"]) == 1
            assert len(router.recall("召回排序样本", top_k=-3)["living_states"]) == 1
        finally:
            conn.close()


def test_hit_count_and_last_recalled_at_are_written_for_returned_cards_only():
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:hit", "命中项目", "独有词条甲乙丙")
            _put(conn, "proj:miss", "未中项目", "毫无关联的内容")
            updated_before = conn.execute("SELECT updated_at FROM living_states WHERE entity_id = 'proj:hit'").fetchone()[0]
            assert _hits(conn, "proj:hit") == (0, None)

            router.recall("独有词条甲乙丙")
            count, last = _hits(conn, "proj:hit")
            assert count == 1 and last and len(last) == 19 and "T" not in last
            router.recall("命中项目")
            assert _hits(conn, "proj:hit")[0] == 2
            assert _hits(conn, "proj:miss") == (0, None)
            assert conn.execute(
                "SELECT updated_at FROM living_states WHERE entity_id = 'proj:hit'").fetchone()[0] == updated_before
            assert not conn.in_transaction
        finally:
            conn.close()


def test_no_match_and_scratchpad_fallback_write_nothing():
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:hit", "命中项目", "独有词条甲乙丙")
            before = conn.total_changes
            assert router.recall("星际航行指南")["strategy"] == "NO_MATCH"
            assert conn.total_changes == before
            assert _hits(conn, "proj:hit") == (0, None)
        finally:
            conn.close()


def test_hit_writeback_failure_is_fail_open():
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:hit", "命中项目", "独有词条甲乙丙")
            conn.execute(
                "CREATE TRIGGER block_hits BEFORE UPDATE OF hit_count ON living_states "
                "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )
            conn.commit()
            out = router.recall("命中项目")
            assert out["strategy"] == "LIVING_STATE_DIRECT" and out["state"]["entity_id"] == "proj:hit"
            assert not conn.in_transaction
            assert _hits(conn, "proj:hit") == (0, None)
        finally:
            conn.close()


if __name__ == "__main__":
    test_tokenizer_uses_original_words_plus_2grams()
    test_chinese_query_without_spaces_hits_living_card()
    test_spaced_query_prefers_stronger_match_over_first_row()
    test_same_name_entities_return_most_recently_updated_first()
    test_score_formula_weights_and_decay_floor()
    test_top_k_is_respected_and_ordered_by_score()
    test_hit_count_and_last_recalled_at_are_written_for_returned_cards_only()
    test_no_match_and_scratchpad_fallback_write_nothing()
    test_hit_writeback_failure_is_fail_open()
    print("✅ 召回打分与时效排序测试全部通过")
