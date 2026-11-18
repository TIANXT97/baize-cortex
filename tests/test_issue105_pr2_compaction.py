"""Issue #105 PR2:夜间整理里的常驻层合并与预算控制——超 90% 才动、先合并再降级、hard 不动、dry_run 只报告"""
import json
import os

from baize.core import memory_migrator as mm
from baize.core.consolidator import DreamConsolidator
from baize.core.resident import ResidentManager
from baize.core.resident_compactor import compact_resident, over_line, parse_merges, usage
from tests._mm_helpers import workspace


class MergeLLM:
    def __init__(self, merges=None, fail=False, raw=None):
        self.merges, self.fail, self.raw, self.calls = merges or [], fail, raw, []

    def chat(self, system, user):
        self.calls.append(user)
        if self.fail:
            raise TimeoutError("fake")
        if self.raw is not None:
            return self.raw
        return json.dumps({"merges": self.merges(user) if callable(self.merges) else self.merges}, ensure_ascii=False)


def rules_of(ws):
    return {r["id"]: r for r in ResidentManager(ws.conn).list()}


def seed(ws, specs):
    """specs: [(text, scope, priority, moved_from)] -> id 列表;写完把 updated_at 依次拉开,id 小的更旧。"""
    manager = ResidentManager(ws.conn)
    ids = []
    for index, (text, scope, priority, moved) in enumerate(specs):
        rule = manager.create(text, scope, priority, moved_from=moved)["rule"]
        ws.conn.execute("UPDATE resident_rules SET updated_at = ? WHERE id = ?", (f"2026-11-0{index + 1} 00:00:00", rule["id"]))
        ids.append(rule["id"])
    ws.conn.commit()
    return ids


def test_below_ninety_percent_does_nothing_and_asks_no_model():
    with workspace() as ws:
        seed(ws, [("先给结论再给依据", "dm", "normal", None), ("回复保持简洁", "group_ok", "normal", None)])
        llm = MergeLLM()
        digest = ws.table_digest()
        report = compact_resident(ws.conn, llm, "apply")
        assert report["status"] == "below_threshold" and report["triggered"] is False and llm.calls == []
        assert ws.table_digest() == digest
        assert report["before"]["dm_budget"] == 1500 and report["before"]["group_budget"] == 600


def test_ninety_percent_lines_on_dm_total_and_group_subset():
    with workspace() as ws:
        os_budget = {"CORTEX_RESIDENT_DM_BUDGET": "1000", "CORTEX_RESIDENT_GROUP_BUDGET": "400"}
        os.environ.update(os_budget)
        try:
            rules = [{"id": 1, "text": "字" * 880, "scope": "dm", "priority": "normal", "updated_at": "x"}]
            assert usage(rules)["dm_total"] == len("【常驻规则】") + 1 + 2 + 880 == 889
            assert over_line(rules) == {"dm": False, "group": False}, "889 / 1000 没过 90%"
            rules[0]["text"] = "字" * 895
            assert over_line(rules)["dm"] is True
            group = [{"id": 2, "text": "字" * 350, "scope": "group_ok", "priority": "normal", "updated_at": "x"}]
            assert over_line(group) == {"dm": False, "group": False}
            group[0]["text"] = "字" * 360
            assert over_line(group) == {"dm": False, "group": True}, "群聊可用部分超过 400 的 90% 也触发"
        finally:
            for name in os_budget:
                os.environ.pop(name, None)


def over_budget(ws, specs, dm="400", group="1000"):
    os.environ["CORTEX_RESIDENT_DM_BUDGET"] = dm
    os.environ["CORTEX_RESIDENT_GROUP_BUDGET"] = group
    return seed(ws, specs)


def test_merge_same_scope_then_retire_members_with_validation():
    with workspace() as ws:
        ids = over_budget(ws, [
            ("规则甲:" + "回复先给结论" * 8, "dm", "normal", None),
            ("规则乙:" + "回复要简洁" * 8, "dm", "low", "USER.md"),
            ("规则丙:" + "每次先确认范围" * 8, "dm", "normal", None),
            ("规则丁:" + "不要在群里提内部信息" * 5, "group_ok", "normal", None),
        ], dm="250")
        merged = "回复先给结论、保持简洁,动手前先确认范围。"
        llm = MergeLLM([
            {"ids": [ids[0], ids[1], ids[2]], "text": merged},
            {"ids": [ids[0], ids[3]], "text": "跨 scope 合并"},
        ])
        report = compact_resident(ws.conn, llm, "apply")
        assert report["triggered"] and report["mode"] == "apply"
        assert [m["keep_id"] for m in report["merged"]] == [ids[0]] and report["merged"][0]["status"] == "applied"
        assert report["merged"][0]["retired_ids"] == [ids[1], ids[2]]
        left = rules_of(ws)
        assert left[ids[0]]["text"] == merged and ids[1] not in left and ids[2] not in left
        assert left[ids[0]]["scope"] == "dm" and left[ids[0]]["moved_from"] == "USER.md", "带 USER.md 来源的合并后仍是 dm、仍带来源"
        assert left[ids[3]]["scope"] == "group_ok", "group_ok 的没被拖进去"
        assert any(r["reason"] == "ids_not_in_pool_or_reused" for r in report["rejected_merges"]), "只合并同 scope"
        retired = ResidentManager(ws.conn).list(include_retired=True)
        assert {r["id"] for r in retired} >= {ids[1], ids[2]}, "不物理删除"
        assert ResidentManager(ws.conn).history(ids[0]), "合并前的原文留在历史里"


def test_merge_rejections_hard_unsafe_not_shorter_and_changed_rules():
    with workspace() as ws:
        ids = over_budget(ws, [
            ("规则一:" + "先确认" * 20, "dm", "hard", None),
            ("规则二:" + "再动手" * 20, "dm", "normal", None),
            ("规则三:" + "最后复核" * 20, "dm", "normal", None),
            ("群规一:" + "不外传" * 15, "group_ok", "normal", None),
            ("群规二:" + "不截图" * 15, "group_ok", "normal", None),
        ], dm="300")
        ip = ".".join(["192", "0", "2", "9"])
        llm = MergeLLM(lambda user: [
            {"ids": [ids[0], ids[1]], "text": "含 hard 的合并"},
            {"ids": [ids[1], ids[2]], "text": "规则二加规则三" * 40},
            {"ids": [ids[3], ids[4]], "text": f"群里别提 {ip} 这台"},
            {"ids": [ids[1]], "text": "只有一条"},
            {"ids": "x", "text": "坏格式"},
        ])
        report = compact_resident(ws.conn, llm, "apply")
        reasons = [r["reason"] for r in report["rejected_merges"]]
        assert "ids_not_in_pool_or_reused" in reasons, "priority=hard 不进合并池"
        assert "not_shorter" in reasons
        assert any(r.startswith("invalid:") and "group_ok" in r for r in reasons), "合并结果走同一套校验:group_ok 不能含 IP"
        assert "bad_ids" in reasons
        assert report["merged"] == []
        assert rules_of(ws)[ids[0]]["priority"] == "hard"


def test_merge_aborts_when_rule_changed_during_run():
    with workspace() as ws:
        ids = over_budget(ws, [("规则甲:" + "先确认" * 20, "dm", "normal", None), ("规则乙:" + "再动手" * 20, "dm", "normal", None)],
                          dm="150")
        manager = ResidentManager(ws.conn)

        class Racing(MergeLLM):
            def chat(self, system, user):
                manager.update(ids[1], text="规则乙被人改过了")
                return super().chat(system, user)

        report = compact_resident(ws.conn, Racing([{"ids": ids, "text": "合并后的短文本"}]), "apply")
        assert report["rejected_merges"][0]["reason"] == "rule_changed_during_run"
        assert rules_of(ws)[ids[1]]["text"] == "规则乙被人改过了"


def test_demote_lowest_priority_oldest_first_never_hard_and_stops_under_ninety():
    with workspace() as ws:
        ids = over_budget(ws, [
            ("硬规则:" + "底线" * 40, "dm", "hard", None),
            ("普通旧规则:" + "先看" * 30, "dm", "normal", None),
            ("低优先旧规则:" + "顺手" * 30, "dm", "low", None),
            ("低优先新规则:" + "留意" * 30, "dm", "low", None),
            ("普通新规则:" + "复核" * 30, "dm", "normal", None),
        ], dm="300")
        report = compact_resident(ws.conn, MergeLLM([]), "apply")
        order = [d["rule_id"] for d in report["demoted"]]
        assert order[:2] == [ids[2], ids[3]], "优先级低的先降,同级最久没改的先降"
        assert ids[0] in rules_of(ws), "hard 不降级"
        for d in report["demoted"]:
            row = ws.conn.execute("SELECT raw_text, phase_hint, reason FROM scratchpad_log WHERE id = ?", (d["fact_id"],)).fetchone()
            assert row[0] == d["text"] and row[1] == "explicit_addition" and f"rule_id={d['rule_id']}" in row[2]
            assert "moved_from=resident_rules" in row[2]
        assert not over_line(list(rules_of(ws).values()))["dm"] or report["still_over"]
        assert report["after"]["dm_total"] * 100 <= report["after"]["dm_budget"] * 90 or report["still_over"]
        survivors = rules_of(ws)
        assert len(survivors) + len(order) == 5 and all(i not in survivors for i in order)


def test_all_hard_rules_cannot_be_reduced_and_report_says_so():
    with workspace() as ws:
        ids = over_budget(ws, [(f"硬规则{i}:" + "底线" * 40, "dm", "hard", None) for i in range(3)], dm="250")
        digest = ws.table_digest()
        llm = MergeLLM([])
        report = compact_resident(ws.conn, llm, "apply")
        assert report["still_over"] is True and report["demoted"] == [] and report["merged"] == []
        assert llm.calls == [], "池里没有可合并的非 hard 规则,不调模型"
        assert any("hard" in n for n in report["notes"])
        assert all(r["priority"] == "hard" for r in rules_of(ws).values()) and len(rules_of(ws)) == 3
        assert ws.table_digest() == digest, "除运行记录外什么都没变"
        assert ws.conn.execute("SELECT COUNT(*) FROM memory_migration_runs WHERE file='resident'").fetchone()[0] == 1
        injected = ResidentManager(ws.conn).render("dm")
        assert injected["report"]["total_chars"] <= 250, "还超就沿用按优先级注入:不报错,注入文本不超预算"


def test_group_overage_only_demotes_group_ok_rules():
    with workspace() as ws:
        ids = over_budget(ws, [
            ("私聊规则:" + "先看" * 30, "dm", "low", None),
            ("群规一:" + "不外传" * 20, "group_ok", "normal", None),
            ("群规二:" + "不截图" * 20, "group_ok", "normal", None),
        ], dm="1500", group="140")
        report = compact_resident(ws.conn, MergeLLM([]), "apply")
        assert report["over"] == {"dm": False, "group": True}
        assert [d["rule_id"] for d in report["demoted"]] == [ids[1]], "只有群聊部分超线,只降 group_ok 里最久没改的"
        assert ids[0] in rules_of(ws) and ids[2] in rules_of(ws)


def test_dry_run_reports_only_and_changes_nothing():
    with workspace() as ws:
        ids = over_budget(ws, [
            ("规则甲:" + "回复先给结论" * 8, "dm", "normal", None),
            ("规则乙:" + "回复要简洁" * 8, "dm", "normal", None),
            ("规则丙:" + "每次先确认范围" * 8, "dm", "low", None),
        ], dm="85")
        digest = ws.table_digest()
        llm = MergeLLM([{"ids": [ids[0], ids[1]], "text": "回复先给结论且简洁。"}])
        for mode, preview in (("dry_run", False), ("apply", True)):
            report = compact_resident(ws.conn, llm, mode, consolidation_dry_run=preview)
            assert report["mode"] == "dry_run"
            assert report["merged"][0]["status"] == "would_merge" and report["merged"][0]["keep_id"] == ids[0]
            assert all(d["status"] == "would_demote" for d in report["demoted"]) and report["demoted"]
            assert set(rules_of(ws)) == set(ids) and rules_of(ws)[ids[0]]["text"].startswith("规则甲")
            expected = ws.table_digest(skip=("memory_migration_runs",))
            assert expected == digest, "规则、事实、历史都没变"
        rows = ws.conn.execute("SELECT mode, status FROM memory_migration_runs").fetchall()
        assert rows == [("dry_run", "dry_run")], "夜间 dry_run 只留一条运行记录;整理预览(dry_run=True)连记录都不写"


def test_off_mode_skips_everything():
    with workspace() as ws:
        over_budget(ws, [("规则甲:" + "回复先给结论" * 8, "dm", "normal", None)] * 1, dm="50")
        digest = ws.table_digest()
        llm = MergeLLM([])
        assert compact_resident(ws.conn, llm, "off") == {"status": "off"} and llm.calls == []
        assert ws.table_digest(skip=()) == ws.table_digest(skip=()) and ws.table_digest() == digest


def test_model_failure_skips_merge_but_still_demotes():
    with workspace() as ws:
        over_budget(ws, [("规则甲:" + "回复先给结论" * 8, "dm", "low", None), ("规则乙:" + "回复要简洁" * 8, "dm", "normal", None)], dm="110")
        report = compact_resident(ws.conn, MergeLLM(fail=True), "apply")
        assert any("模型调用失败" in n for n in report["notes"]) and report["demoted"]
        assert compact_resident(ws.conn, None, "apply")["status"] in ("ok", "below_threshold")
        for raw in ("没有 JSON", '{"merges": "x"}', "[]", None):
            assert parse_merges(raw) == []


def test_user_origin_demotion_goes_to_private_profile_card_anchor():
    with workspace() as ws:
        ws.add_card()
        over_budget(ws, [("用户规则:" + "先看" * 40, "dm", "low", "USER.md"), ("别的规则:" + "复核" * 40, "dm", "normal", None)], dm="200")
        report = compact_resident(ws.conn, MergeLLM([]), "apply")
        first = report["demoted"][0]
        assert first["anchor"] == "proj:user_profile"
        text = ws.conn.execute("SELECT raw_text FROM scratchpad_log WHERE id = ?", (first["fact_id"],)).fetchone()[0]
        assert text == f"proj:user_profile {first['text']}"


def test_nightly_report_carries_resident_budget_and_migration_summary_and_never_migrates():
    from unittest import mock
    with workspace(memory=["x" * 1700]) as ws:
        os.environ["CORTEX_MEMORY_MIGRATION"] = "apply"
        ids = over_budget(ws, [("规则甲:" + "回复先给结论" * 8, "dm", "low", None), ("规则乙:" + "回复要简洁" * 8, "dm", "normal", None)], dm="110")
        ws.migrator(None).run_file("MEMORY.md", "test")
        digest = ws.digest("MEMORY.md")
        consolidator = DreamConsolidator(ws.conn, archive_dir=os.path.join(ws.root, "cold"), llm_client=MergeLLM([]))
        with mock.patch.object(mm.MemoryMigrator, "run_file", side_effect=AssertionError("夜间整理不该触发外迁")), \
                mock.patch.object(mm.MemoryMigrator, "on_request", side_effect=AssertionError("夜间整理不该触发外迁")):
            preview = consolidator.run_consolidation(dry_run=True)
            assert preview["resident_budget"]["mode"] == "dry_run" and preview["resident_budget"]["demoted"]
            assert set(rules_of(ws)) == set(ids), "整理预览不改常驻层"
            report = consolidator.run_consolidation()
        assert {r["file"] for r in report["memory_migration"]["runs"]} == {"MEMORY.md", "resident"}, "摘要含当天的外迁和常驻层整理记录"
        assert report["resident_budget"]["mode"] == "apply" and report["resident_budget"]["triggered"]
        assert set(rules_of(ws)) != set(ids), "夜间整理按预算降级了规则"
        assert ws.digest("MEMORY.md") == digest, "夜间整理不碰记忆文件"
        assert "routing" in report and "candidates" in report, "既有的归卡与候选报告字段原样保留"


def test_nightly_resident_step_error_does_not_break_consolidation():
    from unittest import mock
    with workspace() as ws:
        consolidator = DreamConsolidator(ws.conn, archive_dir=os.path.join(ws.root, "cold"))
        with mock.patch("baize.core.consolidator.compact_resident", side_effect=RuntimeError("boom")), \
                mock.patch("baize.core.consolidator.summarize_day", side_effect=RuntimeError("boom")):
            report = consolidator.run_consolidation(dry_run=True)
        assert report["resident_budget"] == {"status": "error", "error": "RuntimeError"}
        assert report["memory_migration"]["status"] == "error" and report["status"] in ("ok", "dry_run")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"✅ Issue #105 PR2 常驻层合并与预算控制测试全部通过({len(tests)} 项)")
