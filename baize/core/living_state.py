"""Layer 1: Living State Vault 核心实现

实现项目活体状态卡的查询、更新与墓碑归档。
"""
from __future__ import annotations
import json
import sqlite3
from typing import Any, Dict, List, Optional

from baize.core.visibility import VISIBILITY_GROUP_OK, VISIBILITY_KEY


class LivingStateManager:
    def __init__(self, db_conn: sqlite3.Connection):
        self.conn = db_conn

    def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """获取指定实体的现役活体状态卡(O(1) 主键直达)。"""
        row = self.conn.execute(
            """
            SELECT entity_id, display_name, version, status, current_decision, metadata_json, tombstones_json, updated_at
            FROM living_states
            WHERE entity_id = ?
            """,
            (entity_id,)
        ).fetchone()
        return _row_to_state(row) if row else None

    def list_active_states(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT entity_id, display_name, version, status, current_decision, metadata_json, tombstones_json, updated_at
            FROM living_states
            WHERE status = 'ACTIVE'
            ORDER BY entity_id
            """
        ).fetchall()
        return [_row_to_state(r) for r in rows]

    def resolve_display_name(self, entity_id: str, proposed: Optional[str]) -> str:
        current = self.get_state(entity_id)
        if current is None or current["display_name"] == entity_id:
            return proposed or (current["display_name"] if current else entity_id)
        return current["display_name"]

    def rename_display(self, entity_id: str, display_name: str) -> None:
        self.conn.execute(
            "UPDATE living_states SET display_name = ? WHERE entity_id = ?",
            (display_name, entity_id),
        )
        self.conn.commit()

    def set_visibility(self, entity_id: str, visibility: str, commit: bool = True) -> Optional[Dict[str, Any]]:
        """只改 metadata.visibility,不动版本、定案与 updated_at;private 即移除该键(缺省即私人)。"""
        current = self.get_state(entity_id)
        if current is None:
            return None
        metadata = dict(current["metadata"])
        if visibility == VISIBILITY_GROUP_OK:
            metadata[VISIBILITY_KEY] = VISIBILITY_GROUP_OK
        else:
            metadata.pop(VISIBILITY_KEY, None)
        self.conn.execute(
            "UPDATE living_states SET metadata_json = ? WHERE entity_id = ?",
            (json.dumps(metadata, ensure_ascii=False), entity_id),
        )
        if commit:
            self.conn.commit()
        return self.get_state(entity_id)

    def update_decision(
        self,
        entity_id: str,
        display_name: str,
        new_decision: str,
        superseded_plan: Optional[str] = None,
        killed_reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        commit: bool = True,
    ) -> Dict[str, Any]:
        """就地更新定案，并将被取代的方案追加进墓碑区。"""
        cur = self.conn.cursor()
        current = self.get_state(entity_id)

        tombstones: List[Dict[str, Any]] = current["tombstones"] if current else []
        version = (current["version"] + 1) if current else 1
        merged_metadata = {**(current["metadata"] if current else {}), **(metadata or {})}

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
                json.dumps(merged_metadata, ensure_ascii=False),
                json.dumps(tombstones, ensure_ascii=False),
            )
        )
        if commit:
            self.conn.commit()
        return self.get_state(entity_id) or {}


def _load_json(raw: Optional[str], fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _row_to_state(row) -> Dict[str, Any]:
    return {
        "entity_id": row[0],
        "display_name": row[1],
        "version": row[2],
        "status": row[3],
        "current_decision": row[4],
        "metadata": _load_json(row[5], {}),
        "tombstones": _load_json(row[6], []),
        "updated_at": row[7],
    }
