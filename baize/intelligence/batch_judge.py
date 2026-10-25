"""Issue #44 PR4(B 项):夜间批量判研引擎 —— System 2 按会话窗口切片,状态机防死循环。

红线 2 落地:judge_status 显式状态机取代 IS NULL 裸奔;批次 ID/条数严格校验;
失败重试有上限,failed 终态不参与任何召回层。approval 走 confidence 双门槛
(noul 与「是否定案」反相关,不能当门槛——美西实测真定案 noul 0.004~0.128)。
"""
import json
from typing import Any, Dict, List

from baize.core.scratchpad import CANDIDATE_DECISION, CANDIDATE_FACT, ScratchpadManager
from baize.intelligence.phase_detector import PHASE_CATEGORY

BATCH_SIZE = 10
MAX_TEXT_CHARS = 300
MAX_RETRY = 3
APPROVAL_CONFIDENCE_GATE = 0.9
CANDIDATE_NOUL_GATE = 0.8

_SYSTEM_PROMPT = (
    "你是相态判别器。对给定的对话记录数组,逐条判定 phase "
    "(decision_approved / rejection / exploration / chitchat)、noul(0~1,信息价值) "
    "与 confidence(0~1,分类置信度)。只输出 JSON 数组,元素形如 "
    '{"id": 记录id, "phase": "...", "noul": 0.0, "confidence": 0.0},'
    "id 与条数必须与输入完全一致。"
)


class BatchJudgeRunner:
    def __init__(
        self,
        conn,
        llm_client: Any,
        batch_size: int = BATCH_SIZE,
        max_retry: int = MAX_RETRY,
    ):
        self.conn = conn
        self.llm_client = llm_client
        self.batch_size = batch_size
        self.max_retry = max_retry
        self.scratchpad = ScratchpadManager(conn)

    def run(self) -> Dict[str, Any]:
        rows = self.scratchpad.fetch_pending_judgement(max_retry=self.max_retry)
        report: Dict[str, Any] = {
            "batches": 0, "judged": 0, "approved": 0, "rejected": 0,
            "candidates": 0, "plain": 0, "failed_batches": 0,
        }
        for batch in self._slice(rows):
            report["batches"] += 1
            try:
                verdicts = self._judge_batch(batch)
            except Exception:
                report["failed_batches"] += 1
                self.scratchpad.mark_judge_retry([r["id"] for r in batch], self.max_retry)
                self.conn.commit()
                continue
            self._apply(batch, verdicts, report)
            self.conn.commit()
        return report

    def _slice(self, rows: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        # 按会话分组、组内时间正序,批次不跨会话(缓解批次锚定效应)
        batches: List[List[Dict[str, Any]]] = []
        current_session = None
        for row in rows:
            if not batches or row["session_id"] != current_session or len(batches[-1]) >= self.batch_size:
                batches.append([])
                current_session = row["session_id"]
            batches[-1].append(row)
        return batches

    def _judge_batch(self, batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        payload = [
            {"id": row["id"], "text": (row["raw_text"] or "")[:MAX_TEXT_CHARS]} for row in batch
        ]
        raw = self.llm_client.chat(_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False))
        verdicts = json.loads(raw)
        if not isinstance(verdicts, list) or len(verdicts) != len(batch):
            raise ValueError("批次返回条数与输入不一致")
        expected_ids = {row["id"] for row in batch}
        by_id: Dict[int, Dict[str, Any]] = {}
        for verdict in verdicts:
            if not isinstance(verdict, dict) or verdict.get("id") not in expected_ids:
                raise ValueError("批次返回 ID 与输入不一致")
            if verdict.get("phase") not in PHASE_CATEGORY:
                raise ValueError(f"未知 phase: {verdict.get('phase')}")
            by_id[verdict["id"]] = verdict
        return [by_id[row["id"]] for row in batch]

    def _apply(self, batch: List[Dict[str, Any]], verdicts: List[Dict[str, Any]], report: Dict[str, Any]) -> None:
        for row, verdict in zip(batch, verdicts):
            noul = float(verdict.get("noul") or 0.0)
            confidence = float(verdict.get("confidence") or 0.0)
            phase = verdict["phase"]
            if phase in ("decision_approved", "rejection") and confidence >= APPROVAL_CONFIDENCE_GATE:
                hint = "approval" if phase == "decision_approved" else "rejection"
                self.scratchpad.mark_judged(row["id"], hint, noul, "batch_judge")
                report["approved" if phase == "decision_approved" else "rejected"] += 1
            elif phase == "decision_approved" and confidence < APPROVAL_CONFIDENCE_GATE:
                # Issue #56 P1-3:弱信号定案标 decision,夜间按「是否正式定案」核实而非事实标准
                self.scratchpad.mark_judged(row["id"], "candidate", noul, "batch_judge", CANDIDATE_DECISION)
                report["candidates"] += 1
            elif phase in ("exploration", "chitchat") and noul >= CANDIDATE_NOUL_GATE:
                self.scratchpad.mark_judged(row["id"], "candidate", noul, "batch_judge", CANDIDATE_FACT)
                report["candidates"] += 1
            else:
                self.scratchpad.mark_judged(row["id"], None, noul, "batch_judge")
                report["plain"] += 1
            report["judged"] += 1
