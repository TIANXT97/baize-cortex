"""Phase 8 专项测试:全角冒号清洗、无锚点定案兜底、黑户转正、backfill/purge 端点"""
import glob
import gzip
import json
import os
import tempfile

from fastapi.testclient import TestClient

from baize.core.consolidator import DreamConsolidator, _clean_decision
from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import ScratchpadManager
from baize.server.app import create_app
from baize.storage.db import init_db


def test_fullwidth_colon_stripped_from_decision():
    assert _clean_decision("：方案X") == "方案X"
    assert _clean_decision(":: 方案Y") == "方案Y"


def test_anchorless_authoritative_approval_is_consumed():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "n.db"))
        try:
            sp = ScratchpadManager(conn)
            sp.append_log("s", "user", "proj:f 定案:方案一", phase_hint="approval")
            sp.append_log("s", "user", "无锚点定案:方案二", phase_hint="approval")
            report = DreamConsolidator(conn, archive_dir=os.path.join(d, "a")).run_consolidation()
            assert "proj:f" in report["updated_states"]
            assert report["archived_logs"] == 2
            assert conn.execute("SELECT COUNT(*) FROM scratchpad_log").fetchone()[0] == 0
        finally:
            conn.close()


def test_upsert_promotes_blackbox_name_and_keeps_good_name():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "u.db"))
        try:
            living = LivingStateManager(conn)
            living.update_decision("proj:g", "proj:g", "旧")
            assert living.resolve_display_name("proj:g", "玻璃模具") == "玻璃模具"
            living.update_decision("proj:g", "玻璃模具", "新")
            assert living.resolve_display_name("proj:g", "别的名字") == "玻璃模具"
        finally:
            conn.close()


def test_backfill_and_purge_endpoints():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "e.db")
        archive = os.path.join(d, "cold")
        app = create_app(db, archive_dir=archive)
        with TestClient(app) as client:
            client.post("/v1/living-state/decision", json={
                "entity_id": "proj:h", "display_name": "proj:h", "new_decision": "旧",
                "metadata": {"aliases": ["模具别名"]},
            })
            dry = client.post("/v1/admin/backfill-display-names", json={"dry_run": True}).json()
            assert dry == [{"entity_id": "proj:h", "old_display_name": "proj:h", "new_display_name": "模具别名"}]
            assert client.get("/v1/living-state/proj:h").json()["display_name"] == "proj:h"
            client.post("/v1/admin/backfill-display-names", json={"dry_run": False})
            assert client.get("/v1/living-state/proj:h").json()["display_name"] == "模具别名"

            for i in range(3):
                client.post("/v1/scratchpad/log", json={"session_id": "p", "role": "user", "raw_text": f"碎屑{i}"})
            client.post("/v1/scratchpad/log", json={"session_id": "p", "role": "assistant", "raw_text": "保留"})
            assert not glob.glob(os.path.join(archive, "purged_*.jsonl.gz"))

            preview = client.post("/v1/admin/purge-scratchpad", json={"role": "user", "dry_run": True}).json()
            assert preview["matched"] == 3
            assert len(preview["sample"]) == 3
            assert preview["purged"] == 0
            assert not glob.glob(os.path.join(archive, "purged_*.jsonl.gz"))

            real = client.post("/v1/admin/purge-scratchpad", json={"role": "user", "dry_run": False}).json()
            assert real["purged"] == 3
            files = glob.glob(os.path.join(archive, "purged_*.jsonl.gz"))
            assert len(files) == 1
            with gzip.open(files[0], "rt", encoding="utf-8") as fh:
                rows = [json.loads(line)["raw_text"] for line in fh if line.strip()]
            assert rows == ["碎屑0", "碎屑1", "碎屑2"]
            remaining = client.post("/v1/admin/purge-scratchpad", json={"dry_run": True}).json()
            assert remaining["matched"] == 1


if __name__ == "__main__":
    test_fullwidth_colon_stripped_from_decision()
    test_anchorless_authoritative_approval_is_consumed()
    test_upsert_promotes_blackbox_name_and_keeps_good_name()
    test_backfill_and_purge_endpoints()
    print("✅ Phase 8 consolidation closure 测试全部通过")
