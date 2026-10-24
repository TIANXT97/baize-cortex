"""Phase 6 专项测试:别名行折叠、LLM 展示名与别名沉淀、决策清洗、自然语言召回直达"""
import gzip
import json
import os
import tempfile

from baize.core.consolidator import DreamConsolidator
from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import ScratchpadManager
from baize.intelligence.recall_router import CognitiveRecallRouter
from baize.storage.db import init_db


class _FakeLLM:
    def chat(self, system, user):
        return json.dumps({
            "decision": ":transport 固定为 edit",
            "superseded": None,
            "display_name": "飞书流式卡片",
            "aliases": ["流式卡片", "飞书卡片"],
        }, ensure_ascii=False)


def test_alias_line_folds_into_metadata_and_strips_colon():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "a.db"))
        try:
            ScratchpadManager(conn).append_log(
                "s", "user", "proj:feishu_streaming alias:流式卡片,飞书卡片,卡片 定案::transport 固定为 edit",
                phase_hint="approval",
            )
            report = DreamConsolidator(conn, archive_dir=os.path.join(d, "arc")).run_consolidation()
            assert report["updated_states"] == ["proj:feishu_streaming"]

            state = LivingStateManager(conn).get_state("proj:feishu_streaming")
            assert state["metadata"]["aliases"] == ["流式卡片", "飞书卡片", "卡片"]
            assert state["current_decision"] == "transport 固定为 edit"
        finally:
            conn.close()


def test_llm_extracts_display_name_and_aliases_and_keeps_existing():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "l.db"))
        try:
            living = LivingStateManager(conn)
            living.update_decision("proj:feishu_streaming", "proj:feishu_streaming", "旧",
                                   metadata={"aliases": ["老别名"]})
            ScratchpadManager(conn).append_log(
                "s", "user", "proj:feishu_streaming 定案:transport 固定为 edit", phase_hint="approval",
            )
            DreamConsolidator(conn, archive_dir=os.path.join(d, "arc"), llm_client=_FakeLLM()).run_consolidation()

            state = living.get_state("proj:feishu_streaming")
            assert state["display_name"] == "飞书流式卡片"
            assert state["current_decision"] == "transport 固定为 edit"
            assert state["metadata"]["aliases"] == ["老别名", "流式卡片", "飞书卡片"]
        finally:
            conn.close()


def test_natural_language_recall_hits_alias_directly():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "r.db"))
        try:
            ScratchpadManager(conn).append_log(
                "s", "user", "proj:feishu_streaming alias:流式卡片,飞书卡片 定案:transport 固定为 edit",
                phase_hint="approval",
            )
            DreamConsolidator(conn, archive_dir=os.path.join(d, "arc")).run_consolidation()

            out = CognitiveRecallRouter(conn).recall("流式卡片 transport 定的什么")
            assert out["strategy"] == "LIVING_STATE_DIRECT"
            assert out["state"]["entity_id"] == "proj:feishu_streaming"
            assert out["state"]["current_decision"] == "transport 固定为 edit"
        finally:
            conn.close()


def test_decision_text_fallback_when_no_alias():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "f.db"))
        try:
            LivingStateManager(conn).update_decision("proj:x", "X", "流式卡片使用 transport 固定为 edit")
            out = CognitiveRecallRouter(conn).recall("transport 定的什么")
            assert out["strategy"] == "DECISION_TEXT_MATCH"
            assert out["state"]["entity_id"] == "proj:x"
        finally:
            conn.close()


def test_generic_words_are_blacklisted_from_alias_library():
    # Issue #44 CTO 红线:纯通用词不得入库,已入库的由慢波下次触碰该卡时清洗
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "g.db"))
        try:
            living = LivingStateManager(conn)
            living.update_decision("proj:x", "X", "旧定案", metadata={"aliases": ["记忆", "老别名"]})
            ScratchpadManager(conn).append_log(
                "s", "user", "proj:x alias:方案,模具知识库 定案:新定案", phase_hint="approval",
            )
            DreamConsolidator(conn, archive_dir=os.path.join(d, "arc")).run_consolidation()
            state = living.get_state("proj:x")
            assert state["metadata"]["aliases"] == ["老别名", "模具知识库"]
        finally:
            conn.close()


if __name__ == "__main__":
    test_alias_line_folds_into_metadata_and_strips_colon()
    test_llm_extracts_display_name_and_aliases_and_keeps_existing()
    test_natural_language_recall_hits_alias_directly()
    test_decision_text_fallback_when_no_alias()
    test_generic_words_are_blacklisted_from_alias_library()
    print("✅ Phase 6 语义别名专项测试全部通过")
