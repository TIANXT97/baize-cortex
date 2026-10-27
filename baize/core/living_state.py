"""Layer 1: Living State Vault 核心实现

实现项目活体状态卡的查询、更新与墓碑归档。
Issue #78:每次卡片内容变化前先把旧版本存进 living_state_history;定案被新定案替换时自动给旧定案留墓碑。
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from baize.core.temporal_graph import BiTemporalGraphManager, plan_node_id
from baize.core.visibility import VISIBILITY_GROUP_OK, VISIBILITY_KEY, VISIBILITY_PRIVATE

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_SUPERSEDED_BY = 200
_MAX_SOURCE_LOG_IDS = 20
_HISTORY_COLUMNS = (
    "id, version, display_name, status, current_decision, metadata_json, tombstones_json, "
    "entity_updated_at, recorded_at, source, note"
)
_MIN_COVER_LEN = 2
AUTO_TOMBSTONE_REASON = "被新定案取代"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def _squash(text: Optional[str]) -> str:
    return " ".join((text or "").split())


def tombstone_covers(plan: str, decision: str) -> bool:
    """显式推翻的方案是否已经覆盖了旧定案:文本相等,或一方包含另一方(较短的至少 2 字)。"""
    a, b = _squash(plan), _squash(decision)
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= _MIN_COVER_LEN and (a in b or b in a))


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

    def snapshot(self, entity_id: str, source: str, note: Optional[str] = None) -> bool:
        """把卡片当前内容存一份进历史表(调用方在同一事务里随后改卡)。卡片不存在时返回 False。"""
        cur = self.conn.execute(
            """
            INSERT INTO living_state_history
                (entity_id, version, display_name, status, current_decision, metadata_json, tombstones_json,
                 entity_updated_at, recorded_at, source, note)
            SELECT entity_id, version, display_name, status, current_decision, metadata_json, tombstones_json,
                   updated_at, strftime('%Y-%m-%d %H:%M:%S', 'now'), ?, ?
            FROM living_states WHERE entity_id = ?
            """,
            (source, note, entity_id),
        )
        return cur.rowcount > 0

    def history(self, entity_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """卡片历史快照,新的在前。每行是该次改动之前的卡片内容。"""
        rows = self.conn.execute(
            f"SELECT {_HISTORY_COLUMNS} FROM living_state_history WHERE entity_id = ? ORDER BY id DESC LIMIT ?",
            (entity_id, max(1, int(limit))),
        ).fetchall()
        return [_history_entry(r) for r in rows]

    def rollback(
        self,
        entity_id: str,
        to_version: Optional[int] = None,
        history_id: Optional[int] = None,
        dry_run: bool = True,
        source: str = "rollback",
    ) -> Dict[str, Any]:
        """把卡片回滚到历史快照;回滚本身也先存一份历史,所以能再回滚回去。

        缺省目标 = 最近一条内容与当前不同的快照。to_version 取该版本号最新的一条快照,history_id 精确指定。
        版本号继续递增(不复用);展示名/定案/metadata/墓碑按快照恢复,可见性保留当前值;
        只动活体卡与 decided_as 图谱边,不碰草稿流与冷归档。
        """
        if to_version is not None and history_id is not None:
            raise ValueError("to_version 与 history_id 只能给一个")
        current = self.get_state(entity_id)
        if current is None:
            raise LookupError("living state not found")
        rows = [_history_entry(r) for r in self.conn.execute(
            f"SELECT {_HISTORY_COLUMNS} FROM living_state_history WHERE entity_id = ? ORDER BY id DESC",
            (entity_id,),
        ).fetchall()]
        current_key = _content_key(
            current["display_name"], current["current_decision"], current["metadata"], current["tombstones"]
        )
        if history_id is not None:
            target = next((r for r in rows if r["history_id"] == history_id), None)
            if target is None:
                raise LookupError("history not found")
        elif to_version is not None:
            target = next((r for r in rows if r["version"] == to_version), None)
            if target is None:
                raise LookupError("history version not found")
        else:
            target = next((r for r in rows if _content_key(
                r["display_name"], r["current_decision"], r["metadata"], r["tombstones"]
            ) != current_key), None)
            if target is None:
                raise LookupError("no earlier version with different content")

        metadata = {k: v for k, v in target["metadata"].items() if k != VISIBILITY_KEY}
        kept_visibility = current["metadata"].get(VISIBILITY_KEY)
        if kept_visibility is not None:
            metadata[VISIBILITY_KEY] = kept_visibility
        changed = _content_key(
            target["display_name"], target["current_decision"], metadata, target["tombstones"]
        ) != current_key
        has_edges = self.conn.execute(
            "SELECT 1 FROM graph_edges WHERE src_id = ? AND rel_type = 'decided_as' LIMIT 1", (entity_id,)
        ).fetchone() is not None
        report: Dict[str, Any] = {
            "entity_id": entity_id, "dry_run": dry_run, "changed": changed,
            "target": {
                "history_id": target["history_id"], "version": target["version"],
                "recorded_at": target["recorded_at"], "source": target["source"],
            },
            "from": {
                "version": current["version"], "display_name": current["display_name"],
                "current_decision": current["current_decision"], "tombstones": len(current["tombstones"]),
            },
            "to": {
                "version": current["version"] + 1, "display_name": target["display_name"],
                "current_decision": target["current_decision"], "tombstones": len(target["tombstones"]),
            },
            "visibility_kept": kept_visibility or VISIBILITY_PRIVATE,
            "graph": ("would_sync" if has_edges else "no_decided_as_edges") if changed else "unchanged",
        }
        if dry_run or not changed:
            return report

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.snapshot(entity_id, source, f"回滚到历史快照 #{target['history_id']}(v{target['version']})之前的状态")
            self.conn.execute(
                """
                UPDATE living_states
                SET version = ?, display_name = ?, current_decision = ?, metadata_json = ?, tombstones_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE entity_id = ?
                """,
                (
                    current["version"] + 1, target["display_name"], target["current_decision"],
                    json.dumps(metadata, ensure_ascii=False), json.dumps(target["tombstones"], ensure_ascii=False),
                    entity_id,
                ),
            )
            if has_edges:
                self._sync_decision_edge(entity_id, target["display_name"], target["current_decision"])
                report["graph"] = "synced"
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return report

    def _sync_decision_edge(self, entity_id: str, display_name: str, decision: str) -> None:
        """让现役 decided_as 边指向当前定案(与夜间整理同口径);调用方负责事务。"""
        graph = BiTemporalGraphManager(self.conn)
        now = self.conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now')").fetchone()[0]
        plan_id = plan_node_id(entity_id, decision)
        graph.upsert_node(entity_id, display_name, "project", commit=False)
        graph.upsert_node(plan_id, decision, "rule", commit=False)
        stale = self.conn.execute(
            "SELECT dst_id FROM v_active_graph_edges WHERE src_id = ? AND rel_type = 'decided_as' AND dst_id != ?",
            (entity_id, plan_id),
        ).fetchall()
        for (dst,) in stale:
            graph.invalidate_edge(entity_id, dst, "decided_as", now, commit=False)
        active = self.conn.execute(
            "SELECT 1 FROM v_active_graph_edges WHERE src_id = ? AND dst_id = ? AND rel_type = 'decided_as' LIMIT 1",
            (entity_id, plan_id),
        ).fetchone()
        if active is None:
            graph.add_edge(entity_id, plan_id, "decided_as", valid_at=now, commit=False)

    def rename_display(self, entity_id: str, display_name: str, source: str = "backfill_display_names") -> None:
        current = self.get_state(entity_id)
        if current is None or current["display_name"] == display_name:
            return
        self.snapshot(entity_id, source)
        self.conn.execute(
            "UPDATE living_states SET display_name = ? WHERE entity_id = ?",
            (display_name, entity_id),
        )
        self.conn.commit()

    def set_visibility(
        self, entity_id: str, visibility: str, commit: bool = True, source: str = "admin:visibility"
    ) -> Optional[Dict[str, Any]]:
        """只改 metadata.visibility,不动版本、定案与 updated_at;private 即移除该键(缺省即私人)。"""
        current = self.get_state(entity_id)
        if current is None:
            return None
        metadata = dict(current["metadata"])
        if visibility == VISIBILITY_GROUP_OK:
            metadata[VISIBILITY_KEY] = VISIBILITY_GROUP_OK
        else:
            metadata.pop(VISIBILITY_KEY, None)
        if metadata == current["metadata"]:
            return current
        self.snapshot(entity_id, source)
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
        *,
        source: str = "api",
        source_log_ids: Optional[List[int]] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """就地更新定案,先存旧版本进历史;被取代的方案追加进墓碑区。

        显式给了 superseded_plan 就留那一条;旧定案没被它覆盖(或根本没给)而新定案不同时,
        自动再给旧定案留一条,避免「改为 Y」时旧方案无声消失。
        """
        cur = self.conn.cursor()
        current = self.get_state(entity_id)
        if current is not None:
            self.snapshot(entity_id, source, note)

        tombstones: List[Dict[str, Any]] = current["tombstones"] if current else []
        version = (current["version"] + 1) if current else 1
        merged_metadata = {**(current["metadata"] if current else {}), **(metadata or {})}

        stamp: Dict[str, Any] = {
            "superseded_by": _squash(new_decision)[:_MAX_SUPERSEDED_BY], "at": _now(), "source": source,
        }
        if source_log_ids:
            stamp["source_log_ids"] = list(source_log_ids)[:_MAX_SOURCE_LOG_IDS]

        if superseded_plan:
            tombstones.append({
                "plan": superseded_plan,
                "killed_reason": killed_reason or "推翻更新",
                "version": version - 1,
                **stamp,
            })
        old_decision = current["current_decision"] if current else ""
        if (
            _squash(old_decision)
            and _squash(old_decision) != _squash(new_decision)
            and not (superseded_plan and tombstone_covers(superseded_plan, old_decision))
        ):
            tombstones.append({
                "plan": old_decision, "killed_reason": AUTO_TOMBSTONE_REASON, "version": version - 1, **stamp,
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


def _content_key(display_name: str, decision: str, metadata: Dict[str, Any], tombstones: List[Any]) -> str:
    """回滚比较用的内容指纹:可见性是用户手动定的,不算卡片内容。"""
    meta = {k: v for k, v in metadata.items() if k != VISIBILITY_KEY}
    return json.dumps([display_name, decision, meta, tombstones], ensure_ascii=False, sort_keys=True)


def _history_entry(row) -> Dict[str, Any]:
    return {
        "history_id": row[0], "version": row[1], "display_name": row[2], "status": row[3],
        "current_decision": row[4], "metadata": _load_json(row[5], {}), "tombstones": _load_json(row[6], []),
        "entity_updated_at": row[7], "recorded_at": row[8], "source": row[9], "note": row[10],
    }


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
