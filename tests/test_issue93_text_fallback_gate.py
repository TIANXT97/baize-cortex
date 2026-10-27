"""Issue #93:文本兜底(DECISION_TEXT_MATCH)的准入收紧——无名大卡不能靠泛词碎片撑高命中率

只用合成数据。规则:
- 通用词(代词、连接词、助动词、高频虚化名词)不计入准入证据;
- 卡名为 id 本身的「无名卡」,名字不算准入依据;
- facts 只能靠单条 fact 内足够多的非通用命中放行(命名卡 2 分、无名卡 3 分),跨条拼凑不算,
  在这张卡自己的 facts 里过于普遍的词不算,且只给封顶的加分;
- 卡名、别名、现行决策里的非通用整词/双 2-gram 命中照常放行。
"""
import os
import tempfile

from baize.core.living_state import LivingStateManager
from baize.intelligence.recall_router import CognitiveRecallRouter, _is_generic_token
from baize.storage.db import init_db

BIG_FACTS = [
    "调度器每晚两点执行归档任务并记录耗时", "网关超时时间调整为三十秒以避免长请求被截断", "备份脚本把数据库快照写入冷存储目录",
    "验收记录:接口回归测试全部通过没有失败项", "升级后需要重启服务并检查健康接口返回状态", "配置文件中的令牌项必须放在环境变量里不能写进仓库",
    "测试环境的端口范围固定为一组示例端口", "监控面板每五分钟刷新一次指标", "日志轮转保留最近七份压缩文件", "证书到期前十四天开始提醒续期",
    "并发上限设置为八个工作线程", "缓存预热脚本在发布后自动运行", "回滚方案是切换到上一个稳定版本", "数据迁移前先做一次完整校验",
    "告警规则把连续三次失败视为严重级别", "镜像构建使用固定基础版本避免漂移", "文档目录下的说明文件由维护者统一更新",
]
GENERIC_QUERIES = [
    "我们需要检查一下是不是可以了", "目前设置没有问题可以继续使用", "现在的情况需要重启一下看看",
    "这次测试没有通过是不是有问题", "不能这样设置一下再看看结果", "已经检查过了没有失败的情况",
]
SPECIFIC_QUERY = "网关超时时间是多少"


def _env(d):
    conn = init_db(os.path.join(d, "s.db"))
    return conn, CognitiveRecallRouter(conn), LivingStateManager(conn)


def _facts(texts):
    return {"facts": [{"id": i, "text": t} for i, t in enumerate(texts, 1)]}


def _ids(out):
    return [s["entity_id"] for s in out["living_states"] if not s.get("provisional")]


def test_generic_token_rule_is_about_language_not_topics():
    for generic in ("这个", "没有", "问题", "现在", "可以", "这个问题", "没有问题", "the", "不是"):
        assert _is_generic_token(generic), generic
    for specific in ("网关", "超时", "网关超时", "备份脚本", "示例仓库", "0b8b352", "Cortex"):
        assert not _is_generic_token(specific), specific


def test_unlabeled_big_card_is_not_admitted_by_generic_fragments():
    with tempfile.TemporaryDirectory() as d:
        conn, router, living = _env(d)
        try:
            living.update_decision("proj:misc_notes", "proj:misc_notes", "", metadata=_facts(BIG_FACTS))
            for query in GENERIC_QUERIES:
                out = router.recall(query)
                assert out["strategy"] == "NO_MATCH" and _ids(out) == [], (query, out["strategy"])
        finally:
            conn.close()


def test_unlabeled_big_card_still_answers_when_one_fact_matches_distinctively():
    with tempfile.TemporaryDirectory() as d:
        conn, router, living = _env(d)
        try:
            living.update_decision("proj:misc_notes", "proj:misc_notes", "", metadata=_facts(BIG_FACTS))
            out = router.recall(SPECIFIC_QUERY)
            assert out["strategy"] == "DECISION_TEXT_MATCH", out["strategy"]
            assert _ids(out) == ["proj:misc_notes"]
        finally:
            conn.close()


def test_named_card_hits_on_name_alias_and_decision_are_unchanged():
    with tempfile.TemporaryDirectory() as d:
        conn, router, living = _env(d)
        try:
            living.update_decision(
                "proj:gateway", "示例网关", "超时时间统一设置为三十秒", metadata={"aliases": ["入口代理"], **_facts(BIG_FACTS)},
            )
            by_name = router.recall("示例网关现在是什么状态")
            assert by_name["strategy"] == "LIVING_STATE_DIRECT" and _ids(by_name) == ["proj:gateway"]
            by_alias = router.recall("入口代理的情况")
            assert by_alias["strategy"] == "LIVING_STATE_DIRECT" and _ids(by_alias) == ["proj:gateway"]
            by_decision = router.recall("超时时间统一")
            assert by_decision["strategy"] == "DECISION_TEXT_MATCH" and _ids(by_decision) == ["proj:gateway"]
        finally:
            conn.close()


def test_named_big_card_is_not_admitted_by_generic_fragments_either():
    with tempfile.TemporaryDirectory() as d:
        conn, router, living = _env(d)
        try:
            living.update_decision("proj:notes", "杂项备忘", "", metadata=_facts(BIG_FACTS))
            for query in GENERIC_QUERIES:
                assert router.recall(query)["strategy"] == "NO_MATCH", query
        finally:
            conn.close()


def test_facts_evidence_must_come_from_a_single_fact():
    with tempfile.TemporaryDirectory() as d:
        conn, router, living = _env(d)
        try:
            # 「曙光」只在第一条、「星河」只在第二条:拼起来有两个 2-gram,但没有哪一条 fact 同时含有两者
            living.update_decision(
                "proj:split", "拆分样本", "", metadata=_facts(["曙光计划的负责人是维护者", "星河任务的排期已经确认"]),
            )
            assert router.recall("曙光星河")["strategy"] == "NO_MATCH"
            same_fact = router.recall("曙光计划")
            assert same_fact["strategy"] == "DECISION_TEXT_MATCH" and _ids(same_fact) == ["proj:split"]
        finally:
            conn.close()


def test_overlapping_grams_count_as_one_piece_of_evidence():
    with tempfile.TemporaryDirectory() as d:
        conn, router, living = _env(d)
        try:
            living.update_decision("proj:notes", "杂项备忘", "", metadata=_facts(BIG_FACTS))
            # 「需要重启」产生的「要重」「重启」共用一个字,只算一份证据,不够放行
            assert router.recall("现在的情况需要重启一下看看")["strategy"] == "NO_MATCH"
            # 两处互不重叠的实义片段(「重启」「服务」)才够放行
            out = router.recall("重启服务")
            assert out["strategy"] == "DECISION_TEXT_MATCH" and _ids(out) == ["proj:notes"]
        finally:
            conn.close()


def test_terms_common_inside_the_cards_own_facts_do_not_count():
    with tempfile.TemporaryDirectory() as d:
        conn, router, living = _env(d)
        try:
            facts = [f"通用标记 条目{n}的普通说明" for n in "甲乙丙丁戊己"] + ["通用标记 独特名词甲乙需要单独核对"]
            living.update_decision("proj:common", "共性样本", "", metadata=_facts(facts))
            assert router.recall("通用标记")["strategy"] == "NO_MATCH"
            out = router.recall("独特名词甲乙")
            assert out["strategy"] == "DECISION_TEXT_MATCH" and _ids(out) == ["proj:common"]
        finally:
            conn.close()


def test_facts_give_a_capped_bonus_so_volume_cannot_win_ranking():
    with tempfile.TemporaryDirectory() as d:
        conn, router, living = _env(d)
        try:
            living.update_decision("proj:big", "大样本", "", metadata=_facts(BIG_FACTS))
            living.update_decision("proj:small", "小样本", "网关超时时间统一设置为三十秒")
            out = router.recall(SPECIFIC_QUERY)
            assert out["strategy"] == "DECISION_TEXT_MATCH"
            scores = {s["entity_id"]: s["score"] for s in out["living_states"]}
            assert scores["proj:small"] > scores["proj:big"], scores
            assert _ids(out)[0] == "proj:small"
        finally:
            conn.close()


def test_group_view_is_not_wider():
    with tempfile.TemporaryDirectory() as d:
        conn, router, living = _env(d)
        try:
            living.update_decision("proj:misc_notes", "proj:misc_notes", "", metadata=_facts(BIG_FACTS))
            living.update_decision("proj:open", "公开样本", "公开说明统一设置为三十秒", metadata=_facts(BIG_FACTS))
            living.set_visibility("proj:open", "group_ok")
            for query in GENERIC_QUERIES + [SPECIFIC_QUERY]:
                group = router.recall(query, scope="group")
                assert group["scope"] == "group"
                assert set(_ids(group)) <= {"proj:open"}, (query, _ids(group))
                assert "proj:misc_notes" not in str(group.get("living_states")), query
                assert not group.get("provisional_facts") and not group.get("facts")
                for card in group["living_states"]:
                    assert "facts" not in card["metadata"], "群聊视图仍不带 facts"
            # 私人卡只在真正够格命中时才进「被拦下」的记录,泛词闲聊不再误报
            assert router.recall(GENERIC_QUERIES[0], scope="group")["withheld_entity_ids"] == []
            assert router.recall(SPECIFIC_QUERY, scope="group")["withheld_entity_ids"] == ["proj:misc_notes"]
            # 群可见卡靠现行决策的命中照常
            hit = router.recall("公开说明统一", scope="group")
            assert hit["strategy"] == "DECISION_TEXT_MATCH" and _ids(hit) == ["proj:open"]
        finally:
            conn.close()


if __name__ == "__main__":
    test_generic_token_rule_is_about_language_not_topics()
    test_unlabeled_big_card_is_not_admitted_by_generic_fragments()
    test_unlabeled_big_card_still_answers_when_one_fact_matches_distinctively()
    test_named_card_hits_on_name_alias_and_decision_are_unchanged()
    test_named_big_card_is_not_admitted_by_generic_fragments_either()
    test_facts_evidence_must_come_from_a_single_fact()
    test_overlapping_grams_count_as_one_piece_of_evidence()
    test_terms_common_inside_the_cards_own_facts_do_not_count()
    test_facts_give_a_capped_bonus_so_volume_cannot_win_ranking()
    test_group_view_is_not_wider()
    print("✅ Issue #93 文本兜底准入收紧测试全部通过")
