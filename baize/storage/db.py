"""统一 SQLite 存储与连接管理"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from baize.storage.migrations import migrate_phase_a

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def init_db(db_path: str = "baize.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    migrate_phase_a(conn)
    return conn


def open_memory_copy(db_path: str) -> sqlite3.Connection:
    """把数据库文件读进内存连接再用:源文件只读 + immutable 打开,不产生 -wal/-shm,不写任何字节。

    immutable 会忽略 -wal 与热日志,所以遇到未合并的 -wal 或残留 -journal 一律拒绝,避免漏读数据。
    """
    wal = db_path + "-wal"
    if (os.path.exists(wal) and os.path.getsize(wal) > 0) or os.path.exists(db_path + "-journal"):
        raise ValueError("存在未合并的 -wal 或残留 -journal,请先用 sqlite 的 backup 拷一份再对拷贝运行")
    source = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        source.backup(conn)
    finally:
        source.close()
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    migrate_phase_a(conn)
    return conn


@contextmanager
def get_db_connection(db_path: str = "baize.db") -> Iterator[sqlite3.Connection]:
    conn = init_db(db_path)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
