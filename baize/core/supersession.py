"""supersedes_id 写入校验:同一事务内让旧记录认知过期、现实失效。"""
import sqlite3
from datetime import datetime, timezone

from baize.storage.migrations import BACKFILL_SOURCE


def supersede(conn: sqlite3.Connection, table: str, new_id: int, old_id: int, commit: bool = True) -> None:
    if table not in BACKFILL_SOURCE:
        raise ValueError(f"不支持的表: {table}")
    if new_id == old_id:
        raise ValueError("记录不能推翻自身")

    new = conn.execute(
        f"SELECT COALESCE(agent_id, 'default'), valid_at FROM {table} WHERE rowid = ?", (new_id,)
    ).fetchone()
    old = conn.execute(
        f"SELECT COALESCE(agent_id, 'default') FROM {table} WHERE rowid = ?", (old_id,)
    ).fetchone()
    if new is None:
        raise ValueError("新记录不存在")
    if old is None:
        raise ValueError("被推翻的记录不存在")
    if new[0] != old[0]:
        raise ValueError("被推翻的记录不属于同一 agent")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(f"UPDATE {table} SET supersedes_id = ? WHERE rowid = ?", (old_id, new_id))
        conn.execute(
            f"UPDATE {table} SET expired_at = COALESCE(expired_at, ?), invalid_at = COALESCE(invalid_at, ?) "
            f"WHERE rowid = ?",
            (now, new[1] or now, old_id),
        )
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
