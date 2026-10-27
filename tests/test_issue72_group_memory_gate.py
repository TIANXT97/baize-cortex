"""Issue #72:群聊记忆闸——只认显式 chat_type,私人卡群里绝对不带,旧服务端无回显时群聊不注入"""
import logging
import os
import tempfile

from fastapi.testclient import TestClient

from baize.client.outbox import Outbox
from baize.client.plugin import CortexClient, SessionScopes
from baize.core.consolidator import DreamConsolidator
from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import ScratchpadManager
from baize.core.seeds import PROJ_ID, seed_initial_assets
from baize.core.visibility import scope_from_chat_type
from baize.server.app import create_app
from baize.storage.db import init_db

# 网关真实的 session_id 格式与 Hermes initialize 传入的身份字段
GATEWAY_SESSION = "20000101_000000_0a0b0c0d"
GATEWAY_KWARGS = {"platform": "feishu", "chat_id": "oc_test_chat", "gateway_session_key": "agent:main:feishu:group:oc_test_chat:on_x"}

PRIVATE_ID, PRIVATE_NAME, PRIVATE_TEXT = "proj:private_cinema", "私人影院", "私有备注排最后且严禁外传"
OPEN_ID, OPEN_NAME, OPEN_TEXT = "proj:open_router", "公开路由", "路由分流走备用链路"
QUERY_BOTH = "私人影院 公开路由"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(f"{record.levelname} {record.name} {record.getMessage()}")


def _capture(*names):
    handler = _Capture()
    loggers = [logging.getLogger(n) for n in names]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    return handler, loggers


def _release(handler, loggers):
    for lg in loggers:
        lg.removeHandler(handler)


def _app(d):
    return create_app(os.path.join(d, "p.db"), os.path.join(d, "cold"))


def _card(tc, entity, name, decision, metadata=None):
    body = {"entity_id": entity, "display_name": name, "new_decision": decision}
    if metadata is not None:
        body["metadata"] = metadata
    assert tc.post("/v1/living-state/decision", json=body).status_code == 200


def _populate(tc):
    _card(tc, PRIVATE_ID, PRIVATE_NAME, PRIVATE_TEXT, {"facts": [{"id": 1, "text": "私有事实甲"}]})
    _card(tc, OPEN_ID, OPEN_NAME, OPEN_TEXT, {"facts": [{"id": 2, "text": "混入的会话事实乙"}]})
    r = tc.post("/v1/admin/living-state/visibility", json={"entity_id": OPEN_ID, "visibility": "group_ok", "dry_run": False})
    assert r.status_code == 200, r.text


def _client(tc, d, session=GATEWAY_SESSION):
    def transport(url, body, headers, timeout):
        resp = tc.post(url.split("testserver", 1)[1], json=body, headers=headers)
        return resp.status_code, resp.json(), dict(resp.headers)

    return CortexClient("http://testserver", None, session, Outbox(os.path.join(d, "o.jsonl")), transport=transport)


def test_scope_from_chat_type_is_fail_closed():
    for dm in ("dm", "DM", " p2p ", "private", "direct"):
        assert scope_from_chat_type(dm) == "dm", dm
    for other in ("group", "forum", "channel", "thread", "", None, 0, [], "dm2", "unknown"):
        assert scope_from_chat_type(other) == "group", other


def test_server_group_scope_filters_private_cards_and_drafts():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _populate(tc)
            sid = "20000101_000000_0a0b0c0d"
            tc.post("/v1/scratchpad/log", json={"session_id": sid, "role": "user", "raw_text": "私人影院的草稿想法 暗号青檀"})
            tc.post("/v1/cortex/add", json={"session_id": sid, "content": "私人影院显式事实 暗号青檀", "category": "fact", "reason": "r"})

            legacy = tc.post("/v1/recall", json={"query": QUERY_BOTH}).json()
            assert "scope" not in legacy and any(s["entity_id"] == PRIVATE_ID for s in legacy["living_states"])

            dm = tc.post("/v1/recall", json={"query": QUERY_BOTH, "scope": "dm"}).json()
            assert dm["scope"] == "dm" and any(s["entity_id"] == PRIVATE_ID for s in dm["living_states"])

            for scope in ("group", "forum", "", "weird"):
                body = tc.post("/v1/recall", json={"query": QUERY_BOTH, "scope": scope}).json()
                assert body["scope"] == "group", scope
                assert [s["entity_id"] for s in body["living_states"]] == [OPEN_ID]
                assert PRIVATE_NAME not in str(body) and PRIVATE_TEXT not in str(body)
                assert "facts" not in body["living_states"][0]["metadata"], "卡片 facts 群里不出"
                assert "混入的会话事实乙" not in str(body)
                assert PRIVATE_ID in body["withheld_entity_ids"]

            for query in ("暗号青檀", "私人影院的草稿想法", "青檀 草稿 显式事实 影院"):
                body = tc.post("/v1/recall", json={"query": query, "scope": "group"}).json()
                assert body["living_states"] == [], query
                assert "facts" not in body and "provisional_facts" not in body and "source_log_ids" not in body
                assert "青檀" not in str(body)


def test_server_group_scope_hides_ppr_ranking_and_private_resolution():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _card(tc, "proj:ppr", "图谱项目", "图谱定案")
            tc.post("/v1/graph/node", json={"node_id": "proj:ppr", "label": "图谱项目", "node_type": "project"})
            tc.post("/v1/graph/node", json={"node_id": "topic:x", "label": "外围话题", "node_type": "concept"})
            tc.post("/v1/graph/edge", json={"src_id": "topic:x", "dst_id": "proj:ppr", "rel_type": "depends_on", "valid_at": "2020-01-01 00:00:00"})
            full = tc.post("/v1/recall", json={"query": "外围话题 现状", "scope": "dm"}).json()
            assert full["strategy"] == "PPR_SPREADING" and full["ranking"]

            body = tc.post("/v1/recall", json={"query": "外围话题 现状", "scope": "group"}).json()
            assert body["living_states"] == []
            for leaked in ("ranking", "seed_nodes", "resolved_state", "state"):
                assert not body.get(leaked), leaked
            assert "proj:ppr" not in str(body) and "图谱" not in str(body)


def test_server_group_scope_hides_recent_turns_and_candidates():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            tc.post("/v1/scratchpad/log", json={"session_id": GATEWAY_SESSION, "role": "user", "raw_text": "夜里讨论了机房迁移的具体日程安排"})
            dm = tc.post("/v1/recall", json={"query": "机房迁移的具体日程安排", "scope": "dm"}).json()
            assert dm["living_states"], "dm 下最近对话层应能召回"
            group = tc.post("/v1/recall", json={"query": "机房迁移的具体日程安排", "scope": "group"}).json()
            assert group["living_states"] == [] and group["strategy"] == "NO_MATCH"
            assert "机房迁移" not in str(group)


def test_client_matrix_with_gateway_session_ids():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _populate(tc)
            client = _client(tc, d)
            seen = set()
            cases = {
                "dm": True, "p2p": True,
                "group": False, "forum": False, "": False, None: False, "channel": False, "mystery": False,
            }
            for chat_type, sees_private in cases.items():
                sid = f"20261023_161320_{len(seen):08x}"
                seen.add(sid)
                client.bind_session(sid, chat_type)
                text = client.prefetch(QUERY_BOTH, session_id=sid, as_text=True)
                assert (PRIVATE_NAME in text) is sees_private, (chat_type, text)
                assert (OPEN_NAME in text) is True, (chat_type, text)
                assert ("混入的会话事实乙" in text) is sees_private, (chat_type, text)

            assert PRIVATE_NAME not in client.prefetch(QUERY_BOTH, session_id="never_bound_20000101_000000_0a0b0c0d", as_text=True)
            assert PRIVATE_NAME not in client.prefetch(QUERY_BOTH, as_text=True), "session_id 为空且未登记也按群聊"
            assert PRIVATE_NAME not in client.prefetch(QUERY_BOTH, session_id="oc_or_dm_looking_id", as_text=True)


def test_client_old_server_without_scope_echo_injects_nothing_in_group():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _populate(tc)

            def old_server(url, body, headers, timeout):
                body = {k: v for k, v in body.items() if k != "scope"}
                resp = tc.post(url.split("testserver", 1)[1], json=body, headers=headers)
                return resp.status_code, resp.json(), dict(resp.headers)

            client = CortexClient("http://testserver", None, GATEWAY_SESSION, Outbox(os.path.join(d, "o.jsonl")), transport=old_server)
            handler, loggers = _capture("baize.cortex_plugin")
            try:
                client.bind_session(GATEWAY_SESSION, "group")
                assert client.prefetch(QUERY_BOTH, as_text=True) == ""
                assert client.prefetch(QUERY_BOTH) == []
            finally:
                _release(handler, loggers)
            assert any("memory gate CLOSED" in m and "'group'" in m for m in handler.messages), handler.messages

            client.bind_session("dm_sess_20261023", "dm")
            assert PRIVATE_NAME in client.prefetch(QUERY_BOTH, session_id="dm_sess_20261023", as_text=True), "dm 在旧服务端上行为不变"


def test_client_second_layer_filters_a_misbehaving_server():
    leaky = {
        "scope": "group", "strategy": "LIVING_STATE_DIRECT", "state": {"entity_id": PRIVATE_ID, "current_decision": "不应出现"},
        "provisional_facts": [{"id": 9, "text": "草稿泄露"}], "facts": [{"id": 8, "text": "候选泄露"}],
        "living_states": [
            {"entity_id": PRIVATE_ID, "display_name": PRIVATE_NAME, "current_decision": "私有定案", "metadata": {}, "provisional": False},
            {"entity_id": "scratchpad:9", "display_name": "未固化记录", "current_decision": "草稿泄露", "metadata": {"visibility": "group_ok"}, "provisional": True, "source_log_id": 9},
            {"entity_id": OPEN_ID, "display_name": OPEN_NAME, "current_decision": OPEN_TEXT, "provisional": False,
             "metadata": {"visibility": "group_ok", "facts": [{"id": 2, "text": "混入的会话事实乙"}]}, "tombstones": [{"plan": "旧墓碑"}]},
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        client = CortexClient("http://x", None, GATEWAY_SESSION, Outbox(os.path.join(d, "o.jsonl")), transport=lambda *a: (200, leaky, {}))
        client.bind_session(GATEWAY_SESSION, "group")
        handler, loggers = _capture("baize.cortex_plugin")
        try:
            text = client.prefetch("任意查询内容", as_text=True)
        finally:
            _release(handler, loggers)
        assert OPEN_NAME in text and OPEN_TEXT in text
        for leaked in (PRIVATE_NAME, "私有定案", "草稿泄露", "候选泄露", "不应出现", "混入的会话事实乙", "旧墓碑"):
            assert leaked not in text, leaked
        line = next(m for m in handler.messages if "memory gate scope=group" in m)
        assert PRIVATE_ID in line and "scratchpad:9" in line and "'group'" in line
        assert PRIVATE_NAME not in line and "私有定案" not in line, "日志只写 entity_id,不写卡片内容"


def test_shared_scopes_survive_client_rebuild():
    with tempfile.TemporaryDirectory() as d:
        scopes = SessionScopes()
        first = CortexClient("http://x", None, "a", Outbox(os.path.join(d, "o1.jsonl")), transport=lambda *a: (200, {}, {}), scopes=scopes)
        first.bind_session("20261023_000000_aaaaaaaa", "dm")
        second = CortexClient("http://x", None, "b", Outbox(os.path.join(d, "o2.jsonl")), transport=lambda *a: (200, {}, {}), scopes=scopes)
        assert second.scope_for("20261023_000000_aaaaaaaa") == "dm", "initialize 重建客户端不丢其他会话的映射"


def test_session_switch_inherits_only_for_same_chat_reasons():
    with tempfile.TemporaryDirectory() as d:
        client = CortexClient("http://x", None, "s0", Outbox(os.path.join(d, "o.jsonl")), transport=lambda *a: (200, {}, {}))
        client.bind_session("20261023_000000_aaaaaaaa", "dm")
        client.bind_session("20261023_000000_bbbbbbbb", None, parent_session_id="20261023_000000_aaaaaaaa", reason="compression")
        assert client.scope_for("20261023_000000_bbbbbbbb") == "dm"
        client.bind_session("20261023_000000_cccccccc", None, parent_session_id="20261023_000000_aaaaaaaa", reason="resume")
        assert client.scope_for("20261023_000000_cccccccc") == "group", "resume 可能换到别的聊天,不继承"
        client.bind_session("20261023_000000_dddddddd", "group", parent_session_id="20261023_000000_aaaaaaaa", reason="compression")
        assert client.scope_for("20261023_000000_dddddddd") == "group", "显式值优先于继承"
        client.bind_session("20261023_000000_eeeeeeee", None, parent_session_id="never_seen", reason="compression")
        assert client.scope_for("20261023_000000_eeeeeeee") == "group"


def test_visibility_endpoint_and_survives_consolidation_seed_and_decision_updates():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _card(tc, OPEN_ID, OPEN_NAME, OPEN_TEXT)
            get = lambda: tc.get(f"/v1/living-state/{OPEN_ID}").json()
            assert tc.post("/v1/admin/living-state/visibility", json={"entity_id": OPEN_ID, "visibility": "group_ok"}).json()["dry_run"] is True
            assert "visibility" not in get()["metadata"], "默认 dry_run 不落库"
            assert tc.post("/v1/admin/living-state/visibility", json={"entity_id": OPEN_ID, "visibility": "bogus"}).status_code == 422
            assert tc.post("/v1/admin/living-state/visibility", json={"entity_id": "proj:none", "visibility": "group_ok"}).status_code == 404

            before = get()
            out = tc.post("/v1/admin/living-state/visibility", json={"entity_id": OPEN_ID, "visibility": "group_ok", "dry_run": False}).json()
            assert out["before"] == "private" and out["after"] == "group_ok"
            after = get()
            assert after["metadata"]["visibility"] == "group_ok"
            assert (after["version"], after["current_decision"], after["updated_at"]) == (before["version"], before["current_decision"], before["updated_at"])

            _card(tc, OPEN_ID, OPEN_NAME, "新定案", {"aliases": ["别名甲"]})
            assert get()["metadata"]["visibility"] == "group_ok", "后续 decision 写入合并 metadata,不冲掉可见性"

            tc.post("/v1/admin/living-state/visibility", json={"entity_id": OPEN_ID, "visibility": "private", "dry_run": False})
            assert "visibility" not in get()["metadata"] and get()["metadata"]["aliases"] == ["别名甲"]


def test_visibility_survives_nightly_consolidation_and_reseed():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "c.db"))
        try:
            living = LivingStateManager(conn)
            living.update_decision("proj:glass_mold", "模具知识库", "旧方案", metadata={"aliases": ["模具"]})
            living.set_visibility("proj:glass_mold", "group_ok")
            ScratchpadManager(conn).append_log("day", "user", "proj:glass_mold 定案:飞桨切片", phase_hint="approval")
            report = DreamConsolidator(conn, archive_dir=os.path.join(d, "cold")).run_consolidation()
            assert "proj:glass_mold" in report["updated_states"]
            state = living.get_state("proj:glass_mold")
            assert state["current_decision"] != "旧方案" and state["metadata"]["visibility"] == "group_ok"

            seed_initial_assets(conn)
            seed_aliases = living.get_state(PROJ_ID)["metadata"]["aliases"]
            assert seed_aliases, "种子卡应带别名"
            assert "visibility" not in living.get_state(PROJ_ID)["metadata"], "seed 不替用户标可见"
            living.set_visibility(PROJ_ID, "group_ok")
            seed_initial_assets(conn)
            seeded = living.get_state(PROJ_ID)["metadata"]
            assert seeded["visibility"] == "group_ok" and seeded["aliases"] == seed_aliases
        finally:
            conn.close()


def test_replay_group_request_does_not_surface_private_cards():
    """重放 10-25 事故形态:群聊 + 网关 session_id,私人卡(默认无标记)与 general_decisions facts 都不能出来。"""
    with tempfile.TemporaryDirectory() as d:
        with TestClient(_app(d)) as tc:
            _card(tc, "proj:general_decisions", "通用决策", "", {"facts": [{"id": 1, "text": "本机清单基线"}]})
            _card(tc, PRIVATE_ID, PRIVATE_NAME, PRIVATE_TEXT)
            client = _client(tc, d)
            client.bind_session(GATEWAY_SESSION, "group")
            query = "请补充一下你的看法 私人影院 通用决策 清单基线"
            handler, loggers = _capture("baize.cortex_plugin", "baize.recall")
            try:
                assert client.prefetch(query, as_text=True) == ""
            finally:
                _release(handler, loggers)
            joined = "\n".join(handler.messages)
            assert PRIVATE_ID in joined and "proj:general_decisions" in joined and "memory gate scope=group" in joined
            assert PRIVATE_TEXT not in joined and "清单基线" not in joined

            client.bind_session("dm_session_20261026", "dm")
            dm_text = client.prefetch(query, session_id="dm_session_20261026", as_text=True)
            assert PRIVATE_NAME in dm_text and "清单基线" in dm_text


if __name__ == "__main__":
    test_scope_from_chat_type_is_fail_closed()
    test_server_group_scope_filters_private_cards_and_drafts()
    test_server_group_scope_hides_ppr_ranking_and_private_resolution()
    test_server_group_scope_hides_recent_turns_and_candidates()
    test_client_matrix_with_gateway_session_ids()
    test_client_old_server_without_scope_echo_injects_nothing_in_group()
    test_client_second_layer_filters_a_misbehaving_server()
    test_shared_scopes_survive_client_rebuild()
    test_session_switch_inherits_only_for_same_chat_reasons()
    test_visibility_endpoint_and_survives_consolidation_seed_and_decision_updates()
    test_visibility_survives_nightly_consolidation_and_reseed()
    test_replay_group_request_does_not_surface_private_cards()
    print("✅ Issue #72 群聊记忆闸测试全部通过")
