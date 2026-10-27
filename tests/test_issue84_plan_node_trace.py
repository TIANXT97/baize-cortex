"""Issue #84:问题里带方案原话(含已被取代的旧方案)时,PPR 解析不出现役卡不能挡住后面的兜底层"""
import os
import tempfile
from contextlib import contextmanager

from fastapi.testclient import TestClient

from baize.core.temporal_graph import plan_node_id
from baize.server.app import create_app

ENTITY = "proj:cache"
OLD_PLAN = "使用Redis做缓存"
NEW_PLAN = "使用Memcached做缓存"
FACT = "Redis集群密码 存放在保险库里"
QUERY_OLD = "使用Redis做缓存 密码"
QUERY_NEW = "使用Memcached做缓存 当前状态"


@contextmanager
def _env(**values):
    saved = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
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


def _decide(tc, entity, plan):
    resp = tc.post("/v1/scratchpad/log", json={
        "session_id": "s1", "role": "user", "raw_text": f"{entity} 定案:{plan}", "phase_hint": "approval"})
    assert resp.status_code == 200, resp.text
    report = tc.post("/v1/sleep/consolidate", json={}).json()
    assert entity in report["updated_states"], report


def _fact(tc, text):
    resp = tc.post("/v1/cortex/add", json={"session_id": "s1", "content": text, "category": "fact", "reason": "r"})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _recall(tc, query, **extra):
    resp = tc.post("/v1/recall", json={"query": query, **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _flow(tc):
    """旧方案 Redis 被新方案 Memcached 取代,另有一条未固化的显式事实。"""
    _decide(tc, ENTITY, OLD_PLAN)
    _decide(tc, ENTITY, NEW_PLAN)
    return _fact(tc, FACT)


def _ids(out):
    return [s["entity_id"] for s in out["living_states"]]


def test_old_plan_wording_traces_to_the_current_plan_and_keeps_facts():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            fact_id = _flow(tc)
            conn = tc.app.state.conn
            active = [r[0] for r in conn.execute(
                "SELECT dst_id FROM v_active_graph_edges WHERE src_id = ? AND rel_type = 'decided_as'", (ENTITY,))]
            assert active == [plan_node_id(ENTITY, NEW_PLAN)], "旧方案的 decided_as 边已失效,图里只剩新方案"

            out = _recall(tc, QUERY_OLD, scope="dm")
            assert out["strategy"] == "PLAN_NODE_TRACE", out
            assert _ids(out)[0] == ENTITY
            card = out["living_states"][0]
            assert card["current_decision"] == NEW_PLAN, "返回的是现行方案,不是旧方案"
            assert OLD_PLAN not in card["current_decision"]
            assert out["superseded_plan_matched"] is True
            assert out["source_log_ids"] == [fact_id], "未固化的显式事实一并带出"
            assert any(s.get("source_log_id") == fact_id for s in out["living_states"])


def test_current_plan_wording_also_traces_to_the_card():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            _flow(tc)
            out = _recall(tc, QUERY_NEW)
            assert out["strategy"] == "PLAN_NODE_TRACE", out
            assert _ids(out) == [ENTITY] and out["superseded_plan_matched"] is False


def test_nothing_resolved_by_graph_falls_through_to_the_remaining_layers():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            conn = tc.app.state.conn
            # 孤立方案节点:有标签、没有任何 decided_as 边指向它,也没有所属的卡
            conn.execute("INSERT INTO graph_nodes (node_id, label, node_type) VALUES ('plan:orphan', '孤立旧方案', 'rule')")
            conn.commit()
            fact_id = _fact(tc, "孤立旧方案 的备份口令 存放在别处")
            out = _recall(tc, "孤立旧方案 备份口令")
            assert out["strategy"] == "SCRATCHPAD_EXPLICIT_FALLBACK", out
            assert out["source_log_ids"] == [fact_id]

            # 孤立节点既没有所属的卡、也没有任何事实命中时,结果是 NO_MATCH,而不是空的 PPR 结果
            conn.execute("INSERT INTO graph_nodes (node_id, label, node_type) VALUES ('plan:orphan2', '另一个孤立方案', 'rule')")
            conn.commit()
            empty = _recall(tc, "另一个孤立方案")
            assert empty["strategy"] == "NO_MATCH" and empty["living_states"] == [], empty


def test_normal_direct_and_ppr_hits_are_unchanged():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            _flow(tc)
            direct = _recall(tc, ENTITY)
            assert direct["strategy"] == "LIVING_STATE_DIRECT" or _ids(direct) == [ENTITY]

            tc.post("/v1/graph/node", json={"node_id": ENTITY, "label": "缓存层", "node_type": "project"})
            tc.post("/v1/graph/node", json={"node_id": "topic:x", "label": "外围话题", "node_type": "concept"})
            tc.post("/v1/graph/edge", json={
                "src_id": "topic:x", "dst_id": ENTITY, "rel_type": "depends_on", "valid_at": "2020-01-01 00:00:00"})
            ppr = _recall(tc, "外围话题 现状")
            assert ppr["strategy"] == "PPR_SPREADING" and ppr["resolved_state"]["entity_id"] == ENTITY
            assert "superseded_plan_matched" not in ppr


def test_group_scope_never_returns_private_cards_via_plan_nodes():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            _flow(tc)
            _fact(tc, "私有备注 只在私聊可见")
            group = _recall(tc, QUERY_OLD, scope="group")
            assert group["scope"] == "group" and group["living_states"] == [], group
            assert ENTITY in group["withheld_entity_ids"], "只记 id,不返回内容"
            assert OLD_PLAN not in str(group) and NEW_PLAN not in str(group) and "保险库" not in str(group)
            for leaked in ("provisional_facts", "facts", "source_log_ids", "resolved_state", "state"):
                assert not group.get(leaked), leaked

            # 标成群可见后,可以顺着方案节点命中,但仍不带 facts 与草稿
            assert tc.post("/v1/admin/living-state/visibility", json={
                "entity_id": ENTITY, "visibility": "group_ok", "dry_run": False}).status_code == 200
            visible = _recall(tc, QUERY_OLD, scope="group")
            assert [s["entity_id"] for s in visible["living_states"]] == [ENTITY]
            assert visible["living_states"][0]["current_decision"] == NEW_PLAN
            assert "保险库" not in str(visible) and not visible.get("provisional_facts")
            assert visible["withheld_entity_ids"] == []


def test_plan_trace_only_follows_decided_as_edges_to_active_cards():
    with tempfile.TemporaryDirectory() as d, _env(CORTEX_DAY_JUDGE="apply"):
        with TestClient(_app(d)) as tc:
            _flow(tc)
            conn = tc.app.state.conn
            # 同一个方案节点上的其它关系类型不当作所属关系
            conn.execute("INSERT INTO graph_nodes (node_id, label, node_type) VALUES ('topic:other', '其它话题', 'concept')")
            conn.execute(
                "INSERT INTO graph_edges (src_id, dst_id, rel_type, valid_at) VALUES ('topic:other', ?, 'mentions', '2020-01-01 00:00:00')",
                (plan_node_id(ENTITY, OLD_PLAN),))
            # 所属卡已归档(非 ACTIVE)就不返回
            conn.execute("UPDATE living_states SET status = 'ARCHIVED' WHERE entity_id = ?", (ENTITY,))
            conn.commit()
            out = _recall(tc, QUERY_OLD)
            assert ENTITY not in _ids(out) and out["strategy"] != "PLAN_NODE_TRACE", out


if __name__ == "__main__":
    test_old_plan_wording_traces_to_the_current_plan_and_keeps_facts()
    test_current_plan_wording_also_traces_to_the_card()
    test_nothing_resolved_by_graph_falls_through_to_the_remaining_layers()
    test_normal_direct_and_ppr_hits_are_unchanged()
    test_group_scope_never_returns_private_cards_via_plan_nodes()
    test_plan_trace_only_follows_decided_as_edges_to_active_cards()
    print("✅ Issue #84 方案节点追溯与兜底续走测试全部通过")
