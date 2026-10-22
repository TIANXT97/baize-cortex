"""Layer 0: 瞬态草稿流管理器"""
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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
        idempotency_key: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> int:
        now = _utc_now()
        cur = self.conn.execute(
            """
            INSERT INTO scratchpad_log
                (session_id, role, raw_text, phase_hint, noul_score, idempotency_key, agent_id, ingested_at, valid_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, role, raw_text, phase_hint, noul_score, idempotency_key, agent_id, now, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def find_by_idempotency_key(self, key: str) -> Optional[Tuple[int, Optional[str]]]:
        row = self.conn.execute(
            "SELECT id, agent_id FROM scratchpad_log WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def add_explicit(
        self,
        session_id: str,
        content: str,
        category: str,
        reason: str,
        idempotency_key: str,
        agent_id: Optional[str],
        commit: bool = True,
    ) -> int:
        now = _utc_now()
        cur = self.conn.execute(
            """
            INSERT INTO scratchpad_log
                (session_id, role, raw_text, phase_hint, noul_score, category, reason,
                 idempotency_key, agent_id, ingested_at, valid_at)
            VALUES (?, 'user', ?, 'explicit_addition', 0.0, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, content, category, reason, idempotency_key, agent_id, now, now),
        )
        if commit:
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

    def find_for_purge(self, before_id: Optional[int] = None, role: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, session_id, role, raw_text, phase_hint, noul_score, created_at
            FROM scratchpad_log
            WHERE (? IS NULL OR id < ?)
              AND (? IS NULL OR role = ?)
            ORDER BY id ASC
            """,
            (before_id, before_id, role, role),
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
