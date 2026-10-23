"""Issue #32 遗留 2:草稿流 before_date 归档窗口、dry_run 清单、冷归档自洽与一键脚本"""
import glob
import gzip
import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from baize.core.consolidator import DreamConsolidator
from baize.core.normalization import idempotency_key
from baize.core.scratchpad import ScratchpadManager, validate_timestamp
from baize.cortex_purge import cutoff_from_days, main as purge_main
from baize.server.app import create_app
from baize.storage.db import init_db


def _setup(conn, specs):
    sp = ScratchpadManager(conn)
    ids = []
    for role, text, created in specs:
        lid = sp.append_log("s", role, text)
        conn.execute("UPDATE scratchpad_log SET created_at = ? WHERE id = ?", (created, lid))
        ids.append(lid)
    conn.commit()
    return ids


def _remaining(conn):
    return [r[0] for r in conn.execute("SELECT id FROM scratchpad_log ORDER BY id")]


def _archives(arc):
    return glob.glob(os.path.join(arc, "purged_*.jsonl.gz"))


def _read_archive(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


SPECS = [
    ("user", "旧草稿甲", "2026-09-01 08:00:00"),
    ("assistant", "旧草稿乙", "2026-09-02 08:00:00"),
    ("user", "边界草稿", "2026-10-01 00:00:00"),
    ("user", "新草稿丙", "2026-10-20 08:00:00"),
]


def test_dry_run_with_before_date_lists_ids_and_deletes_nothing():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "a.db"))
        try:
            ids = _setup(conn, SPECS)
            report = DreamConsolidator(conn, archive_dir=os.path.join(d, "arc")).purge_scratchpad(
                before_date="2026-10-01 00:00:00")
            assert report["dry_run"] is True
            assert report["matched"] == 2 and report["ids"] == ids[:2]
            assert report["purged"] == 0 and report["archive_file"] is None
            assert _remaining(conn) == ids
            assert not os.path.exists(os.path.join(d, "arc"))
        finally:
            conn.close()


def test_filters_are_combined_with_and_and_cutoff_is_strict():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "b.db"))
        try:
            ids = _setup(conn, SPECS)
            c = DreamConsolidator(conn, archive_dir=os.path.join(d, "arc"))
            assert c.purge_scratchpad(before_date="2026-10-01 00:00:00")["ids"] == ids[:2]
            assert c.purge_scratchpad(before_date="2026-10-01 00:00:01")["ids"] == ids[:3]
            assert c.purge_scratchpad(before_date="2026-10-21 00:00:00", before_id=ids[2])["ids"] == ids[:2]
            assert c.purge_scratchpad(before_date="2026-10-21 00:00:00", role="user")["ids"] == [ids[0], ids[2], ids[3]]
            both = c.purge_scratchpad(before_date="2026-10-01 00:00:01", before_id=ids[2], role="user")
            assert both["ids"] == [ids[0]]
            assert c.purge_scratchpad(role="assistant")["ids"] == [ids[1]]
        finally:
            conn.close()


def test_real_run_archives_losslessly_then_deletes_and_db_stays_healthy():
    with tempfile.TemporaryDirectory() as d:
        arc = os.path.join(d, "arc")
        conn = init_db(os.path.join(d, "c.db"))
        try:
            ids = _setup(conn, SPECS)
            sp = ScratchpadManager(conn)
            text = "显式沉淀的旧事实"
            eid = sp.add_explicit("s", text, "lesson", "留痕原因", idempotency_key("s", text), "default")
            conn.execute("UPDATE scratchpad_log SET created_at = '2026-08-01 00:00:00' WHERE id = ?", (eid,))
            conn.commit()

            report = DreamConsolidator(conn, archive_dir=arc).purge_scratchpad(
                before_date="2026-10-01 00:00:00", dry_run=False)
            assert report["purged"] == 3 and report["ids"] == [ids[0], ids[1], eid]
            assert _remaining(conn) == [ids[2], ids[3]]
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

            files = _archives(arc)
            assert files == [report["archive_file"]]
            rows = _read_archive(files[0])
            assert [r["id"] for r in rows] == report["ids"]
            assert [r["raw_text"] for r in rows] == ["旧草稿甲", "旧草稿乙", text]
            assert rows[2]["category"] == "lesson" and rows[2]["reason"] == "留痕原因"
            assert rows[2]["phase_hint"] == "explicit_addition"
            assert rows[0]["created_at"] == "2026-09-01 08:00:00"
        finally:
            conn.close()


def test_large_purge_is_chunked_and_complete():
    with tempfile.TemporaryDirectory() as d:
        arc = os.path.join(d, "arc")
        conn = init_db(os.path.join(d, "big.db"))
        try:
            sp = ScratchpadManager(conn)
            for i in range(1300):
                sp.append_log("s", "user", f"批量草稿{i}")
            conn.execute("UPDATE scratchpad_log SET created_at = '2026-01-01 00:00:00'")
            conn.commit()
            report = DreamConsolidator(conn, archive_dir=arc).purge_scratchpad(
                before_date="2026-02-01 00:00:00", dry_run=False)
            assert report["purged"] == 1300 and _remaining(conn) == []
            assert len(_read_archive(report["archive_file"])) == 1300
        finally:
            conn.close()


def test_timestamp_validation_rejects_iso_t_and_garbage():
    assert validate_timestamp("2026-10-01 00:00:00") == "2026-10-01 00:00:00"
    for bad in ("2026-10-01T00:00:00", "2026-10-01T00:00:00Z", "2026-10-01", "2026-13-01 00:00:00",
                "2026-02-30 00:00:00", "", " 2026-10-01 00:00:00", "昨天"):
        try:
            validate_timestamp(bad)
            raise AssertionError(f"应被拒绝: {bad!r}")
        except ValueError:
            pass


def test_endpoint_accepts_before_date_and_rejects_bad_format():
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "e.db"), os.path.join(d, "cold"))
        with TestClient(app) as tc:
            ids = _setup(app.state.conn, SPECS)
            dry = tc.post("/v1/admin/purge-scratchpad", json={"before_date": "2026-10-01 00:00:00"}).json()
            assert dry["dry_run"] is True and dry["ids"] == ids[:2] and dry["matched"] == 2

            for bad in ("2026-10-01T00:00:00", "2026-10-01", "垃圾"):
                resp = tc.post("/v1/admin/purge-scratchpad", json={"before_date": bad})
                assert resp.status_code == 422, (bad, resp.text)
            assert _remaining(app.state.conn) == ids

            real = tc.post("/v1/admin/purge-scratchpad", json={
                "before_date": "2026-10-01 00:00:00", "before_id": ids[1] + 1, "dry_run": False}).json()
            assert real["purged"] == 2 and _remaining(app.state.conn) == ids[2:]
            assert len(_archives(os.path.join(d, "cold"))) == 1

            unchanged = tc.post("/v1/admin/purge-scratchpad", json={"role": "user"}).json()
            assert unchanged["dry_run"] is True and unchanged["matched"] == 2
            assert _remaining(app.state.conn) == ids[2:]


def _run_cli(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = purge_main(list(argv))
    return code, buf.getvalue()


def test_cli_defaults_to_dry_run_and_execute_archives():
    with tempfile.TemporaryDirectory() as d:
        db, arc = os.path.join(d, "cli.db"), os.path.join(d, "arc")
        conn = init_db(db)
        try:
            old = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S")
            recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
            ids = _setup(conn, [("user", "四十五天前", old), ("user", "五天前", recent)])
        finally:
            conn.close()

        code, out = _run_cli("--db", db, "--archive-dir", arc, "--days", "30")
        report = json.loads(out)
        assert code == 0 and report["dry_run"] is True and report["ids"] == [ids[0]]
        assert not os.path.exists(arc)

        code, out = _run_cli("--db", db, "--archive-dir", arc, "--days", "30", "--dry-run")
        assert json.loads(out)["dry_run"] is True

        code, out = _run_cli("--db", db, "--archive-dir", arc, "--days", "30", "--execute")
        report = json.loads(out)
        assert code == 0 and report["purged"] == 1 and len(_archives(arc)) == 1

        conn = init_db(db)
        try:
            assert _remaining(conn) == [ids[1]]
        finally:
            conn.close()


def test_cli_rejects_missing_db_and_negative_days():
    with tempfile.TemporaryDirectory() as d:
        missing = os.path.join(d, "nope.db")
        code, _ = _run_cli("--db", missing)
        assert code == 2 and not os.path.exists(missing)
        try:
            _run_cli("--db", missing, "--days", "-1")
            raise AssertionError("负数天数应被拒绝")
        except SystemExit as exc:
            assert exc.code == 2


def test_cutoff_from_days_uses_db_timestamp_format():
    now = datetime(2026, 10, 23, 9, 0, 0, tzinfo=timezone.utc)
    assert cutoff_from_days(30, now) == "2026-09-23 09:00:00"
    assert cutoff_from_days(0, now) == "2026-10-23 09:00:00"


if __name__ == "__main__":
    test_dry_run_with_before_date_lists_ids_and_deletes_nothing()
    test_filters_are_combined_with_and_and_cutoff_is_strict()
    test_real_run_archives_losslessly_then_deletes_and_db_stays_healthy()
    test_large_purge_is_chunked_and_complete()
    test_timestamp_validation_rejects_iso_t_and_garbage()
    test_endpoint_accepts_before_date_and_rejects_bad_format()
    test_cli_defaults_to_dry_run_and_execute_archives()
    test_cli_rejects_missing_db_and_negative_days()
    test_cutoff_from_days_uses_db_timestamp_format()
    print("✅ 草稿流 before_date 归档测试全部通过")
