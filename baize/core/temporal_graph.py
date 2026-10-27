"""Layer 2: 双时态实体拓扑图管理器

负责实体节点写入、带时间戳关系的建立、失效标记与有效子图导出。
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


def plan_node_id(entity_id: str, decision: str) -> str:
    """定案方案节点 ID:sha1(实体|定案文本) 前 12 位。哈希口径不可改,否则已有的 decided_as 边会失联。"""
    digest = hashlib.sha1(f"{entity_id}|{decision}".encode("utf-8")).hexdigest()[:12]
    return f"plan:{digest}"


class BiTemporalGraphManager:
    def __init__(self, db_conn: sqlite3.Connection):
        self.conn = db_conn

    def upsert_node(
        self,
        node_id: str,
        label: str,
        node_type: str,
        properties: Optional[Dict[str, Any]] = None,
        commit: bool = True,
    ):
        """插入或更新实体节点。"""
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO graph_nodes (node_id, label, node_type, properties_json, created_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(node_id) DO UPDATE SET
                label = excluded.label,
                node_type = excluded.node_type,
                properties_json = excluded.properties_json
            """,
            (node_id, label, node_type, json.dumps(properties or {}))
        )
        if commit:
            self.conn.commit()

    def add_edge(
        self,
        src_id: str,
        dst_id: str,
        rel_type: str,
        valid_at: str,
        weight: float = 1.0,
        invalid_at: Optional[str] = None,
        commit: bool = True,
    ):
        """添加带时态区间的关系边。"""
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO graph_edges (src_id, dst_id, rel_type, weight, valid_at, invalid_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (src_id, dst_id, rel_type, weight, valid_at, invalid_at)
        )
        if commit:
            self.conn.commit()

    def invalidate_edge(
        self,
        src_id: str,
        dst_id: str,
        rel_type: str,
        invalid_at_time: str,
        commit: bool = True,
    ):
        """将某条关系边标记为失效（宣告旧事实退役）。"""
        cur = self.conn.cursor()
        cur.execute(
            """
            UPDATE graph_edges
            SET invalid_at = ?
            WHERE src_id = ? AND dst_id = ? AND rel_type = ? AND invalid_at IS NULL
            """,
            (invalid_at_time, src_id, dst_id, rel_type)
        )
        if commit:
            self.conn.commit()

    def get_active_subgraph(self) -> List[Tuple[str, str, float]]:
        """导出当前有效子图（只包含 invalid_at IS NULL 的现役边），专供 PPR 游走。"""
        cur = self.conn.cursor()
        cur.execute("SELECT src_id, dst_id, weight FROM v_active_graph_edges")
        return cur.fetchall()
