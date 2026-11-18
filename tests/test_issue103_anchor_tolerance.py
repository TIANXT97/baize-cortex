"""Issue #103:锚点写法容错(归一比对、中文别名、锚点后紧跟中文正文)、锚点对不上或对多张卡时不猜、
夜间报告里的建卡建议、带有效锚点的事实不吃探讨规则、引号与括号里的问号不算。卡名和正文全部是虚构的合成文本。
"""
import contextlib
import functools
import json
import os
import tempfile

from baize.core.consolidator import DreamConsolidator
from baize.core.fact_router import ROUTING_ENV, anchor_map_of, is_exploratory, resolve_anchor
from baize.core.living_state import LivingStateManager
from baize.core.normalization import idempotency_key
from baize.core.scratchpad import CANDIDATE_HINT, ScratchpadManager
from baize.storage.db import init_db

ALPHA = "proj:alpha_hub"
BETA = "proj:beta_router"
CLOUD = "proj:lumen_cloud"
DELTA = "proj:delta_node"
ONE = "proj:gamma_one"
TWO = "proj:gamma_two"
MODES = ("log", "apply")

AMBIENT_ENV = (ROUTING_ENV, "CORTEX_LLM_URL", "CORTEX_LLM_KEY", "CORTEX_REASONING_MODEL", "CORTEX_BATCH_JUDGE")


@contextlib.contextmanager
def routing_mode(value):
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
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def chat(self, system, user):
        self.calls.append((system, user))
        return self.responses.pop(0)


def _env(d, llm=None):
    conn = init_db(os.path.join(d, "a.db"))
    living = LivingStateManager(conn)
    living.update_decision(ALPHA, "阿尔法枢纽服务", "阿尔法现行定案", metadata={"aliases": ["AlphaHub", "阿尔法"]})
    living.update_decision(BETA, "贝塔路由网关", "贝塔现行定案", metadata={"aliases": ["ZetaPanel", "网关"]})
    living.update_decision(CLOUD, "云枢拓扑", "云枢现行定案", metadata={"aliases": ["云枢"]})
    living.update_decision(DELTA, "德尔塔节点", "德尔塔现行定案", metadata={"aliases": ["Delta"]})
    living.update_decision(ONE, "伽马一号", "伽马一现行定案", metadata={"aliases": ["Shared"]})
    living.update_decision(TWO, "伽马二号", "伽马二现行定案", metadata={"aliases": ["shared"]})
    consolidator = DreamConsolidator(conn, archive_dir=os.path.join(d, "arc"), llm_client=llm)
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


# ---------- 锚点归一比对(纯函数) ----------

def _amap():
    return anchor_map_of([
        (ALPHA, ["AlphaHub", "阿尔法"]), (BETA, ["ZetaPanel", "网关"]), (CLOUD, ["云枢"]),
        (DELTA, ["Delta"]), (ONE, ["Shared"]), (TWO, ["shared"]), ("proj:general_decisions", ["通用决策"]),
    ])


@_isolated
def test_anchor_resolution_is_case_dash_and_underscore_insensitive():
    amap = _amap()
    assert resolve_anchor("alpha_hub", amap) == ("matched", ALPHA)
    assert resolve_anchor("alpha-hub", amap) == ("matched", ALPHA)
    assert resolve_anchor("Alpha_Hub", amap) == ("matched", ALPHA)
    assert resolve_anchor("ALPHA-HUB", amap) == ("matched", ALPHA)
    assert resolve_anchor("AlphaHub", amap) == ("matched", ALPHA)
    assert resolve_anchor("alphahub", amap) == ("matched", ALPHA), "casefolded alias AlphaHub"
    assert resolve_anchor("alpha_hub_x", amap) == ("unknown", None)
    assert resolve_anchor("ZETAPANEL", amap) == ("matched", BETA)


@_isolated
def test_anchor_resolution_chinese_alias_and_glued_text():
    amap = _amap()
    assert resolve_anchor("云枢", amap) == ("matched", CLOUD)
    assert resolve_anchor("云枢拓扑", amap) == ("matched", CLOUD)
    assert resolve_anchor("阿尔法", amap) == ("matched", ALPHA)
    assert resolve_anchor("delta本尊拓扑", amap) == ("matched", DELTA)
    assert resolve_anchor("beta_router网关拓扑", amap) == ("matched", BETA)


@_isolated
def test_anchor_resolution_unknown_ambiguous_and_reserved():
    amap = _amap()
    assert resolve_anchor("tailscale", amap) == ("unknown", None)
    assert resolve_anchor("tailscale2", amap) == ("unknown", None)
    assert resolve_anchor("shared", amap) == ("ambiguous", None)
    assert resolve_anchor("general-decisions", amap) == ("reserved", None)
    assert resolve_anchor("general_decisions", amap) == ("reserved", None)
    assert resolve_anchor("GENERAL-DECISIONS", amap) == ("reserved", None)


# ---------- 问号规则:引号、书名号、括号里的问号不算 ----------

@_isolated
def test_question_marks_inside_quotes_and_brackets_do_not_count():
    not_questions = [
        "AlphaHub 的评审引了「在?」这类回复,结论已定",
        "AlphaHub 的口径写作『OK了?』,以上为准",
        "AlphaHub 的例子是“对吧?”这种说法",
        "AlphaHub 的例子是‘行吗?’这种说法",
        "AlphaHub 的评审引了《是不是?》一句",
        "AlphaHub 的 〈在?〉 标记已定",
        "AlphaHub 的口径写作（对吧？）以上为准",
        "AlphaHub 的注释(OK了?)保留",
        'AlphaHub 的例子是 "在?" 这种说法',
        "AlphaHub 的例子是「他说(“行吗?”)」以上为准",
    ]
    for text in not_questions:
        assert not is_exploratory(text), text


@_isolated
def test_question_marks_outside_quotes_and_brackets_still_count():
    questions = [
        "AlphaHub 的端口说明(见附件)行不行?",
        "AlphaHub 的口径是「定稿」,那可以换吗?",
        "AlphaHub 的评审引了「在?」,这样行吗?",
        "AlphaHub 的端口说明(见附件)行不行?OK",
        "（未闭合 AlphaHub 的端口可以吗?",
        "AlphaHub 的注释「未闭合 这样行吗?",
    ]
    for text in questions:
        assert is_exploratory(text), text


@_isolated
def test_unclosed_or_overlong_pairs_do_not_swallow_the_question():
    assert is_exploratory("AlphaHub 的注释「未闭合\n在?」结论")
    assert is_exploratory("「" + "甲" * 250 + "?」AlphaHub 的结论已定")
    assert not is_exploratory("「" + "甲" * 150 + "?」AlphaHub 的结论已定")


# ---------- 整理里的归卡行为 ----------

@_isolated
def test_anchor_spelling_variants_write_the_card_in_both_modes():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                a = _add(sp, "proj:Alpha-Hub 的回滚窗口是 30 分钟")
                b = _add(sp, "proj:云枢 的端口策略已定")
                c = _add(sp, "proj:delta本尊拓扑 已确认")
                e = _add(sp, "proj:zetapanel 的口令已改")
                report = _run(consolidator, mode)
                assert [_item(report, i)["target"] for i in (a, b, c, e)] == [ALPHA, CLOUD, DELTA, BETA], mode
                assert [_item(report, i)["via"] for i in (a, b, c, e)] == ["anchor"] * 4
                assert [_item(report, i)["decision"] for i in (a, b, c, e)] == ["written"] * 4
                assert _hot(conn) == [] and report["routing"]["summary"]["anchor"] == 4
                assert [f["id"] for f in _facts(living, ALPHA)] == [a]
            finally:
                conn.close()


@_isolated
def test_unknown_anchor_stays_pending_without_model_or_name_guess():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            llm = _QueueLLM([])
            conn, sp, living, consolidator = _env(d, llm=llm)
            try:
                a = _add(sp, "proj:tailscale 的组网方式是中继回退")
                b = _add(sp, "proj:TAILSCALE 的组网走 ZetaPanel 面板的配置")
                report = _run(consolidator, mode)
                item = _item(report, a)
                assert item["decision"] == "pending" and item["target"] is None, (mode, item)
                assert item["reason"] == "anchor_unknown" and item["anchor"] == "anchor_unknown"
                assert item["anchor_name"] == "proj:tailscale"
                assert _item(report, b)["reason"] == "anchor_unknown"
                assert llm.calls == [], "anchor_unknown must not call the model"
                assert report["routing"]["summary"]["model_calls"] == 0
                assert report["routing"]["summary"]["pending_reasons"] == {"anchor_unknown": 2}
                assert _hot(conn) == [a, b] and report["archived_logs"] == 0
                assert _facts(living, BETA) == [] and living.get_state("proj:general_decisions") is None
                assert report["routing"]["card_suggestions"] == [
                    {"anchor": "proj:tailscale", "ids": [a, b], "suggestion": "建议建卡:tailscale"}
                ]
            finally:
                conn.close()


@_isolated
def test_ambiguous_anchor_stays_pending_and_is_not_guessed():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            llm = _QueueLLM([])
            conn, sp, living, consolidator = _env(d, llm=llm)
            try:
                a = _add(sp, "proj:shared 的端口已定,ZetaPanel 同机")
                report = _run(consolidator, mode)
                item = _item(report, a)
                assert item["decision"] == "pending" and item["reason"] == "anchor_ambiguous", (mode, item)
                assert item["anchor"] == "anchor_ambiguous" and item["target"] is None
                assert llm.calls == [] and _hot(conn) == [a]
                assert report["routing"]["summary"]["pending_reasons"] == {"anchor_ambiguous": 1}
                assert report["routing"]["card_suggestions"] == []
                assert _facts(living, ONE) == [] and _facts(living, TWO) == []
            finally:
                conn.close()


@_isolated
def test_valid_anchor_with_question_still_writes_and_skips_the_question_rule():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                a = _add(sp, "proj:alpha_hub 的回滚窗口是多久?")
                b = _add(sp, "proj:alpha_hub 的端口要不要换?")
                c = _add(sp, "proj:alpha_hub 的复验,评审里引了「在?」和「OK了?」")
                report = _run(consolidator, mode)
                assert [_item(report, i)["decision"] for i in (a, b, c)] == ["written"] * 3, mode
                assert [_item(report, i)["via"] for i in (a, b, c)] == ["anchor"] * 3
                assert [f["id"] for f in _facts(living, ALPHA)] == [a, b, c]
                assert _hot(conn) == []
            finally:
                conn.close()


@_isolated
def test_unknown_anchor_still_runs_the_question_rule():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, consolidator = _env(d)
        try:
            a = _add(sp, "proj:tailscale 的组网方式可以吗?")
            report = _run(consolidator, "apply")
            assert _item(report, a)["decision"] == "exploratory", _item(report, a)
            assert report["routing"]["summary"]["pending_reasons"] == {}
        finally:
            conn.close()


@_isolated
def test_quoted_question_in_unanchored_fact_routes_by_name():
    for mode in MODES:
        with tempfile.TemporaryDirectory() as d:
            conn, sp, living, consolidator = _env(d)
            try:
                a = _add(sp, "AlphaHub 的复验,评审里引了「在?」和「OK了?」,结论已定")
                report = _run(consolidator, mode)
                item = _item(report, a)
                assert item["via"] == "name" and item["target"] == ALPHA, (mode, item)
                assert item["decision"] == ("would_route" if mode == "log" else "written")
            finally:
                conn.close()


@_isolated
def test_reserved_anchor_variants_are_never_anchors():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, consolidator = _env(d)
        try:
            a = _add(sp, "proj:GENERAL-DECISIONS 的一条事实,没有卡名")
            report = _run(consolidator, "apply")
            item = _item(report, a)
            assert item["decision"] == "pending" and item["anchor"] == "reserved_anchor", item
            assert item.get("via") is None and living.get_state("proj:general_decisions") is None
            assert report["routing"]["card_suggestions"] == []
        finally:
            conn.close()


@_isolated
def test_unknown_anchor_candidate_is_verified_but_not_routed():
    with tempfile.TemporaryDirectory() as d:
        verdict = json.dumps({"is_fact": True, "entity": ALPHA}, ensure_ascii=False)
        llm = _QueueLLM([verdict])
        conn, sp, living, consolidator = _env(d, llm=llm)
        try:
            lid = sp.append_log("s", "user", "proj:tailscale 的组网走中继回退", phase_hint=CANDIDATE_HINT)
            report = _run(consolidator, "apply")
            assert len(llm.calls) == 1, "only the verification call, no routing call"
            assert report["candidates"]["verified"] == 0
            item = _item(report, lid)
            assert item["decision"] == "pending" and item["reason"] == "anchor_unknown", item
            assert _facts(living, ALPHA) == [] and _hot(conn) == [lid]
            assert report["routing"]["card_suggestions"][0]["suggestion"] == "建议建卡:tailscale"
        finally:
            conn.close()


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"✅ Issue #103 锚点容错与问号规则测试全部通过({len(tests)} 项)")
