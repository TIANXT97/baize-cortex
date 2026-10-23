"""Issue #34:草稿流归档默认豁免 phase_hint 非空的待固化记录,仅显式开关才放行"""
import glob
import gzip
import io
import json
import os
import tempfile
from contextlib import redirect_stdout

from fastapi.testclient import TestClient

from baize.core.consolidator import DreamConsolidator
from baize.core.normalization import idempotency_key
from baize.core.scratchpad import ScratchpadManager
from baize.cortex_purge import main as purge_main
from baize.server.app import create_app
from baize.storage.db import init_db

OLD = "2026-09-01 08:00:00"
CUTOFF = "2026-10-01 00:00:00"


def _seed(conn):
    sp = ScratchpadManager(conn)
    ids = {
        "plain_user": sp.append_log("s", "user", "普通草稿"),
        "plain_assistant": sp.append_log("s", "assistant", "助手草稿"),
        "approval": sp.append_log("s", "user", "proj:keep 定案:方案甲", phase_hint="approval"),
        "rejection": sp.append_log("s", "user", "proj:keep 推翻方案乙", phase_hint="rejection"),
    }
    text = "proj:keep 的显式事实"
    ids["explicit"] = sp.add_explicit("s", text, "fact", "r", idempotency_key("s", text), "default")
    conn.execute("UPDATE scratchpad_log SET created_at = ?", (OLD,))
    ids["recent"] = sp.append_log("s", "user", "最近草稿")
    conn.commit()
    return ids


def _remaining(conn):
    return [r[0] for r in conn.execute("SELECT id FROM scratchpad_log ORDER BY id")]


def _pending(ids):
    return sorted([ids["approval"], ids["rejection"], ids["explicit"]])


def _archived(arc):
    rows = []
    for path in glob.glob(os.path.join(arc, "purged_*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            rows += [json.loads(line) for line in fh if line.strip()]
    return rows


def test_default_dry_run_exempts_pending_phase_records():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "a.db"))
        try:
            ids = _seed(conn)
            report = DreamConsolidator(conn, archive_dir=os.path.join(d, "arc")).purge_scratchpad(before_date=CUTOFF)
            assert report["ids"] == [ids["plain_user"], ids["plain_assistant"]], report
            assert report["protected_pending"] == 3
            assert report["include_pending_phase"] is False
        finally:
            conn.close()


def test_unfiltered_purge_still_protects_pending_records():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "b.db"))
        try:
            ids = _seed(conn)
            report = DreamConsolidator(conn, archive_dir=os.path.join(d, "arc")).purge_scratchpad(dry_run=False)
            assert report["purged"] == 3
            assert _remaining(conn) == _pending(ids)
        finally:
            conn.close()


def test_real_run_keeps_approval_alive_for_consolidation():
    with tempfile.TemporaryDirectory() as d:
        arc = os.path.join(d, "arc")
        conn = init_db(os.path.join(d, "c.db"))
        try:
            ids = _seed(conn)
            consolidator = DreamConsolidator(conn, archive_dir=arc)
            report = consolidator.purge_scratchpad(before_date=CUTOFF, dry_run=False)
            assert report["purged"] == 2
            assert _remaining(conn) == sorted(_pending(ids) + [ids["recent"]])
            assert {r["id"] for r in _archived(arc)} == {ids["plain_user"], ids["plain_assistant"]}

            sleep = consolidator.run_consolidation()
            assert "proj:keep" in sleep["updated_states"], sleep
            assert consolidator.living.get_state("proj:keep")["current_decision"] == "方案甲"
        finally:
            conn.close()


def test_explicit_switch_includes_pending_records_losslessly():
    with tempfile.TemporaryDirectory() as d:
        arc = os.path.join(d, "arc")
        conn = init_db(os.path.join(d, "d.db"))
        try:
            ids = _seed(conn)
            report = DreamConsolidator(conn, archive_dir=arc).purge_scratchpad(
                before_date=CUTOFF, dry_run=False, include_pending_phase=True)
            assert report["include_pending_phase"] is True and report["protected_pending"] == 0
            assert report["purged"] == 5
            assert _remaining(conn) == [ids["recent"]]
            hints = {r["id"]: r["phase_hint"] for r in _archived(arc)}
            assert hints[ids["approval"]] == "approval"
            assert hints[ids["explicit"]] == "explicit_addition"
        finally:
            conn.close()


def test_endpoint_defaults_to_protection_and_accepts_switch():
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "e.db"), os.path.join(d, "cold"))
        with TestClient(app) as tc:
            ids = _seed(app.state.conn)
            default = tc.post("/v1/admin/purge-scratchpad", json={"before_date": CUTOFF}).json()
            assert default["ids"] == [ids["plain_user"], ids["plain_assistant"]]
            assert default["protected_pending"] == 3

            scoped = tc.post("/v1/admin/purge-scratchpad", json={
                "before_date": CUTOFF, "role": "user", "include_pending_phase": True}).json()
            assert scoped["ids"] == [ids["plain_user"], ids["approval"], ids["rejection"], ids["explicit"]]

            real = tc.post("/v1/admin/purge-scratchpad", json={"before_date": CUTOFF, "dry_run": False}).json()
            assert real["purged"] == 2
            assert _remaining(app.state.conn) == sorted(_pending(ids) + [ids["recent"]])


def _cli(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = purge_main(list(argv))
    return code, json.loads(buf.getvalue())


def test_cli_requires_flag_to_touch_pending_records():
    with tempfile.TemporaryDirectory() as d:
        db, arc = os.path.join(d, "cli.db"), os.path.join(d, "arc")
        conn = init_db(db)
        try:
            ids = _seed(conn)
        finally:
            conn.close()

        code, report = _cli("--db", db, "--archive-dir", arc, "--days", "30", "--execute")
        assert code == 0 and report["purged"] == 2 and report["protected_pending"] == 3
        assert report["ids"] == [ids["plain_user"], ids["plain_assistant"]]

        code, report = _cli("--db", db, "--archive-dir", arc, "--days", "30", "--include-pending-phase")
        assert code == 0 and report["dry_run"] is True and report["ids"] == _pending(ids)

        conn = init_db(db)
        try:
            assert _remaining(conn) == sorted(_pending(ids) + [ids["recent"]])
        finally:
            conn.close()


if __name__ == "__main__":
    test_default_dry_run_exempts_pending_phase_records()
    test_unfiltered_purge_still_protects_pending_records()
    test_real_run_keeps_approval_alive_for_consolidation()
    test_explicit_switch_includes_pending_records_losslessly()
    test_endpoint_defaults_to_protection_and_accepts_switch()
    test_cli_requires_flag_to_touch_pending_records()
    print("✅ 草稿归档待固化记录保护测试全部通过")
