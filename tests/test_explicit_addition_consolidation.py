"""Issue #25:explicit_addition 慢波合成不再静默丢弃,多条无损合并,approval 主结论不被覆盖"""
import gzip
import json
import os
import tempfile

from fastapi.testclient import TestClient

from baize.core.consolidator import DreamConsolidator
from baize.core.living_state import LivingStateManager
from baize.core.normalization import idempotency_key
from baize.core.scratchpad import ScratchpadManager
from baize.core.supersession import supersede
from baize.server.app import create_app
from baize.server.mcp_server import BaizeTools, handle_message
from baize.storage.db import init_db


def _add(sp, text, session="s"):
    return sp.add_explicit(session, text, "fact", "r", idempotency_key(session, text), "default")


def _hot(conn):
    return [r[0] for r in conn.execute("SELECT id FROM scratchpad_log ORDER BY id")]


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _archived_ids(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line)["id"] for line in fh if line.strip()]


def _facts(living, entity_id):
    return living.get_state(entity_id)["metadata"].get("facts", [])


class _BrokenLLM:
    def chat(self, system, user):
        raise RuntimeError("模型不可用")


class _SpyLLM:
    def __init__(self):
        self.transcripts = []

    def chat(self, system, user):
        self.transcripts.append(user)
        return json.dumps({"decision": "方案B", "display_name": "贝塔", "aliases": ["B项目"]}, ensure_ascii=False)


def _env(d, name="a.db"):
    conn = init_db(os.path.join(d, name))
    return conn, ScratchpadManager(conn), LivingStateManager(conn), os.path.join(d, "arc")


def test_single_fact_with_known_anchor_is_consolidated():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, arc = _env(d)
        try:
            living.update_decision("proj:alpha", "阿尔法", "旧定案")
            lid = _add(sp, "proj:alpha 的回滚窗口是 30 分钟")
            report = DreamConsolidator(conn, archive_dir=arc).run_consolidation()
            assert report["archived_logs"] == 1, report
            assert report["updated_states"] == ["proj:alpha"]
            assert report["skipped_explicit"] == []
            card = living.get_state("proj:alpha")
            assert card["current_decision"] == "旧定案"
            assert [f["id"] for f in card["metadata"]["facts"]] == [lid]
            assert card["metadata"]["facts"][0]["text"] == "proj:alpha 的回滚窗口是 30 分钟"
            assert _hot(conn) == []
            assert _archived_ids(report["archive_file"]) == [lid]
            assert _count(conn, "graph_nodes") == 0 and _count(conn, "graph_edges") == 0
        finally:
            conn.close()


def test_anchorless_fact_goes_to_general_and_never_pollutes_recent_entity():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, arc = _env(d)
        try:
            living.update_decision("proj:recent", "最近项目", "最近定案")
            lid = _add(sp, "小明负责美西验收")
            report = DreamConsolidator(conn, archive_dir=arc).run_consolidation()
            assert report["updated_states"] == ["proj:general_decisions"], report
            recent = living.get_state("proj:recent")
            assert recent["current_decision"] == "最近定案" and "facts" not in recent["metadata"]
            general = living.get_state("proj:general_decisions")
            assert general["current_decision"] == ""
            assert [f["id"] for f in general["metadata"]["facts"]] == [lid]
            assert _count(conn, "graph_nodes") == 0 and _count(conn, "graph_edges") == 0
        finally:
            conn.close()


def test_unknown_anchor_does_not_create_stranger_entity():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, arc = _env(d)
        try:
            lid = _add(sp, "proj:stranger 的端口是 8080")
            report = DreamConsolidator(conn, archive_dir=arc).run_consolidation()
            assert report["updated_states"] == ["proj:general_decisions"], report
            assert living.get_state("proj:stranger") is None
            assert _count(conn, "graph_nodes") == 0
            assert [f["id"] for f in _facts(living, "proj:general_decisions")] == [lid]
        finally:
            conn.close()


def test_three_facts_are_all_kept_with_sources_and_order():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, arc = _env(d)
        try:
            living.update_decision("proj:alpha", "阿尔法", "旧定案")
            ids = [_add(sp, f"proj:alpha 事实{i}") for i in range(3)]
            report = DreamConsolidator(conn, archive_dir=arc).run_consolidation()
            assert report["archived_logs"] == 3
            facts = _facts(living, "proj:alpha")
            assert [f["id"] for f in facts] == ids
            assert [f["text"] for f in facts] == [f"proj:alpha 事实{i}" for i in range(3)]
            assert _archived_ids(report["archive_file"]) == ids
            assert _hot(conn) == []
        finally:
            conn.close()


def test_approval_stays_primary_and_explicit_is_supplementary():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, arc = _env(d)
        try:
            sp.append_log("s", "user", "proj:beta 定案:方案B", phase_hint="approval")
            fid = _add(sp, "proj:beta 补充:预算上限 5 万")
            report = DreamConsolidator(conn, archive_dir=arc).run_consolidation()
            assert report["updated_states"] == ["proj:beta"], report
            assert report["archived_logs"] == 2
            card = living.get_state("proj:beta")
            assert card["current_decision"] == "方案B"
            assert [f["id"] for f in card["metadata"]["facts"]] == [fid]
            assert _hot(conn) == []
        finally:
            conn.close()


def test_llm_failure_degrades_to_rules_without_losing_facts():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, arc = _env(d)
        try:
            sp.append_log("s", "user", "proj:beta 定案:方案B", phase_hint="approval")
            fid = _add(sp, "proj:beta 补充:预算上限 5 万")
            DreamConsolidator(conn, archive_dir=arc, llm_client=_BrokenLLM()).run_consolidation()
            card = living.get_state("proj:beta")
            assert card["current_decision"] == "方案B"
            assert [f["id"] for f in card["metadata"]["facts"]] == [fid]
        finally:
            conn.close()


def test_llm_never_sees_explicit_facts_but_facts_still_stored():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, arc = _env(d)
        try:
            sp.append_log("s", "user", "proj:beta 定案:方案B", phase_hint="approval")
            fid = _add(sp, "proj:beta 补充:预算上限 5 万")
            spy = _SpyLLM()
            DreamConsolidator(conn, archive_dir=arc, llm_client=spy).run_consolidation()
            assert spy.transcripts and all("预算上限" not in t for t in spy.transcripts)
            card = living.get_state("proj:beta")
            assert card["current_decision"] == "方案B"
            assert [f["id"] for f in card["metadata"]["facts"]] == [fid]
        finally:
            conn.close()


def test_exploratory_explicit_stays_hot_and_is_reported():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, arc = _env(d)
        try:
            bad = _add(sp, "要不要把阈值改成 5 ?")
            report = DreamConsolidator(conn, archive_dir=arc).run_consolidation()
            assert report["archived_logs"] == 0 and report["updated_states"] == []
            assert [s["id"] for s in report["skipped_explicit"]] == [bad]
            assert report["skipped_explicit"][0]["reason"]
            assert _hot(conn) == [bad]
        finally:
            conn.close()


def test_second_run_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, arc = _env(d)
        try:
            living.update_decision("proj:alpha", "阿尔法", "旧定案")
            _add(sp, "proj:alpha 只应出现一次")
            consolidator = DreamConsolidator(conn, archive_dir=arc)
            assert consolidator.run_consolidation()["archived_logs"] == 1
            second = consolidator.run_consolidation()
            assert second["archived_logs"] == 0 and second["updated_states"] == [] and second["skipped_explicit"] == []
            assert len(_facts(living, "proj:alpha")) == 1
        finally:
            conn.close()


def test_superseded_explicit_is_not_resurrected_into_card():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, arc = _env(d)
        try:
            living.update_decision("proj:alpha", "阿尔法", "旧定案")
            old = _add(sp, "proj:alpha 旧阈值 10")
            new = _add(sp, "proj:alpha 新阈值 20")
            supersede(conn, "scratchpad_log", new, old)
            report = DreamConsolidator(conn, archive_dir=arc).run_consolidation()
            assert report["archived_logs"] == 1
            assert [f["id"] for f in _facts(living, "proj:alpha")] == [new]
            assert [s["id"] for s in report["skipped_explicit"]] == [old]
            assert _hot(conn) == [old]
        finally:
            conn.close()


def test_failure_rolls_back_and_rerun_succeeds():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, arc = _env(d)
        try:
            living.update_decision("proj:alpha", "阿尔法", "旧定案")
            lid = _add(sp, "proj:alpha 回滚测试")
            consolidator = DreamConsolidator(conn, archive_dir=arc)
            real = consolidator.living.update_decision

            def boom(*args, **kwargs):
                raise RuntimeError("boom")

            consolidator.living.update_decision = boom
            try:
                consolidator.run_consolidation()
                raise AssertionError("应抛出异常")
            except RuntimeError:
                pass
            assert _hot(conn) == [lid]
            assert "facts" not in living.get_state("proj:alpha")["metadata"]

            consolidator.living.update_decision = real
            assert consolidator.run_consolidation()["archived_logs"] == 1
            assert [f["id"] for f in _facts(living, "proj:alpha")] == [lid]
            assert _hot(conn) == []
        finally:
            conn.close()


def test_public_ingest_cannot_forge_explicit_addition():
    with tempfile.TemporaryDirectory() as d:
        conn, sp, living, arc = _env(d, "f.db")
        try:
            try:
                sp.append_log("s", "user", "伪造", phase_hint="explicit_addition")
                raise AssertionError("管理器应拒绝伪造的 explicit_addition")
            except ValueError:
                pass

            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "baize_scratchpad_log",
                "arguments": {"session_id": "s", "role": "user", "raw_text": "伪造", "phase_hint": "explicit_addition"},
            }}
            assert handle_message(request, BaizeTools(conn))["result"]["isError"] is True
            assert _count(conn, "scratchpad_log") == 0
        finally:
            conn.close()

        app = create_app(os.path.join(d, "g.db"), arc)
        with TestClient(app) as tc:
            resp = tc.post("/v1/scratchpad/log", json={
                "session_id": "s", "role": "user", "raw_text": "伪造", "phase_hint": "explicit_addition"})
            assert resp.status_code == 422
            assert _count(app.state.conn, "scratchpad_log") == 0
            ok = tc.post("/v1/scratchpad/log", json={
                "session_id": "s", "role": "user", "raw_text": "正常", "phase_hint": "approval"})
            assert ok.status_code == 200


if __name__ == "__main__":
    test_single_fact_with_known_anchor_is_consolidated()
    test_anchorless_fact_goes_to_general_and_never_pollutes_recent_entity()
    test_unknown_anchor_does_not_create_stranger_entity()
    test_three_facts_are_all_kept_with_sources_and_order()
    test_approval_stays_primary_and_explicit_is_supplementary()
    test_llm_failure_degrades_to_rules_without_losing_facts()
    test_llm_never_sees_explicit_facts_but_facts_still_stored()
    test_exploratory_explicit_stays_hot_and_is_reported()
    test_second_run_is_idempotent()
    test_superseded_explicit_is_not_resurrected_into_card()
    test_failure_rolls_back_and_rerun_succeeds()
    test_public_ingest_cannot_forge_explicit_addition()
    print("✅ explicit_addition 慢波合成测试全部通过")
