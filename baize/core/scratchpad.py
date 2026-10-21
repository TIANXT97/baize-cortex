"""Layer 0: 瞬态草稿流管理器"""
import sqlite3
from typing import Any, Dict, List, Optional


class ScratchpadManager:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def append_log(
        self,
        session_id: str,
        role: str,
        raw_text: str,
        phase_hint: Optional[str] = None,
        noul_score: float = 0.0,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO scratchpad_log (session_id, role, raw_text, phase_hint, noul_score)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, role, raw_text, phase_hint, noul_score),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_session_logs(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, session_id, role, raw_text, phase_hint, noul_score, created_at
            FROM (
                SELECT * FROM scratchpad_log
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id ASC
            """,
            (session_id, limit),
        ).fetchall()
        return [_row_to_log(r) for r in rows]

    def export_pending_phase_logs(self, date_str: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, session_id, role, raw_text, phase_hint, noul_score, created_at
            FROM scratchpad_log
            WHERE phase_hint IS NOT NULL
              AND (? IS NULL OR date(created_at) = ?)
            ORDER BY id ASC
            """,
            (date_str, date_str),
        ).fetchall()
        return [_row_to_log(r) for r in rows]


def _row_to_log(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "session_id": row[1],
        "role": row[2],
        "raw_text": row[3],
        "phase_hint": row[4],
        "noul_score": row[5],
        "created_at": row[6],
    }
