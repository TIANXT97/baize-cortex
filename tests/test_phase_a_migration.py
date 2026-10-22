"""Phase A:Schema 增量迁移、回填、幂等唯一索引与 supersedes_id 校验"""
import glob
import os
import sqlite3
import tempfile

from baize.core.supersession import supersede
from baize.storage.db import SCHEMA_PATH, init_db
from baize.storage.migrations import BACKFILL_SOURCE, NEW_COLUMNS, migrate_phase_a


def _legacy_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO scratchpad_log (session_id, role, raw_text, created_at) VALUES ('s','user','旧草稿','2026-01-02 03:04:05')"
    )
    conn.execute(
        "INSERT INTO living_states (entity_id, display_name, status, current_decision, updated_at) "
        "VALUES ('proj:a','甲','ACTIVE','定案','2026-02-03 04:05:06')"
    )
    conn.execute("INSERT INTO graph_nodes (node_id, label, node_type) VALUES ('n1','a','concept'),('n2','b','concept')")
    conn.execute(
        "INSERT INTO graph_edges (src_id, dst_id, rel_type, valid_at, created_at) "
        "VALUES ('n1','n2','depends_on','2026-03-04 05:06:07','2026-03-05 06:07:08')"
    )
    conn.commit()
    conn.close()


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_columns_added_and_legacy_rows_backfilled():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "m.db")
        _legacy_db(path)
        conn = init_db(path)
        try:
            for table in BACKFILL_SOURCE:
                assert {n for n, _ in NEW_COLUMNS} <= _cols(conn, table)
            assert conn.execute("SELECT ingested_at, valid_at FROM scratchpad_log").fetchone() == (
                "2026-01-02 03:04:05", "2026-01-02 03:04:05")
            assert conn.execute("SELECT ingested_at, valid_at FROM living_states").fetchone() == (
                "2026-02-03 04:05:06", "2026-02-03 04:05:06")
            ingested, valid = conn.execute("SELECT ingested_at, valid_at FROM graph_edges").fetchone()
            assert ingested == "2026-03-05 06:07:08"
            assert valid == "2026-03-04 05:06:07"
            assert conn.execute("SELECT pinned, hit_count FROM scratchpad_log").fetchone() == (0, 0)
            assert len(glob.glob(path + ".bak-*")) == 1
        finally:
            conn.close()


def test_migration_is_repeatable_without_side_effects():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "r.db")
        _legacy_db(path)
        conn = init_db(path)
        try:
            before = conn.execute("SELECT ingested_at FROM scratchpad_log").fetchone()
            report = migrate_phase_a(conn)
            assert report == {"added_columns": [], "backfilled": 0, "backup": None}
            assert conn.execute("SELECT ingested_at FROM scratchpad_log").fetchone() == before
            assert len(glob.glob(path + ".bak-*")) == 1
        finally:
            conn.close()


def test_new_rows_untouched_by_backfill_and_empty_db_skips_backup():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "e.db")
        conn = init_db(path)
        try:
            assert not glob.glob(path + ".bak-*")
            conn.execute(
                "INSERT INTO scratchpad_log (session_id, role, raw_text, ingested_at) VALUES ('s','user','x','keep')"
            )
            conn.commit()
            migrate_phase_a(conn)
            assert conn.execute("SELECT ingested_at FROM scratchpad_log").fetchone()[0] == "keep"
        finally:
            conn.close()


def test_unique_index_blocks_duplicate_idempotency_key():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "u.db"))
        try:
            sql = "INSERT INTO scratchpad_log (session_id, role, raw_text, idempotency_key) VALUES ('s','user','x',?)"
            conn.execute(sql, ("k1",))
            try:
                conn.execute(sql, ("k1",))
                raise AssertionError("重复幂等键未被拒绝")
            except sqlite3.IntegrityError:
                pass
            conn.execute(sql, (None,))
            conn.execute(sql, (None,))
            conn.commit()
        finally:
            conn.close()


def _insert(conn, agent, valid="2026-05-01 00:00:00"):
    cur = conn.execute(
        "INSERT INTO scratchpad_log (session_id, role, raw_text, agent_id, valid_at) VALUES ('s','user','x',?,?)",
        (agent, valid),
    )
    conn.commit()
    return cur.lastrowid


def test_supersede_validation_and_old_record_expiry():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "s.db"))
        try:
            old = _insert(conn, "A", "2026-04-01 00:00:00")
            new = _insert(conn, "A", "2026-05-01 00:00:00")
            other = _insert(conn, "B")
            for args in [(new, new), (new, 9999), (9999, old), (new, other)]:
                try:
                    supersede(conn, "scratchpad_log", *args)
                    raise AssertionError(f"应被拒绝: {args}")
                except ValueError:
                    pass
            try:
                supersede(conn, "graph_nodes", new, old)
                raise AssertionError("非法表应被拒绝")
            except ValueError:
                pass
            assert conn.execute("SELECT expired_at FROM scratchpad_log WHERE id = ?", (old,)).fetchone()[0] is None

            supersede(conn, "scratchpad_log", new, old)
            assert conn.execute("SELECT supersedes_id FROM scratchpad_log WHERE id = ?", (new,)).fetchone()[0] == old
            expired, invalid = conn.execute(
                "SELECT expired_at, invalid_at FROM scratchpad_log WHERE id = ?", (old,)).fetchone()
            assert expired is not None
            assert invalid == "2026-05-01 00:00:00"
        finally:
            conn.close()


if __name__ == "__main__":
    test_columns_added_and_legacy_rows_backfilled()
    test_migration_is_repeatable_without_side_effects()
    test_new_rows_untouched_by_backfill_and_empty_db_skips_backup()
    test_unique_index_blocks_duplicate_idempotency_key()
    test_supersede_validation_and_old_record_expiry()
    print("✅ Phase A 迁移测试全部通过")
