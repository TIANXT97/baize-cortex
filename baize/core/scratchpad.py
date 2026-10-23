"""Layer 0: 瞬态草稿流管理器"""
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


EXPLICIT_HINT = "explicit_addition"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_SEARCH_TOKENS = 16
RECENT_TURN_MAX_CHARS = 400
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def validate_timestamp(value: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise ValueError("时间必须是 YYYY-MM-DD HH:MM:SS 格式,不接受带 T 的 ISO 格式")
    try:
        datetime.strptime(value, TIMESTAMP_FORMAT)
    except ValueError:
        raise ValueError(f"不是合法的日期时间: {value}")
    return value


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
        if phase_hint == EXPLICIT_HINT:
            raise ValueError("phase_hint=explicit_addition 仅限 cortex_add 内部路径使用")
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

    def search_explicit(self, tokens: List[str], limit: int = 5) -> List[Dict[str, Any]]:
        tokens = [t for t in tokens if t][:_MAX_SEARCH_TOKENS]
        if not tokens or limit <= 0:
            return []
        clause = " OR ".join("raw_text LIKE ? ESCAPE '\\'" for _ in tokens)
        params: List[Any] = [f"%{_escape_like(t)}%" for t in tokens]
        rows = self.conn.execute(
            f"""
            SELECT id, raw_text, category, created_at FROM scratchpad_log
            WHERE phase_hint = '{EXPLICIT_HINT}' AND expired_at IS NULL AND ({clause})
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [{"id": r[0], "text": r[1], "category": r[2], "created_at": r[3]} for r in rows]

    def search_recent_turns(
        self, tokens: List[str], limit: int = 3, max_age_hours: float = 72.0,
        max_chars: int = RECENT_TURN_MAX_CHARS,
    ) -> List[Dict[str, Any]]:
        """Issue #37:普通对话轮次的末级召回通道。

        网关每轮都会把 user/assistant 文本写进 Layer 0,但 `phase_hint` 只在 PhaseDetector 判
        decision_approved 时才非空,而固化器与 explicit 兜底都只认 `phase_hint IS NOT NULL`——
        于是普通对话永久只写不读。这里绕开 phase_hint 语义直接按 role + 时间窗 + token 命中取行,
        只服务"跨会话短期连续性",因此窗口短、条数少、文本裁剪,且严格只读。
        """
        words = [t for t in tokens if t][:_MAX_SEARCH_TOKENS]
        if not words or limit <= 0 or max_age_hours <= 0:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).strftime(TIMESTAMP_FORMAT)
        clause = " OR ".join("raw_text LIKE ? ESCAPE '\\'" for _ in words)
        params: List[Any] = [f"%{_escape_like(t)}%" for t in words]
        rows = self.conn.execute(
            f"""
            SELECT id, raw_text, created_at FROM scratchpad_log
            WHERE role = 'user' AND phase_hint IS NULL AND expired_at IS NULL
              AND created_at >= ? AND ({clause})
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (cutoff, *params, limit),
        ).fetchall()
        return [
            {"id": r[0], "text": (r[1] or "")[:max_chars], "category": None, "created_at": r[2]}
            for r in rows
        ]

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

    def find_for_purge(
        self,
        before_id: Optional[int] = None,
        role: Optional[str] = None,
        before_date: Optional[str] = None,
        include_pending_phase: bool = False,
    ) -> List[Dict[str, Any]]:
        where, params = _purge_filter(before_id, role, before_date)
        if not include_pending_phase:
            where += " AND phase_hint IS NULL"
        rows = self.conn.execute(
            f"""
            SELECT id, session_id, role, raw_text, phase_hint, noul_score, created_at, category, reason
            FROM scratchpad_log
            WHERE {where}
            ORDER BY id ASC
            """,
            params,
        ).fetchall()
        return [{**_row_to_log(r), "category": r[7], "reason": r[8]} for r in rows]

    def count_pending_for_purge(
        self,
        before_id: Optional[int] = None,
        role: Optional[str] = None,
        before_date: Optional[str] = None,
    ) -> int:
        where, params = _purge_filter(before_id, role, before_date)
        return self.conn.execute(
            f"SELECT COUNT(*) FROM scratchpad_log WHERE {where} AND phase_hint IS NOT NULL", params
        ).fetchone()[0]

    def export_pending_phase_logs(self, date_str: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, session_id, role, raw_text, phase_hint, noul_score, created_at,
                   expired_at, category, reason
            FROM scratchpad_log
            WHERE phase_hint IS NOT NULL
              AND (? IS NULL OR date(created_at) = ?)
            ORDER BY id ASC
            """,
            (date_str, date_str),
        ).fetchall()
        return [{**_row_to_log(r), "expired_at": r[7], "category": r[8], "reason": r[9]} for r in rows]


def _purge_filter(
    before_id: Optional[int], role: Optional[str], before_date: Optional[str]
) -> Tuple[str, Tuple[Any, ...]]:
    if before_date is not None:
        validate_timestamp(before_date)
    where = "(? IS NULL OR id < ?) AND (? IS NULL OR role = ?) AND (? IS NULL OR created_at < ?)"
    return where, (before_id, before_id, role, role, before_date, before_date)


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
