"""Phase A:鉴权覆盖面收口 —— 配置 CORTEX_AGENT_TOKENS 后 /v1/* 全端点统一 401/403,未配置时行为不变

Issue #41 遗留 2 的前置收口:启用 token 那天,/v1/admin/*、/v1/graph/*、/v1/living-state/decision
等破坏性端点必须与 /v1/cortex/add 同受凭据保护,否则启用清单只配两处环境变量仍留裸奔端点。
"""
import os
import tempfile

from fastapi.testclient import TestClient

from baize.server.app import create_app


def _requests():
    return [
        ("get", "/v1/living-states", None),
        ("get", "/v1/living-state/proj:x", None),
        ("post", "/v1/living-state/decision", {
            "entity_id": "proj:x", "display_name": "X", "new_decision": "d",
        }),
        ("post", "/v1/graph/node", {"node_id": "n:x", "label": "X", "node_type": "concept"}),
        ("post", "/v1/graph/edge", {
            "src_id": "n:x", "dst_id": "n:x", "rel_type": "belongs_to",
            "valid_at": "2026-10-24 00:00:00",
        }),
        ("post", "/v1/graph/invalidate", {
            "src_id": "n:x", "dst_id": "n:x", "rel_type": "belongs_to",
            "invalid_at": "2026-10-24 00:00:00",
        }),
        ("get", "/v1/graph/active-subgraph", None),
        ("post", "/v1/recall/ppr", {"seed_nodes": {"n:x": 1.0}}),
        ("post", "/v1/sleep/consolidate", {"dry_run": True}),
        ("post", "/v1/admin/backfill-display-names", {"dry_run": True}),
        ("post", "/v1/admin/purge-scratchpad", {"dry_run": True}),
        ("post", "/v1/admin/backfill-judge", {"after_date": "2026-10-23 00:00:00", "dry_run": True}),
        ("post", "/v1/admin/living-state/visibility", {
            "entity_id": "proj:x", "visibility": "private", "dry_run": True,
        }),
        ("post", "/v1/admin/expire-scratchpad", {"ids": [1], "dry_run": True}),
        ("get", "/v1/living-state/proj:x/history", None),
        ("post", "/v1/admin/living-state/rollback", {"entity_id": "proj:x", "dry_run": True}),
        ("post", "/v1/admin/seed", None),
    ]


def _send(tc, method, path, body, headers=None):
    call = getattr(tc, method)
    if body is None:
        return call(path, headers=headers)
    return call(path, json=body, headers=headers)


def test_all_v1_endpoints_require_token_when_configured():
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "c.db"), os.path.join(d, "cold"))
        os.environ["CORTEX_AGENT_TOKENS"] = "tokA:agentA"
        try:
            with TestClient(app) as tc:
                for method, path, body in _requests():
                    assert _send(tc, method, path, body).status_code == 401, f"{path} 未带凭据应 401"
                    assert _send(
                        tc, method, path, body, headers={"Authorization": "Bearer nope"}
                    ).status_code == 403, f"{path} 错误凭据应 403"
                    ok = _send(tc, method, path, body, headers={"Authorization": "Bearer tokA"})
                    assert ok.status_code not in (401, 403), f"{path} 正确凭据被拒: {ok.status_code}"
        finally:
            del os.environ["CORTEX_AGENT_TOKENS"]


def test_endpoints_still_open_when_tokens_unset():
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "c.db"), os.path.join(d, "cold"))
        with TestClient(app) as tc:
            assert tc.get("/health").status_code == 200
            assert tc.get("/v1/living-states").status_code == 200
            assert tc.post("/v1/sleep/consolidate", json={"dry_run": True}).status_code == 200
            assert tc.post(
                "/v1/admin/purge-scratchpad", json={"dry_run": True}
            ).status_code == 200
            wrote = tc.post("/v1/living-state/decision", json={
                "entity_id": "proj:x", "display_name": "X", "new_decision": "d",
            })
            assert wrote.status_code == 200
            assert app.state.conn.execute(
                "SELECT agent_id FROM living_states WHERE entity_id = 'proj:x'"
            ).fetchone()[0] is None


if __name__ == "__main__":
    test_all_v1_endpoints_require_token_when_configured()
    test_endpoints_still_open_when_tokens_unset()
    print("✅ Phase A 鉴权覆盖面测试全部通过")
