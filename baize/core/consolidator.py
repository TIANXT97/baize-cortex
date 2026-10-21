"""Layer 3: 慢波合成睡眠引擎 (Dream Consolidation)

夜间折叠相变候选、就地固化活体卡、切断旧图谱边,并将热草稿无损冷归档。
"""
import argparse
import gzip
import json
import os
import re
import sqlite3
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import ScratchpadManager
from baize.core.temporal_graph import BiTemporalGraphManager
from baize.storage.db import init_db

_ENTITY_RE = re.compile(r"(proj:[\w\-]+)")
_DECISION_RE = re.compile(r"(?:定案|改为|敲定)[:：]?\s*(.+)")
_SUPERSEDE_RE = re.compile(r"(?:推翻|废弃|取代)[:：]?\s*(.+?)(?:[，,。；;]|$)|(.+?)\s*(?:推翻|废弃|取代)")


class DreamConsolidator:
    def __init__(
        self,
        conn: sqlite3.Connection,
        archive_dir: str = "cold_archive",
        llm_client: Optional[Any] = None,
    ):
        self.conn = conn
        self.archive_dir = archive_dir
        self.llm_client = llm_client
        self.scratchpad = ScratchpadManager(conn)
        self.living = LivingStateManager(conn)
        self.graph = BiTemporalGraphManager(conn)

    def run_consolidation(self, target_date: Optional[str] = None) -> Dict[str, Any]:
        phase_logs = self.scratchpad.export_pending_phase_logs(target_date)
        updated_states = self._fold_phases(phase_logs)
        archive_file, archived = self._archive_and_purge(target_date)
        return {
            "status": "ok",
            "archived_logs": archived,
            "updated_states": updated_states,
            "archive_file": archive_file,
        }

    def _fold_phases(self, phase_logs: List[Dict[str, Any]]) -> List[str]:
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for log in phase_logs:
            entity = self._extract_entity(log["raw_text"])
            if entity:
                groups[entity].append(log)

        updated: List[str] = []
        for entity_id, logs in groups.items():
            decision, superseded = self._consolidate_group(logs)
            if not decision:
                continue
            current = self.living.get_state(entity_id)
            display = current["display_name"] if current else entity_id
            self.living.update_decision(
                entity_id,
                display,
                decision,
                superseded_plan=superseded,
                killed_reason="睡眠合成:被新定案取代" if superseded else None,
            )
            if superseded:
                self._retire_plan_edges(entity_id, superseded)
            updated.append(entity_id)
        return updated

    def _consolidate_group(self, logs: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
        if self.llm_client is not None:
            try:
                return self._consolidate_with_llm(logs)
            except Exception:
                pass
        return self._consolidate_by_rules(logs)

    def _consolidate_with_llm(self, logs: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
        transcript = "\n".join(log["raw_text"] for log in logs)
        raw = self.llm_client.chat(
            "从对话中提取最终定案 decision 与被推翻的方案 superseded,只输出 JSON。",
            transcript,
        )
        data = json.loads(raw)
        return data.get("decision") or None, data.get("superseded") or None

    def _consolidate_by_rules(self, logs: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
        decision = None
        superseded = None
        for log in logs:
            text = log["raw_text"]
            m = _DECISION_RE.search(text)
            if m:
                decision = m.group(1).strip()
            s = _SUPERSEDE_RE.search(text)
            if s:
                superseded = (s.group(1) or s.group(2)).strip()
        return decision, superseded

    def _extract_entity(self, text: str) -> Optional[str]:
        m = _ENTITY_RE.search(text)
        return m.group(1) if m else None

    def _retire_plan_edges(self, entity_id: str, superseded_plan: str) -> None:
        now = self._now()
        rows = self.conn.execute(
            """
            SELECT src_id, dst_id, rel_type FROM v_active_graph_edges
            WHERE src_id = ? OR dst_id = ?
            """,
            (entity_id, entity_id),
        ).fetchall()
        for src, dst, rel in rows:
            other = dst if src == entity_id else src
            if superseded_plan in other:
                self.graph.invalidate_edge(src, dst, rel, now)

    def _now(self) -> str:
        return self.conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now')").fetchone()[0]

    def _archive_and_purge(self, target_date: Optional[str]) -> Tuple[Optional[str], int]:
        if target_date:
            rows = self.conn.execute(
                """
                SELECT id, session_id, role, raw_text, phase_hint, noul_score, created_at
                FROM scratchpad_log WHERE date(created_at) = ? ORDER BY id
                """,
                (target_date,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT id, session_id, role, raw_text, phase_hint, noul_score, created_at
                FROM scratchpad_log ORDER BY id
                """
            ).fetchall()
        if not rows:
            return None, 0

        os.makedirs(self.archive_dir, exist_ok=True)
        stamp = target_date or "all"
        path = os.path.join(self.archive_dir, f"scratchpad_{stamp}.jsonl.gz")
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({
                    "id": r[0], "session_id": r[1], "role": r[2], "raw_text": r[3],
                    "phase_hint": r[4], "noul_score": r[5], "created_at": r[6],
                }, ensure_ascii=False) + "\n")

        ids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(f"DELETE FROM scratchpad_log WHERE id IN ({placeholders})", ids)
        self.conn.commit()
        return path, len(ids)


def main() -> None:
    parser = argparse.ArgumentParser(description="白泽·Cortex 慢波睡眠合成")
    parser.add_argument("--db", default="baize.db")
    parser.add_argument("--archive-dir", default="cold_archive")
    parser.add_argument("--date", default=None)
    args = parser.parse_args()
    conn = init_db(args.db)
    try:
        report = DreamConsolidator(conn, archive_dir=args.archive_dir).run_consolidation(args.date)
        print(json.dumps(report, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
