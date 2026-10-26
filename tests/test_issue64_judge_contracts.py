"""Issue #64:模型坏响应不能污染记忆或绕过失败终态。"""
import json
import tempfile

from baize.client.recall import extract_memories
from baize.core.consolidator import DreamConsolidator
from baize.core.scratchpad import ScratchpadManager
from baize.intelligence.batch_judge import BatchJudgeRunner
from baize.intelligence.recall_router import CognitiveRecallRouter
from baize.storage.db import init_db


class _Reply:
    def __init__(self, value):
        self.value = value

    def chat(self, system, user):
        return json.dumps(self.value)


def test_malformed_scores_reach_failed_without_partial_batch_writes():
    for invalid in ("invalid", None, True, -0.1, 1.1, float("nan"), float("inf")):
        for field in ("confidence", "noul"):
            conn = init_db(":memory:")
            try:
                sp = ScratchpadManager(conn)
                ids = [sp.append_log("s", "user", f"待判内容{i}") for i in range(2)]
                conn.execute("UPDATE scratchpad_log SET judge_status = 'pending'")
                conn.commit()
                values = [
                    {"id": rid, "phase": "decision_approved", "confidence": 0.99, "noul": 0.1}
                    for rid in ids
                ]
                values[-1][field] = invalid
                runner = BatchJudgeRunner(conn, _Reply(values))
                for _ in range(3):
                    report = runner.run()
                    assert report["failed_batches"] == 1, (field, invalid, report)
                assert conn.execute(
                    "SELECT phase_hint, judge_status, retry_count FROM scratchpad_log ORDER BY id"
                ).fetchall() == [(None, "failed", 3), (None, "failed", 3)]
                assert runner.run()["batches"] == 0
            finally:
                conn.close()


def test_batch_ids_are_unique_integers_and_response_order_is_irrelevant():
    batch = [{"id": 1, "raw_text": "甲"}, {"id": 2, "raw_text": "乙"}]
    one = {"id": 1, "phase": "chitchat", "noul": 0.1, "confidence": 0.9}
    two = {**one, "id": 2}
    runner = BatchJudgeRunner(None, _Reply([two, one]))
    assert [row["id"] for row in runner._judge_batch(batch)] == [1, 2]
    for values in ([one, one], [{**one, "id": True}, two], [{**one, "id": 1.0}, two]):
        try:
            BatchJudgeRunner(None, _Reply(values))._judge_batch(batch)
        except ValueError:
            pass
        else:
            raise AssertionError(f"非法 ID 被接受: {values}")


def test_candidate_verification_requires_json_boolean():
    for invalid in ("false", "true", 0, 1, None):
        conn = init_db(":memory:")
        try:
            for kind, key in (("fact", "is_fact"), ("decision", "is_decision")):
                with tempfile.TemporaryDirectory() as directory:
                    sp = ScratchpadManager(conn)
                    row_id = sp.append_log("s", "user", "待核实的长期记录", "candidate", 0.95, category=kind)
                    consolidator = DreamConsolidator(
                        conn, archive_dir=directory,
                        llm_client=_Reply({key: invalid, "subject": "某项方案", "entity": ""}),
                    )
                    report = consolidator.run_consolidation()
                    assert any(row["id"] == row_id for row in report["candidates"]["skipped"])
                    assert report["archived_logs"] == 0
                    assert conn.execute("SELECT COUNT(*) FROM living_states").fetchone()[0] == 0
        finally:
            conn.close()


def test_full_candidate_response_preserves_unverified_label():
    conn = init_db(":memory:")
    try:
        ScratchpadManager(conn).append_log(
            "s", "user", "服务器IP是192.0.2.1", "candidate", 0.95, category="fact"
        )
        response = CognitiveRecallRouter(conn).recall("服务器IP")
        assert response["strategy"] == "CANDIDATE_UNVERIFIED"
        items = extract_memories(response)
        assert len(items) == 1
        assert items[0]["kind"] == "candidate·未核实"
        assert items[0]["provisional"] is True
    finally:
        conn.close()


if __name__ == "__main__":
    test_malformed_scores_reach_failed_without_partial_batch_writes()
    test_batch_ids_are_unique_integers_and_response_order_is_irrelevant()
    test_candidate_verification_requires_json_boolean()
    test_full_candidate_response_preserves_unverified_label()
    print("Issue #64 模型响应与候选注入契约测试通过")
