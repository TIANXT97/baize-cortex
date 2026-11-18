"""Issue #105 PR2:记忆文件自动外迁——字数算法、阈值、挑选、分类、写库读回、锁内改写、权限属主、dry_run 不变、回滚"""
import json
import logging
import os
import stat
import threading
import time
from unittest import mock

from baize.core import memory_migrator as mm
from baize.core.resident import ResidentManager
from baize.core.scratchpad import ScratchpadManager
from tests._mm_helpers import FakeLLM, know_entry, pad, rule_entry, workspace

D = mm.ENTRY_DELIMITER
BOTTOM = mm.BOTTOM_LINE_MARK


def big_memory(bottom_first=True):
    entries = []
    if bottom_first:
        entries.append(pad(f"{BOTTOM}回复先给结论,再给依据。", 200))
    entries += [rule_entry(i) for i in range(1, 4)] + [know_entry(i) for i in range(1, 7)]
    return entries


def run_apply(ws, llm=None, trigger="test", **kwargs):
    os.environ["CORTEX_MEMORY_MIGRATION"] = "apply"
    return ws.migrator(llm or FakeLLM(), **kwargs).run_file("MEMORY.md", trigger)


# ---------------------------------------------------------------- 字数算法 / 阈值 / 配置
def test_char_count_algorithm_locked_to_hermes():
    assert mm.ENTRY_DELIMITER == "\n§\n"
    assert mm.count_chars("") == 0
    assert mm.count_chars("a") == 1
    assert mm.count_chars("a\n§\nb") == 5
    assert mm.count_chars("a\n§\na\n§\nb") == 5, "重复条目去重(保持顺序)后再算"
    assert mm.count_chars("  a  \n§\n\n§\n b ") == len("a\n§\nb"), "每条 strip、空条目丢弃"
    assert mm.count_chars("a\n§\n§\nb") == len("a" + D + "§\nb"), "单独一个 § 不是分隔符,只认完整的 \\n§\\n"
    assert mm.parse_entries("x\n§\n\n§\ny") == ["x", "y"]
    assert mm.unique_entries("b\n§\na\n§\nb") == ["b", "a"], "去重保持首次出现的顺序"
    assert mm.entries_chars(["中文", "条目"]) == len("中文\n§\n条目"), "按字符数而不是字节数"


def test_thresholds_are_70_and_60_percent():
    assert (mm.TRIGGER_PERCENT, mm.TARGET_PERCENT) == (70, 60)
    assert (mm.trigger_line(2200), mm.target_line(2200)) == (1540, 1320)
    assert (mm.trigger_line(1375), mm.target_line(1375)) == (962, 825)


def test_limits_from_config_yaml_with_defaults():
    with workspace(config="memory:\n  memory_char_limit: 1000  # 注释\n  provider: x\n  user_char_limit: 500\n"
                          "other:\n  memory_char_limit: 9\n") as ws:
        limits = ws.migrator().limits()
        assert limits == {"MEMORY.md": 1000, "USER.md": 500}, "只认 memory 段,不吃别的段的同名键"
    for config in (None, "memory:\n  provider: x\n", "memory:\n  memory_char_limit: abc\n",
                   "memory:\n  memory_char_limit: 0\n", "model:\n  default: x\n"):
        with workspace(config=config) as ws:
            assert ws.migrator().limits() == {"MEMORY.md": 2200, "USER.md": 1375}, config


def test_mode_parsing_default_dry_run_and_invalid_warns():
    with workspace():
        assert mm.migration_mode() == ("dry_run", None)
        for raw, expected in (("off", "off"), ("DRY_RUN", "dry_run"), (" apply ", "apply"), ("", "dry_run")):
            os.environ["CORTEX_MEMORY_MIGRATION"] = raw
            assert mm.migration_mode()[0] == expected, raw
        os.environ["CORTEX_MEMORY_MIGRATION"] = "yes please"
        assert mm.migration_mode() == ("dry_run", "yes please")
        logger = logging.getLogger("baize.memory_migrator")
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger.addHandler(handler)
        try:
            mm._warned_invalid.discard("yes please")
            assert mm.current_mode() == "dry_run"
            assert mm.current_mode() == "dry_run"
        finally:
            logger.removeHandler(handler)
        assert len([r for r in records if "yes please" in r.getMessage()]) == 1, "非法值记一次警告,不刷屏"


# ---------------------------------------------------------------- 不超线 / off / dry_run
def test_below_threshold_not_triggered_and_reported():
    with workspace(memory=[know_entry(1), know_entry(2)], user=[know_entry(3, 300)]) as ws:
        before = ws.digest("MEMORY.md")
        os.environ["CORTEX_MEMORY_MIGRATION"] = "dry_run"
        migrator = ws.migrator(FakeLLM())
        report = migrator.run_file("MEMORY.md", "test")
        assert report["status"] == "skipped" and report["chars_before"] < report["trigger_line"]
        assert any("不外迁" in n for n in report["notes"])
        migrator.on_request("recall")
        assert migrator.wait_idle(5)
        assert ws.conn.execute("SELECT COUNT(*) FROM memory_migration_runs").fetchone()[0] == 1, "请求路径不到线不起外迁"
        assert ws.digest("MEMORY.md") == before


def test_off_mode_never_measures():
    with workspace(memory=big_memory()) as ws:
        os.environ["CORTEX_MEMORY_MIGRATION"] = "off"
        migrator = ws.migrator(FakeLLM())
        with mock.patch.object(migrator, "_count_for") as count, mock.patch.object(mm, "count_chars") as chars, \
                mock.patch.object(mm.os, "stat") as stat_call:
            migrator.on_request("recall")
            assert not count.called and not chars.called and not stat_call.called, "off 时连量字数都不做"
        assert migrator.run_file("MEMORY.md", "test") == {"status": "off", "file": "MEMORY.md"}
        assert ws.conn.execute("SELECT COUNT(*) FROM memory_migration_runs").fetchone()[0] == 0


def test_dry_run_changes_neither_files_nor_db():
    with workspace(memory=big_memory(), user=[know_entry(1, 600), know_entry(2, 600)]) as ws:
        files = {n: ws.digest(n) for n in ("MEMORY.md", "USER.md")}
        counts, digest = ws.table_counts(), ws.table_digest()
        llm = FakeLLM()
        os.environ["CORTEX_MEMORY_MIGRATION"] = "dry_run"
        report = ws.migrator(llm).run_file("MEMORY.md", "test")
        assert report["status"] == "dry_run" and report["mode"] == "dry_run"
        moved = [i for i in report["items"] if i["status"] == "would_move"]
        assert moved and all(i["kind"] in ("rule", "knowledge") for i in moved)
        assert report["chars_after"] < report["target_line"] <= report["chars_before"]
        assert report["backup_path"] is None
        assert {n: ws.digest(n) for n in files} == files, "文件哈希不变"
        assert ws.table_counts() == counts and ws.table_digest() == digest, "除运行记录外所有表的行数和内容都不变"
        assert not os.path.exists(os.path.join(ws.root, mm.BACKUP_DIRNAME)), "dry_run 不建备份目录"
        assert not os.path.exists(ws.path("MEMORY.md.lock")), "dry_run 不碰锁文件"
        run = ws.conn.execute("SELECT status, mode FROM memory_migration_runs").fetchall()
        assert run == [("dry_run", "dry_run")]


# ---------------------------------------------------------------- 挑选与分类
def test_apply_picks_earliest_skips_bottom_line_reaches_target():
    entries = big_memory()
    entries.insert(3, pad(f"{BOTTOM}另一条底线,同样不动。", 150))
    with workspace(memory=entries) as ws:
        report = run_apply(ws)
        assert report["status"] == "done"
        after = ws.entries("MEMORY.md")
        assert report["chars_after"] == mm.count_chars(ws.read("MEMORY.md")) < report["target_line"]
        assert [e for e in after if e.startswith(BOTTOM)] == [e for e in entries if e.startswith(BOTTOM)], "底线条目没动"
        skipped = [i for i in report["items"] if i["status"] == "skipped"]
        assert {i["reason"] for i in skipped} == {"bottom_line"}
        moved = [i for i in report["items"] if i["status"] == "moved"]
        order = [i["orig_index"] for i in moved]
        assert order == sorted(order), "从最早的条目挑起"
        non_bottom = [i for i, e in enumerate(entries) if not e.startswith(BOTTOM)]
        assert order == non_bottom[:len(order)], "没有跳过更早的非底线条目"
        assert len(after) == len(entries) - len(moved)


def test_each_moved_entry_found_verbatim_in_resident_or_facts():
    with workspace(memory=big_memory()) as ws:
        original = ws.entries("MEMORY.md")
        report = run_apply(ws)
        rules = {r["text"]: r for r in ResidentManager(ws.conn).list()}
        facts = {r[0]: r[1] for r in ws.conn.execute("SELECT id, raw_text FROM scratchpad_log")}
        moved = [i for i in report["items"] if i["status"] == "moved"]
        assert moved
        kinds = set()
        for item in moved:
            assert item["text"] in original and item["text"] not in ws.entries("MEMORY.md")
            if item["kind"] == "rule":
                rule = rules[item["text"]]
                assert rule["scope"] == "dm" and rule["moved_from"] == "MEMORY.md" and rule["batch_id"] == report["batch_id"]
                assert rule["id"] == item["rule_id"]
            else:
                assert facts[item["fact_id"]] == item["text"], "事实原文一字不差"
                assert item["queue"] == "pending_card_queue" and item["anchor"] is None
            kinds.add(item["kind"])
        assert kinds == {"rule", "knowledge"}
        row = ws.conn.execute(
            "SELECT phase_hint, category, reason, judge_status FROM scratchpad_log WHERE id = ?", (moved[-1]["fact_id"],)
        ).fetchone() if moved[-1]["kind"] == "knowledge" else None
        if row:
            assert row[0] == "explicit_addition" and "moved_from=MEMORY.md" in row[2]
        run = ws.conn.execute("SELECT status, backup_path, chars_before, chars_after FROM memory_migration_runs").fetchone()
        assert run[0] == "done" and os.path.exists(run[1]) and run[3] < run[2]


def test_backup_permissions_name_and_retention():
    with workspace(memory=big_memory()) as ws:
        folder = os.path.join(ws.root, mm.BACKUP_DIRNAME)
        os.makedirs(folder)
        old = os.path.join(folder, "MEMORY.md.mm-old.bak")
        fresh = os.path.join(folder, "MEMORY.md.mm-fresh.bak")
        for path in (old, fresh):
            with open(path, "w") as fh:
                fh.write("x")
        long_ago = time.time() - 15 * 86400
        os.utime(old, (long_ago, long_ago))
        original = ws.read("MEMORY.md")
        report = run_apply(ws)
        path = report["backup_path"]
        assert os.path.dirname(path) == folder and report["batch_id"] in os.path.basename(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        with open(path, encoding="utf-8") as fh:
            assert fh.read() == original, "备份是外迁前的原文件"
        assert not os.path.exists(old) and os.path.exists(fresh), "14 天前的旧备份被清掉,新的保留"


def test_all_bottom_line_picks_nothing_and_says_so():
    entries = [pad(f"{BOTTOM}底线{i}。", 300) for i in range(7)]
    with workspace(memory=entries) as ws:
        before = ws.digest("MEMORY.md")
        report = run_apply(ws)
        assert report["status"] == "done" and not [i for i in report["items"] if i["status"] == "moved"]
        assert any("没有可迁走" in n for n in report["notes"])
        assert ws.digest("MEMORY.md") == before


def test_partial_when_cannot_reach_target_reports_it():
    entries = [pad(f"{BOTTOM}底线{i}。", 500) for i in range(3)] + [know_entry(1, 100)]
    with workspace(memory=entries) as ws:
        report = run_apply(ws)
        assert [i["text"] for i in report["items"] if i["status"] == "moved"] == [entries[3]]
        assert report["chars_after"] >= report["target_line"]
        assert any("降不到目标" in n for n in report["notes"]), "能挑的都挑了,报告写明降不到"


def test_model_failures_leave_entries_untouched_and_continue():
    entries = big_memory()
    bad_timeout, bad_garble = entries[1], entries[4]
    with workspace(memory=entries) as ws:
        llm = FakeLLM(fail_on=(bad_timeout[:20],), garble_on=(bad_garble[:20],))
        report = run_apply(ws, llm)
        after = ws.entries("MEMORY.md")
        assert bad_timeout in after and bad_garble in after, "模型失败、解析不了的条目这次不动"
        reasons = {i["text"]: i["reason"] for i in report["items"] if i["status"] == "skipped"}
        assert reasons[bad_timeout].startswith("model_failed") and reasons[bad_garble] == "unparsable_answer"
        assert any(i["status"] == "moved" for i in report["items"]), "别的条目照常迁"
        assert report["chars_after"] < report["target_line"]


def test_model_call_cap_per_run():
    config = "memory:\n  memory_char_limit: 1000\n"
    entries = [know_entry(i, 40) for i in range(30)]
    with workspace(memory=entries, config=config) as ws:
        llm = FakeLLM()
        report = run_apply(ws, llm)
        assert mm.MAX_MODEL_CALLS == 10 and len(llm.calls) == 10 and report["model_calls"] == 10
        assert any("上限" in n for n in report["notes"]), "到上限就停,其余留到下次"
        assert len([i for i in report["items"] if i["status"] == "moved"]) == 10


def test_no_model_client_moves_nothing():
    with workspace(memory=big_memory()) as ws:
        before = ws.digest("MEMORY.md")
        os.environ["CORTEX_MEMORY_MIGRATION"] = "apply"
        report = ws.migrator(None).run_file("MEMORY.md", "test")
        assert ws.digest("MEMORY.md") == before
        assert {i["reason"] for i in report["items"] if i["status"] == "skipped"} >= {"no_model_client"}


# ---------------------------------------------------------------- 规则降级为知识的情形
def test_rule_fallbacks_to_knowledge_with_reason():
    long_rule = pad("规则长:务必先确认范围再动手。", 400)
    multi = "规则多行:第一行\n第二行也要遵守"
    entries = [long_rule, multi] + [know_entry(i) for i in range(1, 7)]
    with workspace(memory=entries) as ws:
        report = run_apply(ws)
        by_text = {i["text"]: i for i in report["items"]}
        assert by_text[long_rule]["kind"] == "knowledge" and by_text[long_rule]["rule_fallback"].startswith("rule_invalid")
        assert by_text[multi]["kind"] == "knowledge" and by_text[multi]["rule_fallback"] == "rule_text_not_single_line"
        facts = [r[0] for r in ws.conn.execute("SELECT raw_text FROM scratchpad_log")]
        assert long_rule in facts and multi in facts, "多行文本原样进事实,不被规则的折行改写"
        assert ResidentManager(ws.conn).list() == []


def test_rule_over_resident_budget_becomes_knowledge():
    entries = [rule_entry(i, 200) for i in range(1, 5)] + [know_entry(i) for i in range(1, 6)]
    with workspace(memory=entries) as ws:
        os.environ["CORTEX_RESIDENT_DM_BUDGET"] = "300"
        report = run_apply(ws)
        rules = ResidentManager(ws.conn).list()
        assert len(rules) == 1, "常驻层预算只装得下一条"
        fallbacks = [i for i in report["items"] if i.get("rule_fallback") == "resident_budget_exceeded"]
        assert len(fallbacks) >= 1 and all(i["kind"] == "knowledge" and i.get("fact_id") for i in fallbacks)


def test_identical_existing_rule_is_reused_not_duplicated_nor_retired_on_rollback():
    entries = [rule_entry(1)] + [know_entry(i) for i in range(1, 8)]
    with workspace(memory=entries) as ws:
        ResidentManager(ws.conn).create(entries[0], "dm", "hard", source="manual")
        report = run_apply(ws)
        item = next(i for i in report["items"] if i["text"] == entries[0])
        assert item["status"] == "moved" and item["rule_reused"] is True
        assert len(ResidentManager(ws.conn).list()) == 1, "没有重复写一条"
        migrator = ws.migrator()
        migrator.rollback(report["batch_id"], dry_run=False)
        rule = ResidentManager(ws.conn).list()
        assert len(rule) == 1 and rule[0]["priority"] == "hard" and rule[0]["status"] == "active", "回滚不撤预先存在的规则"


# ---------------------------------------------------------------- USER.md
def user_entries():
    return [rule_entry(1, 150), rule_entry(2, 150), know_entry(1, 300), know_entry(2, 300), know_entry(3, 300)]


def test_user_md_rules_dm_and_knowledge_only_in_private_card():
    with workspace(user=user_entries()) as ws:
        ws.add_card("proj:user_profile", "private")
        cards = ws.conn.execute("SELECT COUNT(*) FROM living_states").fetchone()[0]
        os.environ["CORTEX_MEMORY_MIGRATION"] = "apply"
        report = ws.migrator(FakeLLM()).run_file("USER.md", "test")
        assert report["status"] == "done" and report["limit"] == 1375
        assert report["chars_after"] < mm.target_line(1375) == 825
        moved = [i for i in report["items"] if i["status"] == "moved"]
        rules = [i for i in moved if i["kind"] == "rule"]
        knowledge = [i for i in moved if i["kind"] == "knowledge"]
        assert rules and knowledge
        for item in rules:
            row = ResidentManager(ws.conn).get(item["rule_id"])
            assert row["scope"] == "dm" and row["moved_from"] == "USER.md", "USER.md 来的规则固定 dm"
        for item in knowledge:
            text = ws.conn.execute("SELECT raw_text FROM scratchpad_log WHERE id = ?", (item["fact_id"],)).fetchone()[0]
            assert text == f"proj:user_profile {item['text']}" and item["anchor"] == "proj:user_profile"
        assert ws.conn.execute("SELECT COUNT(*) FROM living_states").fetchone()[0] == cards, "不自动建卡"
        try:
            ResidentManager(ws.conn).create("规则:某条", "group_ok", moved_from="USER.md")
            raise AssertionError("USER.md 来源不能标 group_ok")
        except Exception as exc:
            assert getattr(exc, "status", None) == 422


def test_user_md_knowledge_without_private_card_gets_no_anchor_and_says_why():
    cases = {"missing": None, "not_private": ("proj:user_profile", "group_ok", "ACTIVE"),
             "inactive": ("proj:user_profile", "private", "ARCHIVED")}
    for label, card in cases.items():
        with workspace(user=user_entries()) as ws:
            if card:
                ws.add_card(*card)
            cards = ws.conn.execute("SELECT COUNT(*) FROM living_states").fetchone()[0]
            os.environ["CORTEX_MEMORY_MIGRATION"] = "apply"
            report = ws.migrator(FakeLLM(force_kind="knowledge")).run_file("USER.md", "test")
            moved = [i for i in report["items"] if i["status"] == "moved"]
            assert moved and all(i["anchor"] is None and i["anchor_note"].startswith("user_profile_") for i in moved), label
            texts = [r[0] for r in ws.conn.execute("SELECT raw_text FROM scratchpad_log")]
            assert all(not t.startswith("proj:") for t in texts), label
            assert ws.conn.execute("SELECT COUNT(*) FROM living_states").fetchone()[0] == cards, "不自动建卡"


def test_user_md_rule_fallback_never_makes_group_ok():
    with workspace(user=user_entries()) as ws:
        ws.add_card()
        os.environ["CORTEX_MEMORY_MIGRATION"] = "apply"
        ws.migrator(FakeLLM(force_kind="rule")).run_file("USER.md", "test")
        scopes = {r["scope"] for r in ResidentManager(ws.conn).list()}
        assert scopes <= {"dm"}


# ---------------------------------------------------------------- 镜像事实去重
def test_mirror_fact_expired_and_restored_on_rollback():
    entries = big_memory()
    target = entries[4]
    with workspace(memory=entries) as ws:
        mirror = ScratchpadManager(ws.conn).add_explicit(
            "s1", target, "fact", "hermes_builtin_mirror:add:MEMORY.md", "k-mirror", None
        )
        report = run_apply(ws)
        item = next(i for i in report["items"] if i["text"] == target)
        assert item["status"] == "moved" and item["mirror_ids"] == [mirror]
        live = ws.conn.execute(
            "SELECT id FROM scratchpad_log WHERE phase_hint='explicit_addition' AND expired_at IS NULL AND raw_text = ?",
            (target,),
        ).fetchall()
        assert [r[0] for r in live] == [item["fact_id"]], "夜里只会归一次:镜像行标过期,只剩外迁写的那条"
        assert ws.conn.execute("SELECT expired_at FROM scratchpad_log WHERE id = ?", (mirror,)).fetchone()[0]
        ws.migrator().rollback(report["batch_id"], dry_run=False)
        assert ws.conn.execute("SELECT expired_at FROM scratchpad_log WHERE id = ?", (mirror,)).fetchone()[0] is None, \
            "回滚把镜像事实恢复"
        assert ws.conn.execute("SELECT expired_at FROM scratchpad_log WHERE id = ?", (item["fact_id"],)).fetchone()[0]


def test_mirror_match_uses_normalized_form():
    text = "全角逗号,的条目:务必保留"
    mirrored = mm.normalize_content(text)
    with workspace() as ws:
        rid = ScratchpadManager(ws.conn).add_explicit("s", mirrored, "fact", "hermes_builtin_mirror:add:USER.md", "k", None)
        assert mm._find_mirror_rows(ws.conn, text) == [rid]
        other = ScratchpadManager(ws.conn).add_explicit("s", text + "x", "fact", "hermes_builtin_mirror:add:USER.md", "k2", None)
        assert other not in mm._find_mirror_rows(ws.conn, text), "必须一字不差才算同一条"


# ---------------------------------------------------------------- 权限与属主
def test_mode_preserved_and_owner_restored_via_fchown():
    with workspace(memory=big_memory()) as ws:
        os.chmod(ws.path("MEMORY.md"), 0o640)
        st = os.stat(ws.path("MEMORY.md"))
        real = os.fchown
        calls = []

        def spy(fd, uid, gid):
            calls.append((uid, gid))
            return real(fd, uid, gid)

        with mock.patch.object(mm.os, "fchown", side_effect=spy):
            report = run_apply(ws)
        assert report["status"] == "done"
        assert stat.S_IMODE(os.stat(ws.path("MEMORY.md")).st_mode) == 0o640, "权限保持原样"
        assert (st.st_uid, st.st_gid) in calls, "新文件 chown 回原 uid/gid"
        new = os.stat(ws.path("MEMORY.md"))
        assert (new.st_uid, new.st_gid) == (st.st_uid, st.st_gid)
        assert not [n for n in os.listdir(os.path.dirname(ws.path("MEMORY.md"))) if n.startswith(".mem_cortex_")], "没有残留临时文件"


def test_owner_restored_to_a_different_uid_and_chown_failure_aborts_before_replace():
    with workspace(memory=big_memory()) as ws:
        before = ws.digest("MEMORY.md")
        st = os.stat(ws.path("MEMORY.md"))
        seen = []
        with mock.patch.object(mm.os, "fchown", side_effect=lambda fd, u, g: seen.append((u, g))):
            mm.write_entries_atomic(mm.Path(ws.path("MEMORY.md")), ["a", "b"])
        assert seen == [(st.st_uid, st.st_gid)]
        ws.write("MEMORY.md", big_memory())
        assert ws.digest("MEMORY.md") == before
        with mock.patch.object(mm.os, "fchown", side_effect=PermissionError("nope")):
            digest = ws.digest("MEMORY.md")
            other = (st.st_uid + 1, st.st_gid)
            fake = os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, other[0], other[1], st.st_size,
                                   int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))
            with mock.patch.object(mm.os, "stat", return_value=fake):
                try:
                    mm.write_entries_atomic(mm.Path(ws.path("MEMORY.md")), ["x"])
                    raise AssertionError("属主改不回去必须中止")
                except PermissionError:
                    pass
            assert ws.digest("MEMORY.md") == digest, "中止时原文件没动"
            assert not [n for n in os.listdir(os.path.dirname(ws.path("MEMORY.md"))) if n.startswith(".mem_cortex_")]


def test_lock_file_created_0600_with_owner_of_memory_file():
    with workspace(memory=big_memory()) as ws:
        lock = ws.path("MEMORY.md.lock")
        assert not os.path.exists(lock)
        st = os.stat(ws.path("MEMORY.md"))
        with mock.patch.object(mm.os, "fchown") as fchown:
            with mm.file_lock(mm.Path(ws.path("MEMORY.md"))):
                pass
        assert stat.S_IMODE(os.stat(lock).st_mode) == 0o600
        fchown.assert_called_once()
        assert fchown.call_args[0][1:] == (st.st_uid, st.st_gid), "root 新建的锁文件要归还给记忆文件的属主"
        mode_before = os.stat(lock).st_mode
        with mm.file_lock(mm.Path(ws.path("MEMORY.md"))):
            pass
        assert os.stat(lock).st_mode == mode_before


def test_file_lock_is_flock_on_dot_lock_and_times_out():
    import fcntl
    with workspace(memory=big_memory()) as ws:
        lock = ws.path("MEMORY.md.lock")
        open(lock, "w").close()
        fd = os.open(lock, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            try:
                with mm.file_lock(mm.Path(ws.path("MEMORY.md")), timeout=0.2):
                    raise AssertionError("别人持锁时不能拿到")
            except TimeoutError:
                pass
        finally:
            os.close(fd)
        with mm.file_lock(mm.Path(ws.path("MEMORY.md")), timeout=0.2):
            pass


# ---------------------------------------------------------------- 失败时文件不动
def test_any_step_failure_leaves_file_untouched_and_undoes_db():
    scenarios = {
        "backup": lambda: mock.patch.object(mm, "backup_file", side_effect=OSError("disk full")),
        "lock": lambda: mock.patch.object(mm, "file_lock", side_effect=TimeoutError("busy")),
        "write": lambda: mock.patch.object(mm, "write_entries_atomic", side_effect=OSError("ro")),
        "readback": lambda: mock.patch.object(mm.MemoryMigrator, "_verify_readback", side_effect=RuntimeError("diff")),
    }
    for label, patcher in scenarios.items():
        with workspace(memory=big_memory()) as ws:
            before = ws.digest("MEMORY.md")
            with patcher():
                report = run_apply(ws)
            assert report["status"] == "failed", label
            assert ws.digest("MEMORY.md") == before, f"{label}:文件不动"
            assert ResidentManager(ws.conn).list() == [], f"{label}:规则已撤销"
            live = ws.conn.execute("SELECT COUNT(*) FROM scratchpad_log WHERE expired_at IS NULL").fetchone()[0]
            assert live == 0, f"{label}:事实已标过期"
            status = ws.conn.execute("SELECT status FROM memory_migration_runs").fetchone()[0]
            assert status == "failed"


def test_entries_changed_by_hermes_before_removal_are_kept_and_db_copy_undone():
    entries = big_memory()
    with workspace(memory=entries) as ws:
        original_verify = mm.MemoryMigrator._verify_readback
        gone = entries[1]
        fresh = pad("Hermes 刚写的新条目。", 90)

        def hermes_writes(self, conn, pending):
            original_verify(self, conn, pending)
            current = ws.entries("MEMORY.md")
            ws.write("MEMORY.md", [e for e in current if e != gone] + [fresh])

        with mock.patch.object(mm.MemoryMigrator, "_verify_readback", hermes_writes):
            report = run_apply(ws)
        after = ws.entries("MEMORY.md")
        assert fresh in after, "Hermes 在这期间新写的条目原样保留"
        assert gone not in after
        item = next(i for i in report["items"] if i["text"] == gone)
        assert item["status"] == "undone" and item["reason"] == "changed_in_file_before_removal"
        assert gone not in [r["text"] for r in ResidentManager(ws.conn).list()]
        assert gone not in [r[0] for r in ws.conn.execute("SELECT raw_text FROM scratchpad_log WHERE expired_at IS NULL")]


# ---------------------------------------------------------------- 单飞 / 间隔 / 运行标记
class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_single_flight_and_ten_minute_interval():
    with workspace(memory=big_memory()) as ws:
        os.environ["CORTEX_MEMORY_MIGRATION"] = "apply"
        gate, started = threading.Event(), threading.Event()

        def block():
            started.set()
            gate.wait(10)

        clock = Clock()
        llm = FakeLLM(delay=block, force_kind="knowledge", fail_on=("规则", "知识"))
        migrator = ws.migrator(llm, clock=clock)
        migrator.on_request("recall")
        assert started.wait(5), "后台线程已开始调模型"
        for _ in range(5):
            migrator.on_request("recall")
        other = ws.migrator(FakeLLM())
        assert other.run_file("MEMORY.md", "test")["status"] == "busy", "库里有新鲜的运行标记,另一个实例不能同时跑"
        gate.set()
        assert migrator.wait_idle(10)
        runs = ws.conn.execute("SELECT status FROM memory_migration_runs ORDER BY id").fetchall()
        assert [r[0] for r in runs] == ["done"], "同一时间只跑一个"
        before = len(llm.calls)
        clock.now += mm.MIN_INTERVAL_SECONDS - 1
        migrator.on_request("recall")
        migrator.wait_idle(5)
        assert len(llm.calls) == before, "结束后 10 分钟内不再检查同一个文件"
        clock.now += 2
        gate.set()
        migrator.on_request("recall")
        assert migrator.wait_idle(10)
        assert len(llm.calls) > before, "过了 10 分钟再次触发(文件仍超线:上次模型全失败没迁走)"
        assert mm.MIN_INTERVAL_SECONDS == 600 and mm.STALE_RUN_SECONDS == 1800


def test_stale_running_marker_can_be_taken_over_fresh_one_cannot():
    with workspace(memory=big_memory()) as ws:
        os.environ["CORTEX_MEMORY_MIGRATION"] = "dry_run"
        fresh = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 60))
        stale = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 31 * 60))
        ws.conn.execute("INSERT INTO memory_migration_runs (batch_id, trigger_source, file, mode, status, started_at) "
                        "VALUES ('x-fresh','t','MEMORY.md','apply','running',?)", (fresh,))
        ws.conn.commit()
        assert ws.migrator(FakeLLM()).run_file("MEMORY.md", "test")["status"] == "busy"
        ws.conn.execute("UPDATE memory_migration_runs SET started_at = ? WHERE batch_id = 'x-fresh'", (stale,))
        ws.conn.commit()
        assert ws.migrator(FakeLLM()).run_file("MEMORY.md", "test")["status"] == "dry_run"
        assert ws.conn.execute("SELECT status FROM memory_migration_runs WHERE batch_id = 'x-fresh'").fetchone()[0] == "abandoned"


def test_dry_run_requests_do_not_repeat_for_unchanged_file():
    with workspace(memory=big_memory()) as ws:
        os.environ["CORTEX_MEMORY_MIGRATION"] = "dry_run"
        clock = Clock()
        migrator = ws.migrator(FakeLLM(), clock=clock)
        migrator.on_request("recall")
        assert migrator.wait_idle(10)
        clock.now += 10_000
        migrator.on_request("recall")
        migrator.wait_idle(10)
        assert ws.conn.execute("SELECT COUNT(*) FROM memory_migration_runs").fetchone()[0] == 1, "文件没变不重复出 dry_run 报告"


def test_request_path_swallows_all_errors():
    with workspace(memory=big_memory()) as ws:
        os.environ["CORTEX_MEMORY_MIGRATION"] = "apply"
        migrator = ws.migrator(FakeLLM())
        with mock.patch.object(mm, "count_chars", side_effect=RuntimeError("boom")):
            migrator.on_request("recall")
        with mock.patch.object(mm.os, "stat", side_effect=PermissionError("denied")):
            migrator.on_request("recall")
        with mock.patch.object(mm.threading.Thread, "start", side_effect=RuntimeError("no threads")):
            migrator.on_request("recall")
        assert migrator._flight.acquire(blocking=False), "起线程失败也要把单飞锁还回去"
        migrator._flight.release()
        with mock.patch.object(mm.MemoryMigrator, "run_file", side_effect=RuntimeError("late failure")):
            migrator.on_request("recall")
            assert migrator.wait_idle(5)
        assert migrator._flight.acquire(blocking=False)


def test_count_cache_by_mtime_and_size():
    with workspace(memory=big_memory()) as ws:
        migrator = ws.migrator()
        with mock.patch.object(mm, "count_chars", wraps=mm.count_chars) as counter:
            migrator._count_for("MEMORY.md")
            migrator._count_for("MEMORY.md")
            assert counter.call_count == 1, "mtime/size 没变不重读"
            ws.write("MEMORY.md", [know_entry(1)])
            count, _ = migrator._count_for("MEMORY.md")
            assert counter.call_count == 2 and count == 240


# ---------------------------------------------------------------- 回滚
def test_rollback_adds_back_without_clobbering_new_entries():
    entries = big_memory()
    with workspace(memory=entries) as ws:
        report = run_apply(ws)
        moved = [i["text"] for i in report["items"] if i["status"] == "moved"]
        hermes_new = pad("迁走之后 Hermes 新写的一条。", 100)
        ws.write("MEMORY.md", ws.entries("MEMORY.md") + [hermes_new])
        migrator = ws.migrator()
        preview = migrator.rollback(report["batch_id"], dry_run=True)
        assert preview["status"] == "would_rollback" and preview["dry_run"] is True
        assert [i["text"] for i in preview["items"]] == moved
        assert set(ws.entries("MEMORY.md")).isdisjoint(moved), "dry_run 不改文件"
        assert all(r["status"] == "active" for r in ResidentManager(ws.conn).list())
        done = migrator.rollback(report["batch_id"], dry_run=False)
        assert done["status"] == "rolled_back"
        after = ws.entries("MEMORY.md")
        assert hermes_new in after, "Hermes 之后的新写入没被冲掉"
        assert all(text in after for text in moved) and len(after) == len(set(after)), "条目加回且不重复"
        assert after[:len(entries)] == [e for e in entries], "加回到原来的位置"
        assert not ResidentManager(ws.conn).list(), "常驻规则软删除"
        assert len(ResidentManager(ws.conn).list(include_retired=True)) >= 1, "不物理删除"
        facts = ws.conn.execute("SELECT expired_at FROM scratchpad_log WHERE session_id LIKE 'memory_migration:%'").fetchall()
        assert facts and all(f[0] for f in facts), "事实标过期"
        status = ws.conn.execute("SELECT status, rolled_back_at FROM memory_migration_runs WHERE batch_id = ?",
                                 (report["batch_id"],)).fetchone()
        assert status[0] == "rolled_back" and status[1]
        try:
            migrator.rollback(report["batch_id"], dry_run=False)
            raise AssertionError("不能重复撤回")
        except ValueError:
            pass
        try:
            migrator.rollback("mm-nope", dry_run=True)
            raise AssertionError("没有这个批次")
        except LookupError:
            pass


def test_rollback_skips_entries_already_in_file():
    with workspace(memory=big_memory()) as ws:
        report = run_apply(ws)
        moved = [i["text"] for i in report["items"] if i["status"] == "moved"]
        ws.write("MEMORY.md", ws.entries("MEMORY.md") + [moved[0]])
        ws.migrator().rollback(report["batch_id"], dry_run=False)
        after = ws.entries("MEMORY.md")
        assert after.count(moved[0]) == 1 and all(m in after for m in moved)


def test_rollback_refused_when_it_would_exceed_limit():
    with workspace(memory=big_memory()) as ws:
        report = run_apply(ws)
        filler = [pad(f"Hermes 之后写满的第{i}条。", 300) for i in range(5)]
        ws.write("MEMORY.md", ws.entries("MEMORY.md") + filler)
        digest = ws.digest("MEMORY.md")
        before_rules = ResidentManager(ws.conn).list()
        out = ws.migrator().rollback(report["batch_id"], dry_run=False)
        assert out["status"] == "refused_over_limit" and out["over_limit"] is True
        assert out["backup_path"] == report["backup_path"] and os.path.exists(out["backup_path"])
        assert "超过上限" in out["reason"]
        assert ws.digest("MEMORY.md") == digest, "超上限时不写文件"
        assert ResidentManager(ws.conn).list() == before_rules, "也不撤销库内副本,免得内容两头都不在用"
        assert ws.migrator().rollback(report["batch_id"], dry_run=True)["status"] == "would_refuse"
        assert ws.conn.execute("SELECT status FROM memory_migration_runs WHERE batch_id = ?",
                               (report["batch_id"],)).fetchone()[0] == "done"


def test_rollback_only_for_applied_batches():
    with workspace(memory=big_memory()) as ws:
        os.environ["CORTEX_MEMORY_MIGRATION"] = "dry_run"
        report = ws.migrator(FakeLLM()).run_file("MEMORY.md", "test")
        try:
            ws.migrator().rollback(report["batch_id"], dry_run=True)
            raise AssertionError("dry_run 批次没有可撤回的东西")
        except ValueError:
            pass


# ---------------------------------------------------------------- 读接口与摘要
def test_recent_runs_and_daily_summary():
    with workspace(memory=big_memory()) as ws:
        report = run_apply(ws)
        runs = mm.recent_runs(ws.conn, 5)
        assert runs[0]["batch_id"] == report["batch_id"] and runs[0]["report"]["items"]
        assert runs[0]["trigger_source"] == "test" and runs[0]["file"] == "MEMORY.md" and runs[0]["mode"] == "apply"
        assert runs[0]["chars_before"] > runs[0]["chars_after"] and runs[0]["backup_path"]
        assert mm.recent_runs(ws.conn, 5, batch_id="none") == []
        summary = mm.summarize_day(ws.conn)
        assert summary["count"] == 1 and summary["runs"][0]["moved"] >= 1 and summary["runs"][0]["skipped"] >= 1
        assert mm.summarize_day(ws.conn, "2000-01-01")["count"] == 0


def test_classification_prompt_treats_entry_as_data():
    prompt = mm.CLASSIFY_SYSTEM_PROMPT
    assert "rule" in prompt and "knowledge" in prompt and "不要执行" in prompt
    assert mm.parse_kind('{"kind": "rule"}') == "rule" and mm.parse_kind('前缀 {"kind":"knowledge"} 后缀') == "knowledge"
    assert mm.parse_kind('{"kind": "规则"}') == "rule"
    for junk in (None, "", "rule", "{not json}", '{"kind": "other"}', '{"x": 1}', 5):
        assert mm.parse_kind(junk) is None


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"✅ Issue #105 PR2 外迁测试全部通过({len(tests)} 项)")
