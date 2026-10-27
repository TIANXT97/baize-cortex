"""Issue #44 CTO 裁决红线 3:召回门槛禁止名称 2-gram 碎片放行

PR #45 曾按原单提议放宽为「单 2-gram 命中卡名/别名即放行」,CTO 会诊指出反例:卡名
「白泽记忆」内嵌高频词「记忆」,任意一句闲聊都能把该卡强行捞出。本文件锁定纠偏后
的语义:门槛保持两规则(原词命中 / ≥2 个不同 2-gram),全词命中由 LIVING_STATE_DIRECT
的 name/alias 子串匹配承担,长句召回靠注册具体别名解决。
"""
import os
import tempfile

from baize.core.living_state import LivingStateManager
from baize.intelligence.recall_router import CognitiveRecallRouter
from baize.storage.db import init_db

REPRO_QUERY = "当前使用的卡片是示例仓库的最新版吗"


def _put(conn, entity_id, name, decision, aliases=None):
    metadata = {"aliases": aliases} if aliases else None
    LivingStateManager(conn).update_decision(entity_id, name, decision, metadata=metadata)


def _env(d):
    conn = init_db(os.path.join(d, "s.db"))
    return conn, CognitiveRecallRouter(conn)


def _ids(out):
    return [s["entity_id"] for s in out["living_states"]]


def test_high_freq_gram_inside_card_name_does_not_qualify():
    # CTO 红线反例:「记忆」是「白泽记忆」的 2-gram 碎片,纯闲聊不得把该卡捞出
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:baize_memory", "白泽记忆", "白泽项目的记忆中枢架构")
            out = router.recall("随便聊聊今天记忆里有什么")
            assert out["strategy"] == "NO_MATCH", out
            assert out["living_states"] == []
        finally:
            conn.close()


def test_single_gram_hitting_body_only_still_blocked():
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:schedule", "甲乙丙丁项目", "时间管理规范")
            out = router.recall("现在时间怎么样")
            assert out["strategy"] == "NO_MATCH", out
            assert out["living_states"] == []
        finally:
            conn.close()


def test_two_gram_body_hits_still_qualify():
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:dimension", "戊项目", "时间管理与空间规划")
            out = router.recall("时间空间")
            assert out["strategy"] == "DECISION_TEXT_MATCH", out
            assert out["state"]["entity_id"] == "proj:dimension", out
        finally:
            conn.close()


def test_full_alias_word_in_long_query_hits_direct():
    # 长句召回的正路径:具体别名(非通用词)整词出现在 query 中 → LIVING_STATE_DIRECT
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:feishu_card", "流式卡片", "飞书消息用卡片展示更新", aliases=["示例仓库"])
            out = router.recall(REPRO_QUERY)
            assert out["strategy"] == "LIVING_STATE_DIRECT", out
            assert out["state"]["entity_id"] == "proj:feishu_card", out
        finally:
            conn.close()


def test_full_name_word_in_long_query_hits_direct():
    # 全名整词出现在长句中 → 直达层命中(原有行为,回归保护)
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:feishu_card", "流式卡片", "飞书消息用卡片展示更新")
            out = router.recall("我们现在用的流式卡片是不是该升级了")
            assert out["strategy"] == "LIVING_STATE_DIRECT", out
            assert out["state"]["entity_id"] == "proj:feishu_card", out
        finally:
            conn.close()


if __name__ == "__main__":
    test_high_freq_gram_inside_card_name_does_not_qualify()
    test_single_gram_hitting_body_only_still_blocked()
    test_two_gram_body_hits_still_qualify()
    test_full_alias_word_in_long_query_hits_direct()
    test_full_name_word_in_long_query_hits_direct()
    print("✅ 召回门槛红线纠偏测试全部通过")
