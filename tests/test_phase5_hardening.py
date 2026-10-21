"""Phase 5 加固专项测试:归档唯一性、草稿保留、授权门、别名保护、未来边隔离、模型配置"""
import gzip
import json
import os
import tempfile

from baize.core.consolidator import DreamConsolidator
from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import ScratchpadManager
from baize.core.temporal_graph import BiTemporalGraphManager
from baize.intelligence.phase_detector import PhaseDetector, llm_settings_from_env
from baize.storage.db import init_db


def _read_archive(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line)["raw_text"] for line in fh if line.strip()]


def _hot_rows(conn):
    return [r[0] for r in conn.execute("SELECT raw_text FROM scratchpad_log ORDER BY id")]


def test_consecutive_archives_are_unique_and_lossless():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "u.db"))
        try:
            sp = ScratchpadManager(conn)
            archive = os.path.join(d, "arc")
            sp.append_log("s", "user", "proj:a 定案:方案1", phase_hint="approval")
            first = DreamConsolidator(conn, archive_dir=archive).run_consolidation()
            sp.append_log("s", "user", "proj:a 定案:方案2", phase_hint="approval")
            second = DreamConsolidator(conn, archive_dir=archive).run_consolidation()

            assert first["archive_file"] != second["archive_file"]
            assert _read_archive(first["archive_file"]) == ["proj:a 定案:方案1"]
            assert _read_archive(second["archive_file"]) == ["proj:a 定案:方案2"]
        finally:
            conn.close()


def test_unfolded_drafts_survive_archive():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "k.db"))
        try:
            sp = ScratchpadManager(conn)
            sp.append_log("s", "user", "随手记的排障碎屑")
            sp.append_log("s", "user", "无实体的定案:X", phase_hint="approval")
            sp.append_log("s", "user", "proj:b 定案:Y", phase_hint="approval")
            report = DreamConsolidator(conn, archive_dir=os.path.join(d, "a")).run_consolidation()

            assert report["archived_logs"] == 2
            assert _hot_rows(conn) == ["随手记的排障碎屑"]
        finally:
            conn.close()


def test_assistant_and_hypothetical_cannot_overwrite_decision():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "g.db"))
        try:
            LivingStateManager(conn).update_decision("proj:c", "C", "方案A")
            sp = ScratchpadManager(conn)
            sp.append_log("s", "assistant", "proj:c 定案:方案B", phase_hint="approval")
            sp.append_log("s", "user", "proj:c 如果定案:方案C?", phase_hint="approval")
            sp.append_log("s", "user", "proj:c 探讨 定案:方案D", phase_hint="exploration")
            report = DreamConsolidator(conn, archive_dir=os.path.join(d, "a")).run_consolidation()

            assert report["status"] == "ok"
            assert report["updated_states"] == []
            assert LivingStateManager(conn).get_state("proj:c")["current_decision"] == "方案A"
        finally:
            conn.close()


def test_decision_update_keeps_aliases_and_rewires_graph():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "r.db"))
        try:
            living = LivingStateManager(conn)
            graph = BiTemporalGraphManager(conn)
            living.update_decision("proj:d", "D", "旧方案", metadata={"aliases": ["模具别名"]})
            graph.upsert_node("proj:d", "D", "project")
            graph.upsert_node("plan:old", "旧方案", "rule")
            graph.add_edge("proj:d", "plan:old", "decided_as", valid_at="2000-01-01 00:00:00")

            ScratchpadManager(conn).append_log("s", "user", "proj:d 定案:新方案", phase_hint="approval")
            DreamConsolidator(conn, archive_dir=os.path.join(d, "a")).run_consolidation()

            state = living.get_state("proj:d")
            assert state["current_decision"] == "新方案"
            assert state["metadata"]["aliases"] == ["模具别名"]

            active = graph.get_active_subgraph()
            assert not any(s == "proj:d" and dst == "plan:old" for s, dst, _ in active)
            assert any(s == "proj:d" and dst != "plan:old" for s, dst, _ in active)
        finally:
            conn.close()


def test_future_edges_excluded_from_active_subgraph():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "f.db"))
        try:
            graph = BiTemporalGraphManager(conn)
            graph.upsert_node("proj:e", "E", "project")
            graph.upsert_node("plan:future", "F", "rule")
            graph.add_edge("proj:e", "plan:future", "decided_as", valid_at="2999-01-01")
            assert not any(dst == "plan:future" for _, dst, _ in graph.get_active_subgraph())
        finally:
            conn.close()


def test_busy_timeout_configured():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "b.db"))
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        finally:
            conn.close()


def test_llm_env_settings_and_local_fallback():
    keys = ("CORTEX_LLM_URL", "CORTEX_LLM_KEY", "CORTEX_REASONING_MODEL")
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        assert llm_settings_from_env() is None

        os.environ["CORTEX_LLM_URL"] = "http://127.0.0.1:9/v1/chat/completions"
        os.environ["CORTEX_LLM_KEY"] = "k"
        url, key, reasoning_model = llm_settings_from_env()
        assert reasoning_model == "cbcn/deepseek-v4.1-flash"
    finally:
        for k in keys:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


if __name__ == "__main__":
    test_consecutive_archives_are_unique_and_lossless()
    test_unfolded_drafts_survive_archive()
    test_assistant_and_hypothetical_cannot_overwrite_decision()
    test_decision_update_keeps_aliases_and_rewires_graph()
    test_future_edges_excluded_from_active_subgraph()
    test_busy_timeout_configured()
    test_llm_env_settings_and_local_fallback()
    print("✅ Phase 5 加固测试全部通过")
