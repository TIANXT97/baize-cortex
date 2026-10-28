"""Issue #95 PR-1:锚点只认开头、问号规则去噪、名字匹配归卡、上线闸门 CORTEX_FACT_ROUTING、离线 --dry-run。

样例编号对应 #95 评论里的回归样例;卡名、别名和正文全部是虚构的合成文本,只复现各样例的特征:
ALPHA 对应「项目自身的记忆服务卡」,BETA 对应「带英文别名、与另一个服务联动的网关卡」。
"""
import contextlib
import functools
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

from baize.core import consolidator as consolidator_module
from baize.core.consolidator import DreamConsolidator, _is_authoritative
from baize.core.fact_router import (
    ROUTING_ENV, fact_routing_mode, is_exploratory, leading_anchor, parse_anchor, pick_winner, rank_entities,
)
from baize.core.living_state import LivingStateManager
from baize.core.normalization import idempotency_key
from baize.core.scratchpad import ScratchpadManager
from baize.core.supersession import supersede
from baize.intelligence.recall_router import CognitiveRecallRouter
from baize.storage.db import init_db

ALPHA = "proj:alpha_hub"
BETA = "proj:beta_router"
GENERAL = "proj:general_decisions"
MODES = ("log", "apply")


AMBIENT_ENV = (ROUTING_ENV, "CORTEX_LLM_URL", "CORTEX_LLM_KEY", "CORTEX_REASONING_MODEL", "CORTEX_BATCH_JUDGE")


@contextlib.contextmanager
def routing_mode(value):
    """清掉外部环境里会影响整理的变量(闸门、模型、批量判研),只按 value 设置闸门;退出时原样恢复。"""
    saved = {name: os.environ.get(name) for name in AMBIENT_ENV}
    try:
        for name in AMBIENT_ENV:
            os.environ.pop(name, None)
        if value is not None:
            os.environ[ROUTING_ENV] = value
        yield
    finally:
        for name, previous in saved.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous


def _isolated(test):
    @functools.wraps(test)
    def wrapper():
        with routing_mode(None):
            return test()
    return wrapper


class _QueueLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, system, user):
        self.calls.append((system, user))
        return self.responses.pop(0)


def _env(d, name="a.db", llm_client=None):
    conn = init_db(os.path.join(d, name))
    living = LivingStateManager(conn)
    living.update_decision(ALPHA, "阿尔法枢纽服务", "阿尔法现行定案", metadata={"aliases": ["AlphaHub", "阿尔法"]})
    living.update_decision(BETA, "贝塔路由网关", "贝塔现行定案", metadata={"aliases": ["ZetaPanel", "网关"]})
    consolidator = DreamConsolidator(conn, archive_dir=os.path.join(d, "arc"), llm_client=llm_client)
    return conn, ScratchpadManager(conn), living, consolidator


def _add(sp, text, session="s"):
    return sp.add_explicit(session, text, "fact", "r", idempotency_key(session, text), "default")


def _hot(conn):
    return [r[0] for r in conn.execute("SELECT id FROM scratchpad_log ORDER BY id")]


def _facts(living, entity_id):
    state = living.get_state(entity_id)
    return state["metadata"].get("facts", []) if state else []


def _item(report, log_id):
    return next(item for item in report["routing"]["items"] if item["id"] == log_id)


def _run(consolidator, mode, **kwargs):
    with routing_mode(mode):
        return consolidator.run_consolidation(**kwargs)


def _assert_name_routed(text, target, session_label):
    """无锚点、靠名字匹配到 target:log 只记 would_route 且留热表;apply 写卡、折叠、不碰 general。"""
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                lid = _add(sp, text)
                report = _run(consolidator, mode)
                item = _item(report, lid)
                assert item["via"] == "name" and item["target"] == target, (session_label, mode, item)
                assert living.get_state(GENERAL) is None, (session_label, mode)
                other = ALPHA if target == BETA else BETA
                assert _facts(living, other) == [], (session_label, mode)
                if mode == "log":
                    assert item["decision"] == "would_route", (session_label, item)
                    assert report["routing"]["mode"] == "log"
                    assert report["archived_logs"] == 0 and report["updated_states"] == []
                    assert _hot(conn) == [lid] and _facts(living, target) == []
                    assert [s["kind"] for s in report["skipped_explicit"]] == ["deferred"]
                else:
                    assert item["decision"] == "written", (session_label, item)
                    assert report["routing"]["mode"] == "apply"
                    assert report["archived_logs"] == 1 and report["updated_states"] == [target]
                    assert _hot(conn) == []
                    stored = _facts(living, target)
                    assert [f["id"] for f in stored] == [lid] and stored[0]["routed_by"] == "name"
            finally:
                conn.close()


# ---------- A 组:已经进了 general_decisions 的样例 ----------

def test_sample_517_incidental_reserved_name_is_not_an_anchor_and_name_routes():
    text = "复验结果:倒进 proj:general_decisions 的命中从 20 条降到 0 条。阿尔法枢纽服务 的召回分布前后对比如下。"
    assert leading_anchor(text) is None and parse_anchor(text) is None
    _assert_name_routed(text, ALPHA, "517")


def test_sample_522_anchorless_routes_by_name():
    _assert_name_routed("三个外部模型的现状:白天判定、模糊区复核、夜间模型。AlphaHub 的配置里只有夜间一项在用。", ALPHA, "522")


def test_sample_533_no_matching_card_stays_pending_never_general():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                lid = _add(sp, "上下文体积基线:新会话首轮约一万二千 token,技能索引三种写法实测对比。")
                report = _run(consolidator, mode)
                item = _item(report, lid)
                assert item["decision"] == "pending" and item["target"] is None, (mode, item)
                assert item["reason"] == "no_match" and item["scores"] == []
                assert report["archived_logs"] == 0 and report["updated_states"] == []
                assert [s["kind"] for s in report["skipped_explicit"]] == ["unrouted"]
                assert _hot(conn) == [lid]
                assert living.get_state(GENERAL) is None
                assert report["routing"]["summary"]["pending"] == 1
            finally:
                conn.close()


# ---------- B 组:靠别名 ZetaPanel 归 BETA ----------

def test_sample_544_deployment_fact_routes_to_beta_by_alias():
    _assert_name_routed(
        "ZetaPanel 在本机部署:服务、目录、升级方式。ZetaPanel 升级走脚本,AlphaHub 不受影响。", BETA, "544"
    )


def test_sample_550_panel_credential_fact_routes_to_beta_by_alias():
    _assert_name_routed("ZetaPanel 面板口令已改,与路由网关的对接收口到同一份配置。", BETA, "550")


def test_sample_559_close_scores_stay_pending_in_both_modes():
    """559 的真实特征:另一张卡也被反复提到,两张卡得分接近。PR-1 没有模型,不能硬归,留热表待分拣。"""
    text = (
        "ZetaPanel 端口发布后写操作被拒:ZetaPanel 的来源检查;AlphaHub 同机;AlphaHub 反向代理;"
        "ZetaPanel 修法;AlphaHub 重启。"
    )
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                lid = _add(sp, text)
                report = _run(consolidator, mode)
                item = _item(report, lid)
                assert item["decision"] == "pending" and item["reason"] == "ambiguous", (mode, item)
                assert [s[0] for s in item["scores"]] == [BETA, ALPHA]
                assert _hot(conn) == [lid] and living.get_state(GENERAL) is None
                assert _facts(living, ALPHA) == [] and _facts(living, BETA) == []
            finally:
                conn.close()


def test_sample_559_dominant_alias_still_routes_when_other_card_is_only_a_passing_mention():
    _assert_name_routed(
        "ZetaPanel 端口发布后写操作被拒,根因是来源检查;ZetaPanel 修法见下;AlphaHub 同机但不受影响。", BETA, "559-dominant"
    )


def test_sample_566_end_to_end_link_routes_to_beta_by_alias():
    _assert_name_routed(
        "ZetaPanel 与路由链路端到端打通:ZetaPanel 的模型出口、ZetaPanel 的凭据收口都已验证。", BETA, "566"
    )


# ---------- C 组:被问号规则误跳的样例 ----------

def test_sample_214_sql_placeholders_no_longer_skip_the_fact():
    text = "AlphaHub 召回收口修复上线:查询改成 WHERE session_id = ? AND created_at > ? 两层,服务重启后生效。"
    assert not is_exploratory(text)
    _assert_name_routed(text, ALPHA, "214")


def test_sample_547_url_query_parameters_no_longer_skip_the_fact():
    text = "ZetaPanel 对接配置:面板地址 https://panel.example/status?force=1 ,健康检查 /panel-status?host=&port= 返回空。"
    assert not is_exploratory(text)
    _assert_name_routed(text, BETA, "547")


def test_sample_573_bare_query_string_no_longer_skips_the_fact():
    text = "ZetaPanel 面板提示网关未连通的根因是探活地址 /health?host=&port= 写错,改正后恢复。"
    assert not is_exploratory(text)
    _assert_name_routed(text, BETA, "573")


# ---------- D 组:对照样例,行为不变 ----------

def test_sample_514_superseded_fact_keeps_being_skipped_in_both_modes():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                old = _add(sp, "AlphaHub 的复评记录:旧结论")
                new = _add(sp, "AlphaHub 的复评记录:新结论,推翻旧结论")
                supersede(conn, "scratchpad_log", new, old)
                report = _run(consolidator, mode)
                assert _item(report, old)["decision"] == "superseded"
                assert [s["id"] for s in report["skipped_explicit"] if s["kind"] == "superseded"] == [old]
                assert old in _hot(conn)
                assert old not in [f["id"] for f in _facts(living, ALPHA)]
                assert _item(report, new)["decision"] == ("written" if mode == "apply" else "would_route")
            finally:
                conn.close()


# ---------- 合成样例:疑问句、开头锚点、保留名、闸门 ----------

def test_real_questions_and_deliberation_are_still_skipped_in_both_modes():
    texts = [
        "要不要把阈值改成 5 ?", "AlphaHub 的阈值可以改成 5 吗？", "is AlphaHub ready?", "AlphaHub 要不要扩容呢",
        "如果 AlphaHub 超时就重试", "假设 AlphaHub 端口已发布", "探讨 AlphaHub 的方案", "AlphaHub 的改动尚未批准",
        "WHERE id = ? 这条查询对吗?",
    ]
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                ids = [_add(sp, text) for text in texts]
                report = _run(consolidator, mode)
                assert [s["id"] for s in report["skipped_explicit"]] == ids
                assert {s["kind"] for s in report["skipped_explicit"]} == {"exploratory"}
                assert all(_item(report, i)["decision"] == "exploratory" for i in ids)
                assert report["archived_logs"] == 0 and _hot(conn) == ids
                assert _facts(living, ALPHA) == [] and living.get_state(GENERAL) is None
            finally:
                conn.close()


def test_leading_anchor_writes_the_card_in_both_modes():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                a = _add(sp, "proj:alpha_hub 的回滚窗口是 30 分钟")
                b = _add(sp, "【proj:beta_router】的端口策略已定")
                c = _add(sp, "proj:AlphaHub 的别名写法也能当锚点")
                report = _run(consolidator, mode)
                assert [_item(report, i)["via"] for i in (a, b, c)] == ["anchor"] * 3
                assert [_item(report, i)["decision"] for i in (a, b, c)] == ["written"] * 3
                assert [f["id"] for f in _facts(living, ALPHA)] == [a, c]
                assert [f["id"] for f in _facts(living, BETA)] == [b]
                assert "routed_by" not in _facts(living, ALPHA)[0]
                assert _hot(conn) == [] and report["routing"]["summary"]["anchor"] == 3
            finally:
                conn.close()


def test_leading_reserved_anchor_is_rejected_and_never_written():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                lonely = _add(sp, "proj:general_decisions 开头的一条事实,没有任何卡名")
                named = _add(sp, "proj:general_decisions 开头但提到 AlphaHub 的事实")
                report = _run(consolidator, mode)
                assert _item(report, lonely)["decision"] == "pending"
                assert _item(report, lonely)["anchor"] == "reserved_anchor"
                assert _item(report, named)["target"] == ALPHA and _item(report, named)["via"] == "name"
                assert _item(report, named)["anchor"] == "reserved_anchor"
                assert living.get_state(GENERAL) is None
                assert lonely in _hot(conn)
            finally:
                conn.close()


def test_existing_general_card_is_frozen_and_not_a_routing_target():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                living.update_decision(GENERAL, GENERAL, "", metadata={"aliases": ["通用决策集合"], "facts": [{"id": 9001, "text": "旧事实"}]})
                before = living.get_state(GENERAL)
                lid = _add(sp, "通用决策集合 里的一条事实")
                anchored = _add(sp, "proj:general_decisions 的一条事实")
                report = _run(consolidator, mode)
                assert _item(report, lid)["decision"] == "pending" and _item(report, anchored)["decision"] == "pending"
                after = living.get_state(GENERAL)
                assert after["version"] == before["version"] and _facts(living, GENERAL) == before["metadata"]["facts"]
                assert set(_hot(conn)) == {lid, anchored}
            finally:
                conn.close()


def test_gate_defaults_to_log_and_only_the_literal_apply_enables_writes():
    with routing_mode(None):
        assert fact_routing_mode() == "log"
    for value in ("", "log", "Apply", "APPLY", " apply", "apply ", "true", "1", "on", "yes", "applied"):
        with routing_mode(value):
            assert fact_routing_mode() == "log", repr(value)
    with routing_mode("apply"):
        assert fact_routing_mode() == "apply"

    text = "ZetaPanel 的升级走脚本,ZetaPanel 的端口不变。"
    for value in (None, "Apply", "true", "applied"):
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                lid = _add(sp, text)
                report = _run(consolidator, value)
                assert report["routing"]["mode"] == "log" and _item(report, lid)["decision"] == "would_route"
                assert _hot(conn) == [lid] and _facts(living, BETA) == []
            finally:
                conn.close()


def test_second_apply_run_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, consolidator = _env(d)
        try:
            _add(sp, "ZetaPanel 的升级走脚本。")
            first = _run(consolidator, "apply")
            second = _run(consolidator, "apply")
            assert first["archived_logs"] == 1 and second["archived_logs"] == 0
            assert second["routing"]["items"] == [] and len(_facts(living, BETA)) == 1
        finally:
            conn.close()


# ---------- 名字匹配的边界 ----------

def test_short_aliases_and_ascii_word_boundaries_do_not_route():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                short = _add(sp, "网关配置变更,网关规则不变,阿尔法也提到了")
                embedded = _add(sp, "MyAlphaHubs 与 AlphaHubX 不是同一个名字")
                report = _run(consolidator, mode)
                assert _item(report, short)["decision"] == "pending" and _item(report, short)["scores"] == []
                assert _item(report, embedded)["decision"] == "pending"
                assert _hot(conn) == [short, embedded]
            finally:
                conn.close()


def test_hyphen_joined_ascii_name_and_case_and_fullwidth_still_match():
    ranked = rank_entities("ＡｌｐｈａＨｕｂ-extra 与 alphahub 的升级", {ALPHA: ["AlphaHub"], BETA: ["ZetaPanel"]})
    assert ranked == [(ALPHA, 16)] and pick_winner(ranked) == ALPHA


def test_generic_alias_blacklist_and_id_like_names_are_not_used():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                living.update_decision("proj:gamma_core", "proj:gamma_core", "", metadata={"aliases": ["记忆", "proj:gamma_core"]})
                lid = _add(sp, "记忆 记忆 记忆 proj:gamma_core 的内容中间提到")
                report = _run(consolidator, mode)
                assert _item(report, lid)["decision"] == "pending"
            finally:
                conn.close()


def test_pick_winner_thresholds():
    assert pick_winner([]) is None
    assert pick_winner([(ALPHA, 5)]) is None
    assert pick_winner([(ALPHA, 6)]) == ALPHA
    assert pick_winner([(ALPHA, 20), (BETA, 10)]) == ALPHA
    assert pick_winner([(ALPHA, 20), (BETA, 11)]) is None


# ---------- 候选路径 ----------

def _candidate(sp, text, category=None):
    row = sp.append_log("s", "user", text)
    sp.mark_judged(row, "candidate", 0.9, "batch_judge", category)
    sp.conn.commit()
    return row


def test_candidate_verdict_entity_writes_in_both_modes():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            llm = _QueueLLM([json.dumps({"is_fact": True, "entity": ALPHA}, ensure_ascii=False)])
            conn, sp, living, consolidator = _env(d, llm_client=llm)
            try:
                lid = _candidate(sp, "一条没有任何卡名的候选事实")
                report = _run(consolidator, mode)
                assert _item(report, lid)["via"] == "verdict" and _item(report, lid)["decision"] == "written"
                assert report["candidates"]["verified"] == 1 and _hot(conn) == []
                assert [f["id"] for f in _facts(living, ALPHA)] == [lid]
            finally:
                conn.close()


def test_candidate_name_match_is_recorded_in_log_and_written_in_apply():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            llm = _QueueLLM([json.dumps({"is_fact": True, "entity": ""}, ensure_ascii=False)])
            conn, sp, living, consolidator = _env(d, llm_client=llm)
            try:
                lid = _candidate(sp, "ZetaPanel 的升级走脚本,这是一条候选事实")
                report = _run(consolidator, mode)
                item = _item(report, lid)
                assert item["kind"] == "candidate" and item["via"] == "name" and item["target"] == BETA
                if mode == "log":
                    assert item["decision"] == "would_route"
                    assert report["candidates"]["verified"] == 0 and len(report["candidates"]["skipped"]) == 1
                    assert _hot(conn) == [lid] and _facts(living, BETA) == []
                else:
                    assert item["decision"] == "written" and report["candidates"]["verified"] == 1
                    assert _hot(conn) == [] and [f["id"] for f in _facts(living, BETA)] == [lid]
            finally:
                conn.close()


def test_candidate_with_reserved_verdict_entity_and_no_name_stays_pending():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            llm = _QueueLLM([json.dumps({"is_fact": True, "entity": GENERAL}, ensure_ascii=False)])
            conn, sp, living, consolidator = _env(d, llm_client=llm)
            try:
                living.update_decision(GENERAL, GENERAL, "")
                lid = _candidate(sp, "一条没有任何卡名的候选事实")
                report = _run(consolidator, mode)
                assert _item(report, lid)["decision"] == "pending" and _hot(conn) == [lid]
                assert _facts(living, GENERAL) == []
            finally:
                conn.close()


def test_rejected_candidates_are_still_archived_in_both_modes():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            llm = _QueueLLM([json.dumps({"is_fact": False, "entity": ""}, ensure_ascii=False)])
            conn, sp, living, consolidator = _env(d, llm_client=llm)
            try:
                _candidate(sp, "下月打算把测试环境迁到新机房")
                report = _run(consolidator, mode)
                assert report["archived_logs"] == 1 and len(report["candidates"]["rejected"]) == 1
                assert _hot(conn) == []
            finally:
                conn.close()


# ---------- 批准类记录(_analyze) ----------

def test_approval_without_leading_anchor_stays_hot_and_ignores_recent_card():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                before = living.get_state(ALPHA)["version"]
                mid = sp.append_log("s", "user", "我们决定 proj:alpha_hub 改为方案B", phase_hint="approval")
                bare = sp.append_log("s2", "user", "定案:方案C", phase_hint="approval")
                unknown_mid = sp.append_log("s3", "user", "结论是 proj:stranger 改为方案D", phase_hint="approval")
                report = _run(consolidator, mode)
                assert report["archived_logs"] == 0 and report["updated_states"] == []
                assert _hot(conn) == [mid, bare, unknown_mid]
                assert living.get_state(ALPHA)["version"] == before
                assert living.get_state("proj:stranger") is None and living.get_state(GENERAL) is None
            finally:
                conn.close()


def test_approval_still_inherits_the_session_leading_anchor_and_still_creates_card():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                sp.append_log("s", "user", "proj:alpha_hub 定案:方案B", phase_hint="approval")
                sp.append_log("s", "user", "就这么定", phase_hint="approval")
                sp.append_log("t", "user", "proj:fresh_card 定案:方案Z", phase_hint="approval")
                report = _run(consolidator, mode)
                assert sorted(report["updated_states"]) == [ALPHA, "proj:fresh_card"]
                assert living.get_state(ALPHA)["current_decision"] == "方案B"
                assert _hot(conn) == []
            finally:
                conn.close()


def test_authoritative_check_shares_the_same_question_rule():
    def approval(text):
        return {"role": "user", "phase_hint": "approval", "raw_text": text}

    assert _is_authoritative(approval("proj:a 定案:查询用 WHERE id = ? 取值"))
    assert _is_authoritative(approval("proj:a 定案:面板走 https://panel.example/s?force=1"))
    assert not _is_authoritative(approval("proj:a 定案:方案B?"))
    assert not _is_authoritative(approval("proj:a 如果定案:方案C"))
    assert not _is_authoritative({**approval("proj:a 定案:方案B"), "role": "assistant"})


# ---------- 纯函数表 ----------

def test_parse_anchor_table():
    cases = [
        ("proj:alpha 的回滚窗口", "proj:alpha"), ("[proj:alpha] x", "proj:alpha"), ("【proj:alpha】x", "proj:alpha"),
        ("`proj:alpha` 说明", "proj:alpha"), ("proj:alpha的回滚", "proj:alpha"), ("proj:alpha-x y", "proj:alpha-x"),
        ("proj：alpha 全角冒号", "proj:alpha"), ("(proj:alpha) x", "proj:alpha"),
        ("复验:倒进 proj:alpha 的命中", None), ("x proj:alpha", None), ("see proj:alpha", None),
        ("proj:general_decisions 开头", None), ("`proj:general_decisions` 开头", None), ("Proj:alpha", None), ("", None),
    ]
    assert [(text, parse_anchor(text)) for text, _ in cases] == cases


QUESTION_SKIP = [
    "要不要把阈值改成 5 ?", "这个方案可以吗？", "is this ok?", "能不能这样做呢",
    "查询 SELECT * FROM t WHERE id = ? 这样写对吗？", "访问 https://h.example/p?force=1 可以吗？",
    "阈值是 5?", "假设端口已发布", "如果超时则重试", "尚未批准的改动", "先 `a ? b : c` 再问：行吗?",
    "打开 https://h.example/p?x=1可以吗？", "试试 https://h.example/p?x=1 行不行?", "这样对吗?x=1 是什么意思",
    "WHERE id = ? 这条对吗?", "```\nSELECT 1\n```\n这样行吗?",
]
QUESTION_KEEP = [
    "SQL 用 WHERE id = ? AND name LIKE ? 取值", "INSERT INTO t VALUES (?, ?, ?) 已验证", "分页 LIMIT ? OFFSET ? 已改好",
    "接口 https://h.example/p?force=1 返回 200", "路径 /v1/admin/status?force=1 需要鉴权",
    "面板 /panel-status?host=&port= 返回空", "内联代码 `x ? y : z` 与 `a ?? b` 不是问句",
    "```\nSELECT 1 WHERE a = ?\n```\n上面是示例", "多参数 /q?a=1&b=2 与 /r?&c=3 都可用",
    "接口 https://h.example/p?x=1&y=2 返回 200", "见 http://h.example/q?a=1,已验证", "地址 https://h.example/p?force=1。",
    "UPDATE t SET a = ?, b = ? WHERE id = ?",
]


def test_question_rule_table():
    assert [t for t in QUESTION_SKIP if not is_exploratory(t)] == []
    assert [t for t in QUESTION_KEEP if is_exploratory(t)] == []


# ---------- 报告 ----------

def test_routing_report_is_present_on_every_return_branch():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, consolidator = _env(d)
        try:
            empty = _run(consolidator, None)
            assert empty["routing"] == {"mode": "log", "summary": empty["routing"]["summary"], "items": []}
            _add(sp, "ZetaPanel 的升级走脚本。")
            assert _run(consolidator, "apply", dry_run=True)["routing"]["mode"] == "apply"
            assert consolidator._run_mutex.acquire(blocking=False)
            try:
                busy = _run(consolidator, None)
                assert busy["status"] == "busy" and busy["routing"]["mode"] == "log"
            finally:
                consolidator._run_mutex.release()
        finally:
            conn.close()


def test_dry_run_writes_nothing_and_reports_would_write_or_would_route():
    for mode, expected in (("apply", "would_write"), ("log", "would_route")):
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                lid = _add(sp, "ZetaPanel 的升级走脚本。")
                anchored = _add(sp, "proj:alpha_hub 的锚点事实")
                versions = {e: living.get_state(e)["version"] for e in (ALPHA, BETA)}
                report = _run(consolidator, mode, dry_run=True)
                assert report["status"] == "dry_run" and _item(report, lid)["decision"] == expected
                assert _item(report, anchored)["decision"] == "would_write"
                assert _hot(conn) == [lid, anchored]
                assert {e: living.get_state(e)["version"] for e in (ALPHA, BETA)} == versions
                assert not os.path.exists(os.path.join(d, "arc"))
            finally:
                conn.close()


def test_stale_pending_rows_are_flagged_but_never_archived():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, consolidator = _env(d)
        try:
            old = _add(sp, "一条很久以前写的、匹配不到任何卡的事实")
            fresh = _add(sp, "今天写的、也匹配不到任何卡的事实")
            conn.execute("UPDATE scratchpad_log SET created_at = '2020-01-01 00:00:00' WHERE id = ?", (old,))
            conn.commit()
            report = _run(consolidator, "apply")
            assert _item(report, old)["stale"] is True and "stale" not in _item(report, fresh)
            assert report["routing"]["summary"]["stale"] == 1
            assert _hot(conn) == [old, fresh] and report["archived_logs"] == 0
        finally:
            conn.close()


def test_backlog_warning_and_preview_limits():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, consolidator = _env(d)
        try:
            ids = [_add(sp, f"第 {i} 条匹配不到任何卡的事实,正文足够长" + "字" * 100) for i in range(21)]
            plain = _run(consolidator, None)
            assert plain["routing"]["summary"]["pending"] == 21 and plain["routing"]["summary"]["backlog_warning"] is True
            assert all("preview" not in item for item in plain["routing"]["items"])
            short = _run(consolidator, None, preview_chars=10)
            assert all(len(item["preview"]) == 10 for item in short["routing"]["items"])
            capped = _run(consolidator, None, preview_chars=500)
            assert all(len(item["preview"]) == 80 for item in capped["routing"]["items"])
            assert _hot(conn) == ids
        finally:
            conn.close()


def test_card_changed_between_snapshot_and_commit_marks_item_deferred():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, consolidator = _env(d)
        try:
            lid = _add(sp, "proj:alpha_hub 的锚点事实")
            original = consolidator._take_snapshot

            def snapshot_then_change(target_date):
                snap = original(target_date)
                living.update_decision(ALPHA, "阿尔法枢纽服务", "中途改过的定案")
                return snap

            consolidator._take_snapshot = snapshot_then_change
            report = _run(consolidator, "apply")
            assert _item(report, lid)["decision"] == "deferred" and _item(report, lid)["reason"] == "card_changed"
            assert _hot(conn) == [lid] and _facts(living, ALPHA) == []
        finally:
            conn.close()


# ---------- 私人卡 / 群聊可见性 ----------

def test_routed_facts_never_leak_into_group_recall_and_visibility_is_kept():
    marker = "独特标记乙丙丁"
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, consolidator = _env(d)
        try:
            living.set_visibility(BETA, "group_ok")
            to_private = _add(sp, f"AlphaHub 的内部细节 {marker}甲")
            to_visible = _add(sp, f"ZetaPanel 的内部细节 {marker}乙")
            report = _run(consolidator, "apply")
            assert _item(report, to_private)["decision"] == "written" and _item(report, to_visible)["decision"] == "written"
            assert living.get_state(BETA)["metadata"]["visibility"] == "group_ok"
            assert "visibility" not in living.get_state(ALPHA)["metadata"]

            router = CognitiveRecallRouter(conn)
            for query in ("阿尔法枢纽服务", "AlphaHub 的内部细节", "贝塔路由网关", "ZetaPanel 的内部细节"):
                group = router.recall(query, scope="group")
                assert marker not in json.dumps(group, ensure_ascii=False), query
            private_view = router.recall("阿尔法枢纽服务", scope="group")
            assert ALPHA not in [s["entity_id"] for s in private_view.get("living_states", [])]
            assert ALPHA in private_view["withheld_entity_ids"]
            visible_view = router.recall("贝塔路由网关", scope="group")
            assert BETA in [s["entity_id"] for s in visible_view.get("living_states", [])]
        finally:
            conn.close()


# ---------- 命令行:离线 --dry-run ----------

def _cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = consolidator_module.main(argv)
    return code, out.getvalue(), err.getvalue()


def _dir_state(directory):
    state = {}
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                state[name] = (hashlib.sha256(fh.read()).hexdigest(), os.stat(path).st_mtime_ns)
        else:
            state[name] = "dir"
    return state


def _build_db_file(path):
    conn, sp, living, _ = None, None, None, None
    conn = init_db(path)
    try:
        living = LivingStateManager(conn)
        living.update_decision(ALPHA, "阿尔法枢纽服务", "阿尔法现行定案", metadata={"aliases": ["AlphaHub"]})
        living.update_decision(BETA, "贝塔路由网关", "贝塔现行定案", metadata={"aliases": ["ZetaPanel"]})
        sp = ScratchpadManager(conn)
        _add(sp, "proj:alpha_hub 的锚点事实")
        _add(sp, "ZetaPanel 的升级走脚本。")
        _add(sp, "匹配不到任何卡的事实")
        _add(sp, "要不要扩容?")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def test_cli_dry_run_leaves_every_file_byte_for_byte_unchanged():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "x.db")
            _build_db_file(db)
            assert sorted(os.listdir(d)) == ["x.db"]
            before = _dir_state(d)
            arc = os.path.join(d, "never", "arc")
            with routing_mode(mode):
                code, out, err = _cli(["--db", db, "--dry-run", "--archive-dir", arc, "--preview-chars", "8"])
            assert code == 0, err
            assert _dir_state(d) == before and sorted(os.listdir(d)) == ["x.db"]
            assert not os.path.exists(os.path.join(d, "never"))
            report = json.loads(out)
            assert report["status"] == "dry_run" and report["routing"]["mode"] == mode
            decisions = {item["kind"] + ":" + item["decision"] for item in report["routing"]["items"]}
            assert "explicit:exploratory" in decisions and "explicit:pending" in decisions
            assert "explicit:would_write" in decisions
            assert ("explicit:would_route" in decisions) == (mode == "log")
            assert all(len(item["preview"]) <= 8 for item in report["routing"]["items"])
            assert f"routing={mode}" in err and "mode=dry-run" in err and os.path.abspath(db) in err


def test_cli_dry_run_works_on_a_backup_copy_of_a_live_wal_database():
    with tempfile.TemporaryDirectory() as d:
        live_path, copy_path = os.path.join(d, "live.db"), os.path.join(d, "copy.db")
        live = init_db(live_path)
        try:
            LivingStateManager(live).update_decision(BETA, "贝塔路由网关", "贝塔现行定案", metadata={"aliases": ["ZetaPanel"]})
            _add(ScratchpadManager(live), "ZetaPanel 的升级走脚本。")
            assert os.path.getsize(live_path + "-wal") > 0
            source = sqlite3.connect(f"file:{live_path}?mode=ro", uri=True)
            target = sqlite3.connect(copy_path)
            source.backup(target)
            target.close()
            source.close()
        finally:
            live.close()
        before = _dir_state(d)
        code, out, err = _cli(["--db", copy_path, "--dry-run", "--archive-dir", os.path.join(d, "arc")])
        assert code == 0, err
        assert _dir_state(d) == before
        assert json.loads(out)["routing"]["summary"]["would_route"] == 1


def test_cli_refuses_databases_with_unmerged_wal_or_leftover_journal_without_touching_them():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "live.db")
        live = init_db(db)
        try:
            _add(ScratchpadManager(live), "一条事实")
            assert os.path.getsize(db + "-wal") > 0
            before = _dir_state(d)
            code, out, err = _cli(["--db", db, "--dry-run"])
            assert code == 2 and out == "" and "-wal" in err
            assert _dir_state(d) == before
        finally:
            live.close()
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "j.db")
        _build_db_file(db)
        with open(db + "-journal", "wb") as fh:
            fh.write(b"x")
        before = _dir_state(d)
        code, out, err = _cli(["--db", db, "--dry-run"])
        assert code == 2 and out == "" and _dir_state(d) == before


def test_cli_missing_database_exits_2_and_creates_nothing():
    with tempfile.TemporaryDirectory() as d:
        missing = os.path.join(d, "missing.db")
        for argv in (["--db", missing], ["--db", missing, "--dry-run"]):
            code, out, err = _cli(argv)
            assert code == 2 and out == "" and "不存在" in err
        assert os.listdir(d) == []
        db = os.path.join(d, "x.db")
        _build_db_file(db)
        code, out, err = _cli(["--db", db, "--preview-chars", "-1"])
        assert code == 2 and out == ""


def test_cli_no_llm_never_builds_the_client():
    calls = []
    original = consolidator_module.llm_client_from_env
    consolidator_module.llm_client_from_env = lambda: calls.append(1)
    try:
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "x.db")
            _build_db_file(db)
            assert _cli(["--db", db, "--dry-run", "--no-llm"])[0] == 0 and calls == []
            code, _, err = _cli(["--db", db, "--dry-run"])
            assert code == 0 and calls == [1] and "llm=none" in err
            code, _, err = _cli(["--db", db, "--dry-run", "--no-llm"])
            assert "llm=disabled" in err
    finally:
        consolidator_module.llm_client_from_env = original


def test_cli_default_run_still_executes_and_writes():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "x.db")
        _build_db_file(db)
        with routing_mode(None):
            code, out, err = _cli(["--db", db, "--archive-dir", os.path.join(d, "arc"), "--no-llm"])
        assert code == 0, err
        report = json.loads(out)
        assert report["status"] == "ok" and report["archived_logs"] == 1 and report["updated_states"] == [ALPHA]
        assert "mode=execute" in err and report["routing"]["summary"]["would_route"] == 1
        conn = init_db(db)
        try:
            hot = [r[0] for r in conn.execute("SELECT raw_text FROM scratchpad_log ORDER BY id")]
            assert len(hot) == 3 and "proj:alpha_hub 的锚点事实" not in hot
            assert LivingStateManager(conn).get_state(GENERAL) is None
        finally:
            conn.close()


def test_cli_subprocess_uses_only_the_env_it_is_given():
    """真起子进程跑 python -m:env 完全显式(不继承外部的 CORTEX_*),闸门值只由本用例决定。"""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = {k: v for k, v in os.environ.items() if not k.startswith("CORTEX_")}
    base["PYTHONPATH"] = repo
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "x.db")
        _build_db_file(db)
        before = _dir_state(d)
        results = {}
        for label, extra in (("unset", {}), ("apply", {ROUTING_ENV: "apply"}), ("garbage", {ROUTING_ENV: "Apply "})):
            env = {**base, **extra, "CORTEX_LLM_URL": "http://127.0.0.1:9/never", "CORTEX_LLM_KEY": "k"}
            done = subprocess.run(
                [sys.executable, "-m", "baize.core.consolidator", "--db", db, "--dry-run", "--no-llm"],
                env=env, capture_output=True, text=True, encoding="utf-8", cwd=d, timeout=120,
            )
            assert done.returncode == 0, done.stderr
            report = json.loads(done.stdout)
            results[label] = (report["routing"]["mode"], report["routing"]["summary"]["would_route"])
            assert "llm=disabled" in done.stderr
        assert results == {"unset": ("log", 1), "apply": ("apply", 0), "garbage": ("log", 1)}, results
        assert _dir_state(d) == before and sorted(os.listdir(d)) == ["x.db"]


def test_isolation_clears_ambient_env_and_restores_it():
    ambient = {
        ROUTING_ENV: "apply", "CORTEX_LLM_URL": "http://127.0.0.1:9/x", "CORTEX_LLM_KEY": "k",
        "CORTEX_REASONING_MODEL": "m", "CORTEX_BATCH_JUDGE": "on",
    }
    assert [name for name in AMBIENT_ENV if name in os.environ] == []
    os.environ.update(ambient)
    try:
        with routing_mode(None):
            assert [name for name in AMBIENT_ENV if name in os.environ] == []
            assert fact_routing_mode() == "log"
            with routing_mode("apply"):
                assert fact_routing_mode() == "apply" and "CORTEX_LLM_URL" not in os.environ
            assert ROUTING_ENV not in os.environ
        assert {name: os.environ.get(name) for name in AMBIENT_ENV} == ambient
    finally:
        for name in ambient:
            os.environ.pop(name, None)


# 每个用例都在清掉外部闸门/模型/批量判研变量的环境里运行;需要某个闸门值的用例在里面再用 routing_mode(...)
for _name, _test in list(globals().items()):
    if _name.startswith("test_") and callable(_test):
        globals()[_name] = _isolated(_test)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"✅ Issue #95 锚点与归卡测试全部通过({len(tests)} 项)")
