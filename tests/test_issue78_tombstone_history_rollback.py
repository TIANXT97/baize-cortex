"""Issue #78:改主意自动留墓碑、卡片历史、回滚、真正的 consolidate dry_run"""
import os
import sqlite3
import tempfile
from contextlib import contextmanager

from fastapi.testclient import TestClient

from baize.core.consolidator import DreamConsolidator
from baize.core.living_state import LivingStateManager, tombstone_covers
from baize.core.scratchpad import ScratchpadManager
from baize.core.seeds import PROJ_ID, seed_initial_assets
from baize.core.temporal_graph import plan_node_id
from baize.server.app import create_app
from baize.server.mcp_server import BaizeTools
from baize.storage.db import init_db

ENTITY = "proj:cache"
REDIS = "使用 Redis 做缓存"
MEMCACHED = "Memcached"
TABLES = ("scratchpad_log", "living_states", "living_state_history", "graph_nodes", "graph_edges")


@contextmanager
def _env(**values):
    saved = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _app(d):
    return create_app(os.path.join(d, "p.db"), os.path.join(d, "cold"))


def _dump(conn):
    return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in TABLES}


def _card(tc, decision=REDIS, **extra):
    resp = tc.post("/v1/living-state/decision", json={
        "entity_id": ENTITY, "display_name": "缓存层", "new_decision": decision, **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _log(tc, text, hint="approval"):
    resp = tc.post("/v1/scratchpad/log", json={"session_id": "s1", "role": "user", "raw_text": text, "phase_hint": hint})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _history(tc, entity=ENTITY):
    resp = tc.get(f"/v1/living-state/{entity}/history")
    assert resp.status_code == 200, resp.text
    return resp.json()["history"]


def _state(tc, entity=ENTITY):
    return tc.get(f"/v1/living-state/{entity}").json()


def _rollback(tc, **body):
    return tc.post("/v1/admin/living-state/rollback", json={"entity_id": ENTITY, **body})


def _active_plan_edges(conn):
    return [r[0] for r in conn.execute(
        "SELECT dst_id FROM graph_edges WHERE src_id = ? AND rel_type = 'decided_as' AND invalid_at IS NULL", (ENTITY,))]


def test_redis_to_memcached_leaves_tombstone_history_and_rolls_back_and_forth():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            conn = tc.app.state.conn
            _card(tc)
            log_id = _log(tc, f"{ENTITY} 改为 {MEMCACHED}")
            report = tc.post("/v1/sleep/consolidate", json={}).json()
            assert report["updated_states"] == [ENTITY]

            card = _state(tc)
            assert card["current_decision"] == MEMCACHED and card["version"] == 2
            [stone] = card["tombstones"]
            assert stone["plan"] == REDIS and stone["killed_reason"] == "被新定案取代"
            assert stone["superseded_by"] == MEMCACHED and stone["source"] == "consolidation"
            assert stone["source_log_ids"] == [log_id] and stone["version"] == 1
            assert len(stone["at"]) == 19 and "T" not in stone["at"], "时间戳格式 YYYY-MM-DD HH:MM:SS"

            [snap] = _history(tc)
            # source 记的是取代该版本的那次改动,不是该版本当初的来源
            assert (snap["version"], snap["current_decision"], snap["source"]) == (1, REDIS, "consolidation")
            assert snap["tombstones"] == []
            assert _active_plan_edges(conn) == [plan_node_id(ENTITY, MEMCACHED)]

            dry = _rollback(tc).json()
            assert dry["dry_run"] is True and dry["changed"] is True
            assert dry["from"]["current_decision"] == MEMCACHED and dry["to"]["current_decision"] == REDIS
            assert dry["graph"] == "would_sync"
            assert _state(tc)["current_decision"] == MEMCACHED and len(_history(tc)) == 1, "dry_run 不落库"

            done = _rollback(tc, dry_run=False).json()
            assert done["graph"] == "synced" and done["to"]["version"] == 3
            card = _state(tc)
            assert (card["current_decision"], card["version"], card["tombstones"]) == (REDIS, 3, [])
            assert _active_plan_edges(conn) == [plan_node_id(ENTITY, REDIS)], "decided_as 边同步回旧方案"
            assert [h["source"] for h in _history(tc)] == ["rollback", "consolidation"]

            back = _rollback(tc, dry_run=False).json()
            assert back["changed"] is True
            card = _state(tc)
            assert (card["current_decision"], card["version"]) == (MEMCACHED, 4), "能再回滚回 Memcached"
            assert [t["plan"] for t in card["tombstones"]] == [REDIS], "墓碑随之恢复"
            assert _active_plan_edges(conn) == [plan_node_id(ENTITY, MEMCACHED)]
            assert len(_history(tc)) == 3


def test_explicit_supersede_is_not_duplicated_and_unrelated_plan_adds_second_stone():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "t.db"))
        try:
            living = LivingStateManager(conn)
            living.update_decision(ENTITY, "缓存层", REDIS)
            state = living.update_decision(
                ENTITY, "缓存层", MEMCACHED, superseded_plan=REDIS, killed_reason="成本过高", source="t")
            assert [(t["plan"], t["killed_reason"]) for t in state["tombstones"]] == [(REDIS, "成本过高")]
            assert state["tombstones"][0]["superseded_by"] == MEMCACHED

            living.update_decision(ENTITY, "缓存层", REDIS)
            state = living.update_decision(ENTITY, "缓存层", "Valkey", superseded_plan="Redis", source="t")
            assert [t["plan"] for t in state["tombstones"]][-1] == "Redis", "较短文本被旧定案包含,视为已覆盖"
            assert len([t for t in state["tombstones"] if t["superseded_by"] == "Valkey"]) == 1

            before = len(state["tombstones"])
            state = living.update_decision(ENTITY, "缓存层", "Garnet", superseded_plan="完全无关的另一个方案", source="t")
            assert len(state["tombstones"]) == before + 2, "显式墓碑没覆盖旧定案时,旧定案另留一条"
        finally:
            conn.close()
    assert tombstone_covers("Redis", REDIS) and tombstone_covers(REDIS, "Redis") and not tombstone_covers("", REDIS)
    assert not tombstone_covers("X", "XY"), "一字碎片不算覆盖"


def test_no_tombstone_when_decision_unchanged_or_card_is_new():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "t.db"))
        try:
            living = LivingStateManager(conn)
            assert living.update_decision(ENTITY, "缓存层", REDIS)["tombstones"] == []
            assert living.update_decision(ENTITY, "缓存层", REDIS, metadata={"aliases": ["缓存"]})["tombstones"] == []
            assert living.update_decision(ENTITY, "缓存层", f"  {REDIS}  ")["tombstones"] == [], "仅空白差异不算改主意"
            living.update_decision("proj:blank", "空白卡", "")
            assert living.update_decision("proj:blank", "空白卡", "第一次有内容的定案")["tombstones"] == [], "旧定案为空不留"
        finally:
            conn.close()


def test_every_write_path_snapshots_with_its_source():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            conn = tc.app.state.conn
            _card(tc)
            assert _history(tc) == [], "新建卡没有旧版本可存"

            _card(tc, "Valkey", metadata={"aliases": ["缓存"]})
            BaizeTools(conn).update_decision({"entity_id": ENTITY, "display_name": "缓存层", "new_decision": "Garnet"})
            assert tc.post("/v1/admin/living-state/visibility", json={
                "entity_id": ENTITY, "visibility": "group_ok", "dry_run": False}).status_code == 200
            conn.execute("UPDATE living_states SET display_name = ? WHERE entity_id = ?", (ENTITY, ENTITY))
            conn.commit()
            renamed = DreamConsolidator(conn).backfill_display_names(dry_run=False)
            assert [c["entity_id"] for c in renamed] == [ENTITY]

            sources = [h["source"] for h in reversed(_history(tc))]
            assert sources == ["api:decision", "mcp:update_decision", "admin:visibility", "backfill_display_names"], sources

            _log(tc, f"{ENTITY} 定案:Dragonfly")
            tc.post("/v1/sleep/consolidate", json={})
            assert _history(tc)[0]["source"] == "consolidation"

            again = tc.post("/v1/admin/living-state/visibility", json={
                "entity_id": ENTITY, "visibility": "group_ok", "dry_run": False}).json()
            assert again["after"] == "group_ok"
            assert len(_history(tc)) == len(sources) + 1, "可见性没变化时不写历史"


def test_rename_display_and_seed_snapshot_only_on_real_change():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "t.db"))
        try:
            living = LivingStateManager(conn)
            living.update_decision("proj:x", "proj:x", "定案甲", metadata={"aliases": ["小叉"]})
            living.rename_display("proj:x", "proj:x")
            assert living.history("proj:x") == []
            living.rename_display("proj:x", "小叉")
            assert [h["source"] for h in living.history("proj:x")] == ["backfill_display_names"]
            assert living.history("proj:x")[0]["display_name"] == "proj:x"

            seed_initial_assets(conn)
            assert living.history(PROJ_ID) == [], "首次 seed 是新建"
            seed_initial_assets(conn)
            assert living.history(PROJ_ID) == [], "内容没变的重复 seed 不写历史"
            seeded_name = living.get_state(PROJ_ID)["display_name"]
            living.update_decision(PROJ_ID, seeded_name, "人为改过的定案", metadata={"aliases": ["示例别名"]})
            seed_initial_assets(conn)
            assert [h["source"] for h in living.history(PROJ_ID)] == ["seed", "api"], "seed 覆盖前先存旧版本"
        finally:
            conn.close()


def test_recall_hit_counter_is_not_a_content_change():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _card(tc)
            for _ in range(3):
                assert tc.post("/v1/recall", json={"query": "缓存层"}).json()["strategy"] == "LIVING_STATE_DIRECT"
            assert _history(tc) == []


def test_rollback_keeps_current_visibility_and_never_touches_scratchpad():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            conn = tc.app.state.conn
            _card(tc)
            _card(tc, "Valkey")
            tc.post("/v1/admin/living-state/visibility", json={"entity_id": ENTITY, "visibility": "group_ok", "dry_run": False})
            _log(tc, "一条与回滚无关的草稿 不能被动")
            scratch_before = conn.execute("SELECT * FROM scratchpad_log ORDER BY id").fetchall()

            out = _rollback(tc, to_version=1, dry_run=False).json()
            assert out["visibility_kept"] == "group_ok"
            card = _state(tc)
            assert card["current_decision"] == REDIS and card["metadata"]["visibility"] == "group_ok"
            assert conn.execute("SELECT * FROM scratchpad_log ORDER BY id").fetchall() == scratch_before

            _rollback(tc, to_version=2, dry_run=False)
            assert _state(tc)["metadata"]["visibility"] == "group_ok"

            tc.post("/v1/admin/living-state/visibility", json={"entity_id": ENTITY, "visibility": "private", "dry_run": False})
            _rollback(tc, to_version=1, dry_run=False)
            assert "visibility" not in _state(tc)["metadata"], "当前是私人就保持私人,不被历史里的 group_ok 带回"


def test_rollback_noop_targets_and_errors():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            assert _rollback(tc).status_code == 404, "卡不存在"
            _card(tc)
            assert _rollback(tc).status_code == 404, "没有历史"
            _card(tc, "Valkey")
            assert _rollback(tc, to_version=99).status_code == 404
            assert _rollback(tc, history_id=9999).status_code == 404
            assert _rollback(tc, to_version=1, history_id=1).status_code == 422

            [snap] = _history(tc)
            same = _rollback(tc, history_id=snap["history_id"], dry_run=False).json()
            assert same["changed"] is True
            _card(tc, REDIS)
            noop = _rollback(tc, history_id=snap["history_id"], dry_run=False).json()
            assert noop["changed"] is False and noop["to"]["version"] == noop["from"]["version"] + 1
            assert _state(tc)["version"] == noop["from"]["version"], "内容相同不落库、不涨版本"


def test_rollback_requires_credentials_and_history_endpoint_404():
    with tempfile.TemporaryDirectory() as d:
        app = _app(d)
        with _env(CORTEX_AGENT_TOKENS="tokA:agentA"):
            with TestClient(app) as tc:
                auth = {"Authorization": "Bearer tokA"}
                for call in (
                    lambda h: tc.post("/v1/admin/living-state/rollback", json={"entity_id": ENTITY}, headers=h),
                    lambda h: tc.get(f"/v1/living-state/{ENTITY}/history", headers=h),
                ):
                    assert call({}).status_code == 401
                    assert call({"Authorization": "Bearer no"}).status_code == 403
                assert tc.get(f"/v1/living-state/{ENTITY}/history", headers=auth).status_code == 404


def test_consolidate_dry_run_is_a_true_preview_and_writes_nothing():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        archive = os.path.join(d, "cold")
        with TestClient(_app(d)) as tc:
            conn = tc.app.state.conn
            _card(tc)
            _log(tc, f"{ENTITY} 改为 {MEMCACHED}")
            _log(tc, "proj:newcard 定案:全新的定案")
            _log(tc, "随便聊聊今天的天气", hint=None)
            before = _dump(conn)

            preview = tc.post("/v1/sleep/consolidate", json={"dry_run": True}).json()
            assert _dump(conn) == before, "dry_run 前后逐表一致"
            assert not os.path.exists(archive) or os.listdir(archive) == [], "dry_run 不写冷归档"
            assert not conn.in_transaction
            assert preview["status"] == "dry_run" and preview["dry_run"] is True
            assert preview["updated_states"] == [] and preview["archived_logs"] == 0 and preview["archive_file"] is None
            assert preview["would_archive_logs"] == 2
            assert sorted(preview["would_update_states"]) == [ENTITY, "proj:newcard"]
            by_id = {c["entity_id"]: c for c in preview["changes"]}
            swap = by_id[ENTITY]
            assert swap["change"] == "decision_replaced" and (swap["version_from"], swap["version_to"]) == (1, 2)
            assert (swap["decision_from"], swap["decision_to"]) == (REDIS, MEMCACHED)
            assert [t["plan"] for t in swap["tombstones_added"]] == [REDIS]
            assert by_id["proj:newcard"]["change"] == "new_card"

            real = tc.post("/v1/sleep/consolidate", json={}).json()
            assert real["status"] == "ok" and sorted(real["updated_states"]) == sorted(preview["would_update_states"])
            assert real["archived_logs"] == preview["would_archive_logs"]
            card = _state(tc)
            assert card["current_decision"] == swap["decision_to"] and card["version"] == swap["version_to"]
            assert card["tombstones"] == swap["tombstones_added"], "预览的墓碑与真实执行一致"


def test_consolidate_dry_run_with_nothing_pending_and_repeatable():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            conn = tc.app.state.conn
            first = tc.post("/v1/sleep/consolidate", json={"dry_run": True}).json()
            assert first["status"] == "dry_run" and first["changes"] == [] and first["would_update_states"] == []
            _log(tc, "随便聊聊", hint=None)
            before = _dump(conn)
            for _ in range(3):
                assert tc.post("/v1/sleep/consolidate", json={"dry_run": True}).json()["status"] == "dry_run"
            assert _dump(conn) == before


def test_dry_run_survives_a_failing_analysis_without_leaving_a_transaction():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        conn = init_db(os.path.join(d, "t.db"))
        try:
            ScratchpadManager(conn).append_log("s", "user", f"{ENTITY} 定案:方案甲", phase_hint="approval")
            before = _dump(conn)
            consolidator = DreamConsolidator(conn, archive_dir=os.path.join(d, "cold"))

            def boom(*args, **kwargs):
                raise RuntimeError("写卡失败")

            consolidator.living.update_decision = boom
            try:
                consolidator.run_consolidation(dry_run=True)
                raise AssertionError("应抛出")
            except RuntimeError:
                pass
            assert not conn.in_transaction and _dump(conn) == before
        finally:
            conn.close()


def test_history_table_is_added_to_an_old_database_in_place():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "old.db")
        conn = init_db(path)
        LivingStateManager(conn).update_decision(ENTITY, "缓存层", REDIS)
        conn.execute("DROP TABLE living_state_history")
        conn.commit()
        conn.close()
        raw = sqlite3.connect(path)
        assert raw.execute("SELECT 1 FROM sqlite_master WHERE name = 'living_state_history'").fetchone() is None
        raw.close()

        conn = init_db(path)
        try:
            assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'living_state_history'").fetchone()
            assert LivingStateManager(conn).get_state(ENTITY)["current_decision"] == REDIS, "存量卡不变"
            LivingStateManager(conn).update_decision(ENTITY, "缓存层", MEMCACHED)
            assert len(LivingStateManager(conn).history(ENTITY)) == 1
        finally:
            conn.close()


def test_old_style_tombstones_without_new_fields_still_load():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "t.db"))
        try:
            conn.execute(
                "INSERT INTO living_states (entity_id, display_name, version, status, current_decision, metadata_json, tombstones_json) "
                "VALUES (?, '缓存层', 3, 'ACTIVE', ?, '{}', ?)",
                (ENTITY, MEMCACHED, '[{"plan": "更早的方案", "killed_reason": "旧格式"}]'),
            )
            conn.commit()
            living = LivingStateManager(conn)
            state = living.update_decision(ENTITY, "缓存层", "Valkey", source="t")
            assert [t["plan"] for t in state["tombstones"]] == ["更早的方案", MEMCACHED]
            rolled = living.rollback(ENTITY, dry_run=False)
            assert rolled["changed"] is True
            assert [t["plan"] for t in living.get_state(ENTITY)["tombstones"]] == ["更早的方案"]
        finally:
            conn.close()


if __name__ == "__main__":
    test_redis_to_memcached_leaves_tombstone_history_and_rolls_back_and_forth()
    test_explicit_supersede_is_not_duplicated_and_unrelated_plan_adds_second_stone()
    test_no_tombstone_when_decision_unchanged_or_card_is_new()
    test_every_write_path_snapshots_with_its_source()
    test_rename_display_and_seed_snapshot_only_on_real_change()
    test_recall_hit_counter_is_not_a_content_change()
    test_rollback_keeps_current_visibility_and_never_touches_scratchpad()
    test_rollback_noop_targets_and_errors()
    test_rollback_requires_credentials_and_history_endpoint_404()
    test_consolidate_dry_run_is_a_true_preview_and_writes_nothing()
    test_consolidate_dry_run_with_nothing_pending_and_repeatable()
    test_dry_run_survives_a_failing_analysis_without_leaving_a_transaction()
    test_history_table_is_added_to_an_old_database_in_place()
    test_old_style_tombstones_without_new_fields_still_load()
    print("✅ Issue #78 墓碑、卡片历史、回滚与 dry_run 预览测试全部通过")
