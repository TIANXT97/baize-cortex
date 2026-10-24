"""Issue #44 C:DECISION_TEXT_MATCH 命中门槛 —— 单个 2-gram 命中卡名/别名即放行

根因:无标点中文整句只切出一个原词 token,句中唯一的卡名 2-gram 命中不足以满足
「≥2 个 2-gram」,整卡被刷掉。放行条件收紧为:单 gram 必须命中 display_name 或
alias;仅命中正文(decision/facts)的单 gram 仍被拦截,防通用词噪声。
"""
import os
import tempfile

from baize.core.living_state import LivingStateManager
from baize.intelligence.recall_router import CognitiveRecallRouter
from baize.storage.db import init_db

REPRO_QUERY = "你现在用得卡片是洋哥仓库最新的吗"


def _put(conn, entity_id, name, decision, aliases=None):
    metadata = {"aliases": aliases} if aliases else None
    LivingStateManager(conn).update_decision(entity_id, name, decision, metadata=metadata)


def _env(d):
    conn = init_db(os.path.join(d, "s.db"))
    return conn, CognitiveRecallRouter(conn)


def _ids(out):
    return [s["entity_id"] for s in out["living_states"]]


def test_single_gram_hitting_card_name_qualifies():
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:feishu_card", "流式卡片", "飞书消息用卡片展示更新")
            out = router.recall(REPRO_QUERY)
            assert out["strategy"] == "DECISION_TEXT_MATCH", out
            assert out["state"]["entity_id"] == "proj:feishu_card", out
            assert _ids(out) == ["proj:feishu_card"]
        finally:
            conn.close()


def test_single_gram_hitting_alias_qualifies():
    with tempfile.TemporaryDirectory() as d:
        conn, router = _env(d)
        try:
            _put(conn, "proj:mirror", "镜像站项目", "站点运维记录", aliases=["洋哥镜像"])
            out = router.recall(REPRO_QUERY)
            assert out["strategy"] == "DECISION_TEXT_MATCH", out
            assert out["state"]["entity_id"] == "proj:mirror", out
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


if __name__ == "__main__":
    test_single_gram_hitting_card_name_qualifies()
    test_single_gram_hitting_alias_qualifies()
    test_single_gram_hitting_body_only_still_blocked()
    test_two_gram_body_hits_still_qualify()
    print("✅ 召回单 2-gram 卡名/别名放行门槛测试全部通过")
