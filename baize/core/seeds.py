"""预置核心工程真理档案:模具知识库的现役活体卡与双时态拓扑"""
import json
import sqlite3

from baize.core.temporal_graph import BiTemporalGraphManager

PROJ_ID = "proj:glass_mold_knowledge"
_NODE_IDS = ["topic:aliyun", PROJ_ID, "plan:paddle_slice", "plan:manual_dict"]


def seed_initial_assets(conn: sqlite3.Connection) -> None:
    graph = BiTemporalGraphManager(conn)
    graph.upsert_node("topic:aliyun", "阿里云", "topic")
    graph.upsert_node(PROJ_ID, "玻璃与模具工程知识库", "project")
    graph.upsert_node("plan:paddle_slice", "飞桨切片方案", "rule")
    graph.upsert_node("plan:manual_dict", "人肉拆425张图做特征字典", "rule")

    tombstones = [{
        "plan": "人肉拆425张图做特征字典",
        "killed_reason": "资料增至1500张,人工标注成本爆炸且易错",
    }]
    conn.execute(
        """
        INSERT INTO living_states (entity_id, display_name, version, status, current_decision, metadata_json, tombstones_json, updated_at)
        VALUES (?, ?, 1, 'ACTIVE', ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(entity_id) DO UPDATE SET
            display_name = excluded.display_name,
            version = excluded.version,
            status = excluded.status,
            current_decision = excluded.current_decision,
            metadata_json = excluded.metadata_json,
            tombstones_json = excluded.tombstones_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            PROJ_ID,
            "玻璃与模具工程知识库",
            "整页切片 + 飞桨描述强耦合 + 飞书折叠抽屉按需出图",
            json.dumps({"aliases": ["模具知识库", "玻璃模具"]}, ensure_ascii=False),
            json.dumps(tombstones, ensure_ascii=False),
        ),
    )
    conn.commit()

    placeholders = ",".join("?" * len(_NODE_IDS))
    conn.execute(
        f"DELETE FROM graph_edges WHERE src_id IN ({placeholders}) OR dst_id IN ({placeholders})",
        _NODE_IDS + _NODE_IDS,
    )
    conn.commit()

    valid_at = "2026-10-18 00:00:00"
    graph.add_edge("topic:aliyun", PROJ_ID, "mentions", valid_at)
    graph.add_edge(PROJ_ID, "plan:paddle_slice", "decided_as", valid_at, weight=2.0)
    graph.add_edge(PROJ_ID, "plan:manual_dict", "decided_as", valid_at, invalid_at="2026-10-20 00:00:00")
