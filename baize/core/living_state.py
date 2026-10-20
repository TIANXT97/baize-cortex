"""Layer 1: Living State Vault 核心实现

实现项目活体状态卡的查询、更新与墓碑归档。
"""
from __future__ import annotations
import json
import sqlite3
from typing import Any, Dict, List, Optional


class LivingStateManager:
    def __init__(self, db_conn: sqlite3.Connection):
        self.conn = db_conn

    def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """获取指定实体的现役活体状态卡（O(1) 主键直达）。"""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT entity_id, display_name, version, status, current_decision, metadata_json, tombstones_json, updated_at
            FROM living_states
            WHERE entity_id = ?
            """,
            (entity_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "entity_id": row[0],
            "display_name": row[1],
            "version": row[2],
            "status": row[3],
            "current_decision": row[4],
            "metadata": json.loads(row[5] or "{}"),
            "tombstones": json.loads(row[6] or "[]"),
            "updated_at": row[7],
        }

    def update_decision(
        self,
        entity_id: str,
        display_name: str,
        new_decision: str,
        superseded_plan: Optional[str] = None,
        killed_reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """就地更新定案，并将被取代的方案追加进墓碑区。"""
        cur = self.conn.cursor()
        current = self.get_state(entity_id)

        tombstones: List[Dict[str, Any]] = current["tombstones"] if current else []
        version = (current["version"] + 1) if current else 1

        if superseded_plan:
            tombstones.append({
                "plan": superseded_plan,
                "killed_reason": killed_reason or "推翻更新",
                "version": version - 1,
            })

        cur.execute(
            """
            INSERT INTO living_states (entity_id, display_name, version, status, current_decision, metadata_json, tombstones_json, updated_at)
            VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(entity_id) DO UPDATE SET
                version = excluded.version,
                display_name = excluded.display_name,
                current_decision = excluded.current_decision,
                metadata_json = excluded.metadata_json,
                tombstones_json = excluded.tombstones_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                entity_id,
                display_name,
                version,
                new_decision,
                json.dumps(metadata or {}),
                json.dumps(tombstones, ensure_ascii=False),
            )
        )
        self.conn.commit()
        return self.get_state(entity_id) or {}
