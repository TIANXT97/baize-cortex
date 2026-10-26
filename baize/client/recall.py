"""解析 /v1/recall 响应:兼容旧响应形态(state / resolved_state / facts)与新增的 living_states。"""
from typing import Any, Dict, List


def _state_items(state: Any) -> List[Dict[str, Any]]:
    if not isinstance(state, dict) or not state.get("entity_id"):
        return []
    entity_id = str(state["entity_id"])
    items = [{
        "id": entity_id,
        "kind": "living_state",
        "text": str(state.get("current_decision") or ""),
        "display_name": state.get("display_name"),
        "provisional": False,
    }]
    metadata = state.get("metadata")
    facts = metadata.get("facts") if isinstance(metadata, dict) else None
    if isinstance(facts, list):
        for fact in facts:
            if isinstance(fact, dict) and fact.get("text"):
                items.append({
                    "id": f"fact:{entity_id}:{fact.get('id')}",
                    "kind": "fact",
                    "text": str(fact["text"]),
                    "display_name": state.get("display_name"),
                    "provisional": False,
                })
    return items


def _provisional_items(rows: Any, kind: str = "fact") -> List[Dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    return [
        {
            "id": f"scratchpad:{row['id']}",
            "kind": kind,
            "text": str(row["text"]),
            "display_name": None,
            "provisional": True,
        }
        for row in rows
        if isinstance(row, dict) and row.get("id") is not None and row.get("text")
    ]


def _entry_items(entry: Any, kind: str = "fact") -> List[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return []
    if entry.get("provisional"):
        source = entry.get("source_log_id")
        if source is None or not entry.get("current_decision"):
            return []
        return _provisional_items([{"id": source, "text": entry["current_decision"]}], kind=kind)
    return _state_items(entry)


def extract_memories(response: Any) -> List[Dict[str, Any]]:
    if not isinstance(response, dict):
        return []
    provisional_kind = "candidate·未核实" if response.get("strategy") == "CANDIDATE_UNVERIFIED" else "fact"
    items: List[Dict[str, Any]] = []
    items += _state_items(response.get("state"))
    items += _state_items(response.get("resolved_state"))
    living_states = response.get("living_states")
    if isinstance(living_states, list):
        for entry in living_states:
            items += _entry_items(entry, kind=provisional_kind)
    items += _provisional_items(response.get("provisional_facts"))
    # Issue #44 PR4 硬要求:candidate 候选注入必须显式标注「未核实」,防止被当定案引用
    items += _provisional_items(
        response.get("facts"),
        kind=provisional_kind,
    )

    seen = set()
    unique = []
    for item in items:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)
    return unique
