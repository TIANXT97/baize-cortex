"""Issue #44 PR4(A+B):judge_status 状态机、批量判引擎、candidate 候选流与回填工具"""
import json
import os
import tempfile

from baize.client.recall import extract_memories
from baize.core.consolidator import DreamConsolidator
from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import ScratchpadManager
from baize.intelligence.batch_judge import BatchJudgeRunner
from baize.intelligence.recall_router import CognitiveRecallRouter
from baize.storage.db import init_db


class _QueueLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, system, user):
        self.calls.append((system, user))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _env(d, llm_client=None):
    conn = init_db(os.path.join(d, "s.db"))
    return conn, DreamConsolidator(conn, archive_dir=os.path.join(d, "arc"), llm_client=llm_client)


def _verdict(row_id, phase, noul, confidence):
    return json.dumps(
        {"id": row_id, "phase": phase, "noul": noul, "confidence": confidence}, ensure_ascii=False
    )


def test_append_log_judge_status_state_machine():
    with tempfile.TemporaryDirectory() as d:
        conn, _ = _env(d)
        try:
            os.environ["CORTEX_BATCH_JUDGE"] = "on"
            try:
                sp = ScratchpadManager(conn)
                plain = sp.append_log("s", "user", "随便聊聊")
                assistant = sp.append_log("s", "assistant", "好的主人")
                approval = sp.append_log("s", "user", "就这么定", phase_hint="approval")
                explicit = sp.add_explicit("s", "记住这条", "fact", "r", "k1", None)
                rows = dict(conn.execute(
                    "SELECT id, judge_status FROM scratchpad_log"
                ).fetchall())
                assert rows[plain] == "pending"
                assert rows[assistant] == "done"
                assert rows[approval] == "done"
                assert rows[explicit] == "done"
            finally:
                del os.environ["CORTEX_BATCH_JUDGE"]
            # 开关关闭时普通行保持 NULL(历史同语义,可被默认 purge 清退)
            off = ScratchpadManager(conn).append_log("s", "user", "管线关闭期")
            assert conn.execute(
                "SELECT judge_status FROM scratchpad_log WHERE id = ?", (off,)
            ).fetchone()[0] is None
        finally:
            conn.close()


def test_batch_judge_confidence_gates_and_candidate_channels():
    with tempfile.TemporaryDirectory() as d:
        conn, _ = _env(d)
        try:
            os.environ["CORTEX_BATCH_JUDGE"] = "on"
            try:
                sp = ScratchpadManager(conn)
                ids = [
                    sp.append_log("s1", "user", "确认同意,按方案A执行"),       # 高置信定案 → approval
                    sp.append_log("s1", "user", "下周打算把机房迁到新加坡"),     # 弱信号计划 → candidate
                    sp.append_log("s1", "user", "我的服务器IP是101.200.45.220"),  # 高 noul 事实 → candidate
                    sp.append_log("s1", "user", "今天天气不错"),                 # 闲聊 → 不打标
                    sp.append_log("s2", "user", "不对,昨天那个结论记错了,作废"),  # 高置信推翻 → rejection
                ]
            finally:
                del os.environ["CORTEX_BATCH_JUDGE"]
            llm = _QueueLLM(["[" + ",".join([
                _verdict(ids[0], "decision_approved", 0.03, 0.99),
                _verdict(ids[1], "decision_approved", 0.36, 0.60),
                _verdict(ids[2], "exploration", 0.98, 0.5),
                _verdict(ids[3], "chitchat", 0.01, 0.9),
            ]) + "]", "[" + _verdict(ids[4], "rejection", 0.96, 0.99) + "]"])
            report = BatchJudgeRunner(conn, llm, batch_size=4).run()

            assert report["approved"] == 1 and report["rejected"] == 1
            assert report["candidates"] == 2 and report["plain"] == 1 and report["batches"] == 2
            state = {
                r[0]: r[1:] for r in conn.execute(
                    "SELECT id, phase_hint, judge_status, noul_score FROM scratchpad_log"
                ).fetchall()
            }
            assert state[ids[0]][0] == "approval" and state[ids[0]][1] == "done"
            assert state[ids[4]][0] == "rejection" and state[ids[4]][1] == "done"
            assert state[ids[1]][0] == "candidate" and state[ids[2]][0] == "candidate"
            assert abs(state[ids[2]][2] - 0.98) < 1e-9
            assert state[ids[3]][0] is None and state[ids[3]][1] == "done"
            # 批次不跨会话:两次调用分别是 s1 的 4 条与 s2 的 1 条
            assert json.loads(llm.calls[0][1]) == [
                {"id": ids[0], "text": "确认同意,按方案A执行"},
                {"id": ids[1], "text": "下周打算把机房迁到新加坡"},
                {"id": ids[2], "text": "我的服务器IP是101.200.45.220"},
                {"id": ids[3], "text": "今天天气不错"},
            ]
        finally:
            conn.close()


def test_batch_judge_retry_then_failed_terminal():
    with tempfile.TemporaryDirectory() as d:
        conn, _ = _env(d)
        try:
            os.environ["CORTEX_BATCH_JUDGE"] = "on"
            try:
                row_id = ScratchpadManager(conn).append_log("s", "user", "待判文本")
            finally:
                del os.environ["CORTEX_BATCH_JUDGE"]
            llm = _QueueLLM([ValueError("坏 JSON"), ValueError("坏 JSON"), ValueError("坏 JSON")])
            for _ in range(3):
                BatchJudgeRunner(conn, llm, batch_size=10, max_retry=3).run()
            row = conn.execute(
                "SELECT judge_status, retry_count FROM scratchpad_log WHERE id = ?", (row_id,)
            ).fetchone()
            assert row == ("failed", 3)
            # failed 终态不再被捞取,也不再进第 5 层召回
            assert ScratchpadManager(conn).fetch_pending_judgement() == []
            assert conn.execute("SELECT 1 FROM scratchpad_log WHERE judge_status = 'pending'").fetchone() is None
        finally:
            conn.close()


def test_consolidation_verifies_and_folds_candidates_without_supersede():
    with tempfile.TemporaryDirectory() as d:
        llm = _QueueLLM([
            json.dumps({"is_fact": True, "entity": "proj:x"}, ensure_ascii=False),
            json.dumps({"is_fact": False, "entity": ""}, ensure_ascii=False),
        ])
        conn, consolidator = _env(d, llm_client=llm)
        try:
            LivingStateManager(conn).update_decision("proj:x", "X卡", "现役定案")
            sp = ScratchpadManager(conn)
            good = sp.append_log("s", "user", "我的服务器IP是101.200.45.220")
            sp.mark_judged(good, "candidate", 0.98, "batch_judge")
            bad = sp.append_log("s", "user", "下周打算把机房迁到新加坡")
            sp.mark_judged(bad, "candidate", 0.36, "batch_judge")
            conn.commit()

            report = consolidator.run_consolidation()
            cand = report["candidates"]
            assert cand["verified"] == 1 and len(cand["rejected"]) == 1 and cand["skipped"] == []
            assert report["archived_logs"] == 2

            state = LivingStateManager(conn).get_state("proj:x")
            assert state["current_decision"] == "现役定案"
            assert state["version"] == 2  # facts-only 折叠算一次卡片更新,但不改写定案
            facts = state["metadata"]["facts"]
            assert len(facts) == 1 and facts[0]["text"] == "我的服务器IP是101.200.45.220"
            assert abs(facts[0]["noul"] - 0.98) < 1e-9
            # 红线1:严禁参与定案改写——无 decided_as 新边、无墓碑
            assert conn.execute(
                "SELECT COUNT(*) FROM graph_edges WHERE rel_type = 'decided_as'"
            ).fetchone()[0] == 0
            assert state["tombstones"] == []
            assert conn.execute("SELECT COUNT(*) FROM scratchpad_log").fetchone()[0] == 0
        finally:
            conn.close()


def test_consolidation_without_llm_leaves_candidates_skipped():
    with tempfile.TemporaryDirectory() as d:
        conn, consolidator = _env(d, llm_client=None)
        try:
            sp = ScratchpadManager(conn)
            row_id = sp.append_log("s", "user", "我的服务器IP是101.200.45.220")
            sp.mark_judged(row_id, "candidate", 0.98, "batch_judge")
            conn.commit()
            report = consolidator.run_consolidation()
            assert report["candidates"]["skipped"][0]["id"] == row_id
            assert conn.execute("SELECT COUNT(*) FROM scratchpad_log").fetchone()[0] == 1
        finally:
            conn.close()


def test_batch_judge_step_respects_switch():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "s.db"))
        try:
            os.environ["CORTEX_BATCH_JUDGE"] = "on"
            try:
                sp = ScratchpadManager(conn)
                row_id = sp.append_log("s", "user", "待判")
                llm = _QueueLLM(["[" + _verdict(row_id, "chitchat", 0.01, 0.9) + "]"])
                consolidator = DreamConsolidator(
                    conn, archive_dir=os.path.join(d, "arc"), llm_client=llm
                )
                report = consolidator._batch_judge_step()
                assert report["batches"] == 1 and report["plain"] == 1 and report["judged"] == 1
                assert DreamConsolidator(
                    conn, archive_dir=os.path.join(d, "arc"), llm_client=None
                )._batch_judge_step() == {"status": "skipped_no_llm"}
            finally:
                del os.environ["CORTEX_BATCH_JUDGE"]
            no_llm = DreamConsolidator(conn, archive_dir=os.path.join(d, "arc"), llm_client=None)
            assert no_llm._batch_judge_step() == {"status": "disabled"}
        finally:
            conn.close()


def test_slice_tolerates_null_session_rows():
    # Issue #56 P0-1:ORDER BY session_id 把 NULL 排最前,首行 NULL 曾触发 batches[-1] IndexError
    runner = BatchJudgeRunner.__new__(BatchJudgeRunner)
    runner.batch_size = 10
    both_null = runner._slice([
        {"session_id": None, "id": 1, "raw_text": "a"},
        {"session_id": None, "id": 2, "raw_text": "b"},
    ])
    assert [[r["id"] for r in b] for b in both_null] == [[1, 2]]
    mixed = runner._slice([
        {"session_id": None, "id": 1, "raw_text": "a"},
        {"session_id": "s1", "id": 2, "raw_text": "b"},
        {"session_id": "s1", "id": 3, "raw_text": "c"},
    ])
    assert [[r["id"] for r in b] for b in mixed] == [[1], [2, 3]]


def test_batch_judge_step_failure_does_not_block_consolidation():
    # Issue #56 P0-2:增强项异常不得击杀慢波主折叠
    with tempfile.TemporaryDirectory() as d:
        conn, consolidator = _env(d, llm_client=_QueueLLM([]))
        original_run = BatchJudgeRunner.run

        def _boom(self):
            raise RuntimeError("注入故障")

        os.environ["CORTEX_BATCH_JUDGE"] = "on"
        BatchJudgeRunner.run = _boom
        try:
            ScratchpadManager(conn).append_log("s", "user", "proj:x 定案:方案B", phase_hint="approval")
            report = consolidator.run_consolidation()
            assert report["status"] == "ok"
            assert report["batch_judge"]["status"] == "failed"
            assert "注入故障" in report["batch_judge"]["error"]
            assert report["archived_logs"] == 1
            assert LivingStateManager(conn).get_state("proj:x")["current_decision"] == "方案B"
        finally:
            BatchJudgeRunner.run = original_run
            del os.environ["CORTEX_BATCH_JUDGE"]
            conn.close()


def test_candidate_recall_channel_is_lowest_authority_fallback():
    with tempfile.TemporaryDirectory() as d:
        conn, _ = _env(d)
        try:
            sp = ScratchpadManager(conn)
            cand = sp.append_log("s", "user", "我的服务器IP是101.200.45.220,5台4C8G")
            sp.mark_judged(cand, "candidate", 0.98, "batch_judge")
            router = CognitiveRecallRouter(conn)
            out = router.recall("服务器IP")
            assert out["strategy"] == "CANDIDATE_UNVERIFIED", out
            assert out["confidence"] == 0.45 and out["unverified"] is True
            assert out["provisional"] is True and out["authoritative"] is False
            assert any("服务器IP" in f["text"] for f in out["facts"])

            # explicit 优先于 candidate
            sp.add_explicit("s", "服务器IP是127.0.0.1", "fact", "r", "k-e", None)
            out2 = router.recall("服务器IP")
            assert out2["strategy"] == "SCRATCHPAD_EXPLICIT_FALLBACK", out2

            # 客户端注入标注「未核实」
            items = extract_memories({
                "strategy": "CANDIDATE_UNVERIFIED", "facts": [{"id": cand, "text": "我的服务器IP..."}]
            })
            assert items and items[0]["kind"] == "candidate·未核实"
        finally:
            conn.close()


def test_purge_protects_pending_and_backfill_requires_window():
    with tempfile.TemporaryDirectory() as d:
        conn, consolidator = _env(d)
        try:
            os.environ["CORTEX_BATCH_JUDGE"] = "on"
            try:
                pending = ScratchpadManager(conn).append_log("s", "user", "待判文本")
            finally:
                del os.environ["CORTEX_BATCH_JUDGE"]
            dry = consolidator.purge_scratchpad(role="user", dry_run=True)
            assert dry["matched"] == 0 and dry["protected_pending"] == 1

            # 模拟历史双 NULL 行(管线开启前写入的存量),回填只针对这类行
            conn.execute(
                "INSERT INTO scratchpad_log (session_id, role, raw_text, created_at) "
                "VALUES ('s', 'user', '历史漏判行', '2026-10-23 12:00:00')"
            )
            conn.commit()

            # 回填必须带时间窗,防全量存量风暴
            try:
                ScratchpadManager(conn).backfill_judge(dry_run=True)
                raise AssertionError("无窗口应拒绝")
            except ValueError:
                pass
            listing = ScratchpadManager(conn).backfill_judge(after_date="2026-10-23 00:00:00", dry_run=True)
            assert listing["dry_run"] is True and listing["ids"] and pending not in listing["ids"]
            queued = ScratchpadManager(conn).backfill_judge(after_date="2026-10-23 00:00:00", dry_run=False)
            assert queued["queued"] == len(listing["ids"])
            statuses = dict(conn.execute("SELECT id, judge_status FROM scratchpad_log").fetchall())
            assert statuses[pending] == "pending" and set(statuses.values()) == {"pending"}
            # 回填后 purge 豁免随之扩大
            dry2 = consolidator.purge_scratchpad(role="user", dry_run=True)
            assert dry2["matched"] == 0 and dry2["protected_pending"] == 2
        finally:
            conn.close()


if __name__ == "__main__":
    test_append_log_judge_status_state_machine()
    test_batch_judge_confidence_gates_and_candidate_channels()
    test_batch_judge_retry_then_failed_terminal()
    test_consolidation_verifies_and_folds_candidates_without_supersede()
    test_consolidation_without_llm_leaves_candidates_skipped()
    test_batch_judge_step_respects_switch()
    test_slice_tolerates_null_session_rows()
    test_batch_judge_step_failure_does_not_block_consolidation()
    test_candidate_recall_channel_is_lowest_authority_fallback()
    test_purge_protects_pending_and_backfill_requires_window()
    print("✅ 批量判研与 candidate 候选流测试全部通过")
