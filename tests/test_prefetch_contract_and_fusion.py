"""Issue #27:/v1/recall 的 living_states 兼容字段、宽匹配命中后与未固化 explicit 记录合并、插件侧全量解析"""
import os
import tempfile

from fastapi.testclient import TestClient

from baize.client.outbox import Outbox
from baize.client.plugin import CortexClient
from baize.client.recall import extract_memories
from baize.core.living_state import LivingStateManager
from baize.core.normalization import idempotency_key
from baize.core.scratchpad import ScratchpadManager
from baize.intelligence.recall_router import CognitiveRecallRouter
from baize.server.app import create_app
from baize.storage.db import init_db

NATURAL_QUERY = "白泽 Cortex 修复上线实测 commit 0b8b352"


def _app(d):
    return create_app(os.path.join(d, "p.db"), os.path.join(d, "cold"))


def _card(tc, entity="proj:baize", name="项目卡甲", decision="白泽 Cortex 对照定案"):
    resp = tc.post("/v1/living-state/decision", json={
        "entity_id": entity, "display_name": name, "new_decision": decision})
    assert resp.status_code == 200, resp.text


def _add(tc, text):
    resp = tc.post("/v1/cortex/add", json={"session_id": "s", "content": text, "category": "fact", "reason": "r"})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _recall(tc, query, **extra):
    resp = tc.post("/v1/recall", json={"query": query, **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_every_strategy_exposes_living_states_list():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            assert _recall(tc, "星际航行")["living_states"] == []

            _card(tc, name="阿尔法项目", decision="方案A")
            direct = _recall(tc, "阿尔法项目")
            assert direct["strategy"] == "LIVING_STATE_DIRECT"
            assert [s["entity_id"] for s in direct["living_states"]] == ["proj:baize"]
            assert direct["living_states"][0]["current_decision"] == "方案A"
            assert direct["living_states"][0]["provisional"] is False

            loose = _recall(tc, "方案A 怎么样")
            assert loose["strategy"] == "DECISION_TEXT_MATCH"
            assert [s["entity_id"] for s in loose["living_states"]] == ["proj:baize"]


def test_fallback_facts_are_exposed_as_provisional_living_states():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            lid = _add(tc, "口令是北斗七号")
            out = _recall(tc, "北斗七号")
            assert out["strategy"] == "SCRATCHPAD_EXPLICIT_FALLBACK"
            entry = out["living_states"][0]
            assert entry["provisional"] is True and entry["authoritative"] is False
            assert entry["source_log_id"] == lid
            assert entry["current_decision"] == "口令是北斗七号"


def test_ppr_resolved_state_is_exposed():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _card(tc, entity="proj:ppr", name="图谱项目", decision="图谱定案")
            tc.post("/v1/graph/node", json={"node_id": "proj:ppr", "label": "图谱项目", "node_type": "project"})
            tc.post("/v1/graph/node", json={"node_id": "topic:x", "label": "外围话题", "node_type": "concept"})
            tc.post("/v1/graph/edge", json={
                "src_id": "topic:x", "dst_id": "proj:ppr", "rel_type": "depends_on", "valid_at": "2020-01-01 00:00:00"})
            out = _recall(tc, "外围话题 现状")
            assert out["strategy"] == "PPR_SPREADING", out
            assert [s["entity_id"] for s in out["living_states"]] == ["proj:ppr"]


def test_loose_decision_match_no_longer_hides_unconsolidated_fact():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _card(tc)
            fid = _add(tc, "修复上线实测 commit 0b8b352 已验收")
            out = _recall(tc, NATURAL_QUERY)
            assert out["strategy"] == "DECISION_TEXT_MATCH", out
            assert out["state"]["entity_id"] == "proj:baize"
            assert out["source_log_ids"] == [fid]
            assert [f["id"] for f in out["provisional_facts"]] == [fid]
            assert "provisional" not in out and "authoritative" not in out
            ids = [s.get("source_log_id") for s in out["living_states"]]
            assert [s["entity_id"] for s in out["living_states"]][0] == "proj:baize"
            assert fid in ids


def test_narrow_queries_still_use_fallback_and_consolidated_fact_stops_being_provisional():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _card(tc)
            fid = _add(tc, "修复上线实测 commit 0b8b352 已验收")
            assert _recall(tc, "0b8b352")["strategy"] == "SCRATCHPAD_EXPLICIT_FALLBACK"
            assert _recall(tc, "修复上线实测")["source_log_ids"] == [fid]

            tc.post("/v1/sleep/consolidate", json={})
            out = _recall(tc, NATURAL_QUERY)
            assert out["strategy"] == "DECISION_TEXT_MATCH"
            assert "provisional_facts" not in out and "source_log_ids" not in out
            assert all(not s["provisional"] for s in out["living_states"])


def test_living_card_direct_hit_is_unchanged():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _card(tc, name="阿尔法项目", decision="方案A")
            _add(tc, "阿尔法项目口令是北斗七号")
            out = _recall(tc, "阿尔法项目")
            assert out["strategy"] == "LIVING_STATE_DIRECT"
            assert "provisional" not in out and "provisional_facts" not in out and "source_log_ids" not in out


def test_fusion_is_read_only():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "ro.db"))
        try:
            LivingStateManager(conn).update_decision("proj:baize", "项目卡甲", "白泽 Cortex 对照定案")
            sp = ScratchpadManager(conn)
            text = "修复上线实测 commit 0b8b352 已验收"
            sp.add_explicit("s", text, "fact", "r", idempotency_key("s", text), "default")
            before = conn.total_changes
            rows = conn.execute("SELECT * FROM scratchpad_log").fetchall()
            out = CognitiveRecallRouter(conn).recall(NATURAL_QUERY)
            assert out["strategy"] == "DECISION_TEXT_MATCH" and out["provisional_facts"]
            assert conn.total_changes == before and not conn.in_transaction
            assert conn.execute("SELECT * FROM scratchpad_log").fetchall() == rows
        finally:
            conn.close()


def test_extract_memories_parses_old_and_new_response_shapes():
    state = {
        "entity_id": "proj:a", "display_name": "甲", "current_decision": "定案甲",
        "metadata": {"facts": [{"id": 7, "text": "已固化事实", "category": "fact"}]},
    }
    old_shape = {"strategy": "DECISION_TEXT_MATCH", "state": state, "nodes": []}
    items = extract_memories(old_shape)
    assert [(i["kind"], i["provisional"]) for i in items] == [("living_state", False), ("fact", False)]
    assert items[0]["text"] == "定案甲" and items[1]["text"] == "已固化事实"

    fallback = {"strategy": "SCRATCHPAD_EXPLICIT_FALLBACK", "state": None,
                "facts": [{"id": 9, "text": "未固化事实", "category": "fact"}]}
    items = extract_memories(fallback)
    assert [(i["kind"], i["provisional"], i["id"]) for i in items] == [("fact", True, "scratchpad:9")]

    ppr = {"strategy": "PPR_SPREADING", "resolved_state": state, "ranking": []}
    assert [i["id"] for i in extract_memories(ppr)][0] == "proj:a"

    fused = {"state": state, "provisional_facts": [{"id": 9, "text": "未固化事实"}],
             "living_states": [{"entity_id": "proj:a", "current_decision": "定案甲", "display_name": "甲",
                                "metadata": {}, "provisional": False}]}
    ids = [i["id"] for i in extract_memories(fused)]
    assert ids.count("proj:a") == 1 and "scratchpad:9" in ids and "fact:proj:a:7" in ids

    for bad in (None, [], "x", {}, {"state": "bad", "facts": "bad", "living_states": [1, None]}):
        assert extract_memories(bad) == []


def test_prefetch_injects_memories_end_to_end_and_fails_open():
    with tempfile.TemporaryDirectory() as d:
        app = _app(d)
        with TestClient(app) as tc:
            _card(tc)
            fid = _add(tc, "修复上线实测 commit 0b8b352 已验收")

            def transport(url, body, headers, timeout):
                resp = tc.post(url.split("testserver", 1)[1], json=body, headers=headers)
                return resp.status_code, resp.json(), dict(resp.headers)

            ob = Outbox(os.path.join(d, "o.jsonl"))
            client = CortexClient("http://testserver", None, "sess", ob, transport=transport)
            items = client.prefetch(NATURAL_QUERY)
            assert items, "自动注入不应为空"
            assert items[0]["kind"] == "living_state" and items[0]["provisional"] is False
            assert any(i["provisional"] and i["id"] == f"scratchpad:{fid}" for i in items)

            def boom(url, body, headers, timeout):
                raise TimeoutError("down")

            assert CortexClient("http://testserver", None, "sess", ob, transport=boom).prefetch("x") == []
            bad = CortexClient("http://testserver", None, "sess", ob, transport=lambda *a: (503, {}, {}))
            assert bad.prefetch("x") == []


def test_prefetch_as_text_contract_and_group_fail_closed():
    with tempfile.TemporaryDirectory() as d:
        app = _app(d)
        with TestClient(app) as tc:
            _card(tc)
            fid = _add(tc, "修复上线实测 commit 0b8b352 已验收")

            def transport(url, body, headers, timeout):
                resp = tc.post(url.split("testserver", 1)[1], json=body, headers=headers)
                return resp.status_code, resp.json(), dict(resp.headers)

            ob = Outbox(os.path.join(d, "o.jsonl"))
            client = CortexClient("http://testserver", None, "sess", ob, transport=transport)
            # 1. 验证 as_text=True 契约返回 str
            text_out = client.prefetch(NATURAL_QUERY, as_text=True)
            assert isinstance(text_out, str), "as_text=True 时必须返回 str"
            assert text_out.strip(), "命中记忆时不应为空串"
            assert "commit 0b8b352" in text_out

            # 2. 验证群聊 fail-closed
            group_out = client.prefetch(NATURAL_QUERY, session_id="oc_group_123", as_text=True)
            assert group_out == "", "群聊会话必须 fail-closed 返回空串"
            group_raw = client.prefetch(NATURAL_QUERY, session_id="agent:session:group:456", as_text=False)
            assert group_raw == [], "群聊会话 raw 模式必须返回空列表"

if __name__ == "__main__":
    test_prefetch_as_text_contract_and_group_fail_closed()
    test_every_strategy_exposes_living_states_list()
    test_fallback_facts_are_exposed_as_provisional_living_states()
    test_ppr_resolved_state_is_exposed()
    test_loose_decision_match_no_longer_hides_unconsolidated_fact()
    test_narrow_queries_still_use_fallback_and_consolidated_fact_stops_being_provisional()
    test_living_card_direct_hit_is_unchanged()
    test_fusion_is_read_only()
    test_extract_memories_parses_old_and_new_response_shapes()
    test_prefetch_injects_memories_end_to_end_and_fails_open()
    print("✅ prefetch 契约与召回融合测试全部通过")
