"""Layer 0: 瞬态草稿流管理器"""
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


EXPLICIT_HINT = "explicit_addition"
CANDIDATE_HINT = "candidate"
# Issue #56 P1-3:candidate 行的 category 记来源类型;历史 NULL 按 fact 处理(与 PR #54 行为一致)
# 与 cortex_add 写入的 category(fact/preference/rule/lesson)共用同列,二者靠 phase_hint 区分,勿混用
CANDIDATE_FACT = "fact"
CANDIDATE_DECISION = "decision"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_SEARCH_TOKENS = 16
RECENT_TURN_MAX_CHARS = 400
RECENT_TURN_MIN_CHARS = 6  # 行侧下限：整行只有「可以」「好的」这种两字应答不构成记忆
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")


def batch_judge_enabled() -> bool:
    """Issue #44 PR4:批量判管线总开关。

    关闭时普通 user 行 judge_status 保持 NULL(与历史存量同语义,可被默认 purge 清退),
    开启时才置 pending 进入夜间判研管线——防止开关关闭期间 pending 行无限积压且永不可清。
    """
    return os.environ.get("CORTEX_BATCH_JUDGE", "").strip().lower() in ("1", "true", "on")


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
        # Issue #44 PR4:judge_status 状态机——已打标行与非 user 行视为已判;
        # 普通 user 行仅在批量判管线开启时进 pending,关闭时保持 NULL(历史同语义,可被 purge)
        judge_status = (
            "done" if (phase_hint or role != "user")
            else ("pending" if batch_judge_enabled() else None)
        )
        cur = self.conn.execute(
            """
            INSERT INTO scratchpad_log
                (session_id, role, raw_text, phase_hint, noul_score, idempotency_key, agent_id,
                 ingested_at, valid_at, judge_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, role, raw_text, phase_hint, noul_score, idempotency_key, agent_id, now, now, judge_status),
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
        max_chars: int = RECENT_TURN_MAX_CHARS, agent_id: Optional[str] = None,
        min_chars: int = RECENT_TURN_MIN_CHARS,
    ) -> List[Dict[str, Any]]:
        """Issue #37:普通对话轮次的末级召回通道。

        网关每轮都会把 user/assistant 文本写进 Layer 0,但 `phase_hint` 只在 PhaseDetector 判
        decision_approved 时才非空,而固化器与 explicit 兜底都只认 `phase_hint IS NOT NULL`——
        于是普通对话永久只写不读。这里绕开 phase_hint 语义直接按 role + 时间窗 + token 命中取行,
        只服务"跨会话短期连续性",因此窗口短、条数少、文本裁剪,且严格只读。

        Issue #39 收口:
        - `agent_id` 非空时按代理隔离;NULL 属于未启用 token 时期的历史行,放行以免记忆断层。
        - `min_chars` 过滤零信息量的应答行(「可以」「好的」),这类行被召回只会污染上下文。
        - 按命中 token 数排序再按时间排序,不再单纯"新的优先",否则一个高频词就能把
          三条与当前话题无关的旧对话顶上来。
        """
        words = [t for t in tokens if t][:_MAX_SEARCH_TOKENS]
        if not words or limit <= 0 or max_age_hours <= 0:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).strftime(TIMESTAMP_FORMAT)
        clause = " OR ".join("raw_text LIKE ? ESCAPE '\\'" for _ in words)
        params: List[Any] = [f"%{_escape_like(t)}%" for t in words]
        hits_expr = " + ".join(
            "(CASE WHEN raw_text LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END)" for _ in words
        )
        rows = self.conn.execute(
            f"""
            SELECT id, raw_text, created_at, ({hits_expr}) AS hits
            FROM scratchpad_log
            WHERE role = 'user' AND phase_hint IS NULL AND expired_at IS NULL
              AND created_at >= ?
              AND length(trim(raw_text)) >= ?
              AND (? IS NULL OR agent_id IS NULL OR agent_id = ?)
              AND COALESCE(judge_status, '') <> 'failed'
              AND ({clause})
            ORDER BY hits DESC, created_at DESC, id DESC
            LIMIT ?
            """,
            (*params, cutoff, min_chars, agent_id, agent_id, *params, limit),
        ).fetchall()
        return [
            {
                "id": r[0], "text": (r[1] or "")[:max_chars], "category": None,
                "created_at": r[2], "hits": r[3],
            }
            for r in rows
        ]

    def search_candidates(self, tokens: List[str], limit: int = 3) -> List[Dict[str, Any]]:
        """Issue #44 PR4:candidate 候选流的只读召回(降权通道),复用 recent_turns 的命中数排序。"""
        words = [t for t in tokens if t][:_MAX_SEARCH_TOKENS]
        if not words or limit <= 0:
            return []
        clause = " OR ".join("raw_text LIKE ? ESCAPE '\\'" for _ in words)
        params: List[Any] = [f"%{_escape_like(t)}%" for t in words]
        hits_expr = " + ".join(
            "(CASE WHEN raw_text LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END)" for _ in words
        )
        rows = self.conn.execute(
            f"""
            SELECT id, raw_text, created_at, noul_score, ({hits_expr}) AS hits
            FROM scratchpad_log
            WHERE phase_hint = '{CANDIDATE_HINT}' AND expired_at IS NULL AND ({clause})
            ORDER BY hits DESC, created_at DESC, id DESC
            LIMIT ?
            """,
            (*params, *params, limit),
        ).fetchall()
        return [
            {"id": r[0], "text": (r[1] or "")[:RECENT_TURN_MAX_CHARS], "created_at": r[2],
             "noul": r[3], "hits": r[4]}
            for r in rows
        ]

    def fetch_pending_judgement(self, limit: int = 500, max_retry: int = 3) -> List[Dict[str, Any]]:
        """Issue #44 PR4:捞待批量判的 user 行,按会话分组、id 时间正序(缓解批次锚定效应)。"""
        rows = self.conn.execute(
            """
            SELECT id, session_id, raw_text FROM scratchpad_log
            WHERE judge_status = 'pending' AND retry_count < ? AND role = 'user'
              AND phase_hint IS NULL AND expired_at IS NULL
            ORDER BY session_id, id
            LIMIT ?
            """,
            (max_retry, limit),
        ).fetchall()
        return [{"id": r[0], "session_id": r[1], "raw_text": r[2]} for r in rows]

    def mark_judged(
        self, row_id: int, phase_hint: Optional[str], noul: float, source: str,
        category: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE scratchpad_log
            SET judge_status = 'done', judge_source = ?, phase_hint = COALESCE(?, phase_hint),
                noul_score = ?, category = COALESCE(?, category)
            WHERE id = ?
            """,
            (source, phase_hint, noul, category, row_id),
        )

    def mark_judge_retry(self, row_ids: List[int], max_retry: int) -> None:
        if not row_ids:
            return
        placeholders = ",".join("?" * len(row_ids))
        self.conn.execute(
            f"""
            UPDATE scratchpad_log
            SET retry_count = retry_count + 1,
                judge_status = CASE WHEN retry_count + 1 >= ? THEN 'failed' ELSE 'pending' END
            WHERE id IN ({placeholders})
            """,
            (max_retry, *row_ids),
        )

    def backfill_judge(
        self,
        after_date: Optional[str] = None,
        before_date: Optional[str] = None,
        limit: int = 500,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Issue #44 PR4 盲区1:一次性存量回填——显式时间窗 + 条数上限,默认 dry-run 出清单。

        只把历史双 NULL 行(user 且未打标且未过期)置为 pending,交给夜间批量判;
        不带窗口则拒绝执行,防止全量存量风暴。
        """
        if after_date is None and before_date is None:
            raise ValueError("必须显式指定 after_date 或 before_date 时间窗,防止全量存量回填风暴")
        if after_date is not None:
            validate_timestamp(after_date)
        if before_date is not None:
            validate_timestamp(before_date)
        where = (
            "phase_hint IS NULL AND judge_status IS NULL AND role = 'user' AND expired_at IS NULL "
            "AND (? IS NULL OR created_at >= ?) AND (? IS NULL OR created_at < ?)"
        )
        params: List[Any] = [after_date, after_date, before_date, before_date]
        if dry_run:
            rows = self.conn.execute(
                f"SELECT id, raw_text FROM scratchpad_log WHERE {where} ORDER BY id LIMIT ?",
                (*params, limit),
            ).fetchall()
            return {
                "dry_run": True, "matched": len(rows), "ids": [r[0] for r in rows],
                "sample": [{"id": r[0], "raw_text": (r[1] or "")[:60]} for r in rows[:10]],
            }
        cur = self.conn.execute(
            f"""
            UPDATE scratchpad_log SET judge_status = 'pending'
            WHERE id IN (SELECT id FROM scratchpad_log WHERE {where} ORDER BY id LIMIT ?)
            """,
            (*params, limit),
        )
        self.conn.commit()
        return {"dry_run": False, "queued": cur.rowcount}

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
                 idempotency_key, agent_id, ingested_at, valid_at, judge_status)
            VALUES (?, 'user', ?, 'explicit_addition', 0.0, ?, ?, ?, ?, ?, ?, 'done')
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
            # Issue #44 PR4:待批量判(pending)的行同样豁免——未判完被删等于记忆丢失;failed 不豁免(有冷归档)
            where += " AND phase_hint IS NULL AND COALESCE(judge_status, '') <> 'pending'"
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
            f"SELECT COUNT(*) FROM scratchpad_log WHERE {where} "
            f"AND (phase_hint IS NOT NULL OR judge_status = 'pending')",
            params,
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
