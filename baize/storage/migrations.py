"""Phase A 增量迁移:只加可空列与唯一索引,可重复执行,不改动存量语义。"""
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

BACKFILL_SOURCE = {
    "scratchpad_log": "created_at",
    "living_states": "updated_at",
    "graph_edges": "created_at",
}

NEW_COLUMNS: List[Tuple[str, str]] = [
    ("idempotency_key", "TEXT"),
    ("valid_at", "TEXT"),
    ("invalid_at", "TEXT"),
    ("ingested_at", "TEXT"),
    ("expired_at", "TEXT"),
    ("supersedes_id", "INTEGER"),
    ("pinned", "INTEGER DEFAULT 0"),
    ("hit_count", "INTEGER DEFAULT 0"),
    ("last_recalled_at", "TEXT"),
    ("agent_id", "TEXT"),
]

EXTRA_COLUMNS: Dict[str, List[Tuple[str, str]]] = {
    # Issue #44 PR4:批量判研状态机——NULL 表示历史存量行,永不自动回填(防存量风暴)
    "scratchpad_log": [
        ("category", "TEXT"),
        ("reason", "TEXT"),
        ("judge_status", "TEXT"),
        ("retry_count", "INTEGER DEFAULT 0"),
        ("judge_source", "TEXT"),
    ],
}

_BATCH_SIZE = 500
_ISO_FORMAT = "%Y-%m-%d %H:%M:%S"


def _wanted(table: str) -> List[Tuple[str, str]]:
    return NEW_COLUMNS + EXTRA_COLUMNS.get(table, [])


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _missing_columns(conn: sqlite3.Connection) -> Dict[str, List[Tuple[str, str]]]:
    result = {}
    for table in BACKFILL_SOURCE:
        existing = _columns(conn, table)
        result[table] = [(n, d) for n, d in _wanted(table) if n not in existing]
    return result


def _missing_indexes(conn: sqlite3.Connection) -> List[str]:
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    return [t for t in BACKFILL_SOURCE if f"idx_{t}_idempotency" not in present]


def _needs_backfill(conn: sqlite3.Connection) -> bool:
    for table, src in BACKFILL_SOURCE.items():
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE ingested_at IS NULL AND {src} IS NOT NULL LIMIT 1"
        ).fetchone()
        if row:
            return True
    return False


def _has_data(conn: sqlite3.Connection) -> bool:
    return any(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in BACKFILL_SOURCE)


def _backup(conn: sqlite3.Connection) -> Optional[str]:
    path = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not path:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = f"{path}.bak-{stamp}"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def _backfill(conn: sqlite3.Connection) -> int:
    total = 0
    for table, src in BACKFILL_SOURCE.items():
        stamp = f"strftime('{_ISO_FORMAT}', {src})"
        while True:
            cur = conn.execute(
                f"""
                UPDATE {table}
                SET ingested_at = {stamp}, valid_at = COALESCE(valid_at, {stamp})
                WHERE rowid IN (
                    SELECT rowid FROM {table}
                    WHERE ingested_at IS NULL AND {src} IS NOT NULL
                    LIMIT {_BATCH_SIZE}
                )
                """
            )
            conn.commit()
            if cur.rowcount <= 0:
                break
            total += cur.rowcount
    return total


def migrate_phase_a(conn: sqlite3.Connection) -> Dict[str, Any]:
    report: Dict[str, Any] = {"added_columns": [], "backfilled": 0, "backup": None}
    missing = _missing_columns(conn)
    if not (any(missing.values()) or _missing_indexes(conn) or _needs_backfill(conn)):
        return report

    if any(missing.values()) and _has_data(conn):
        report["backup"] = _backup(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in BACKFILL_SOURCE:
            existing = _columns(conn, table)
            for name, ddl in _wanted(table):
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    report["added_columns"].append(f"{table}.{name}")
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_idempotency "
                f"ON {table}(idempotency_key) WHERE idempotency_key IS NOT NULL"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    report["backfilled"] = _backfill(conn)
    return report
