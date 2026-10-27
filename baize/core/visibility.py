"""活体卡可见性与会话范围(scope):群聊里私人卡绝对不带,只认显式字段,拿不准一律按群聊。"""
from __future__ import annotations

from typing import Any, Dict, Optional

SCOPE_DM = "dm"
SCOPE_GROUP = "group"
VISIBILITY_KEY = "visibility"
VISIBILITY_GROUP_OK = "group_ok"
VISIBILITY_PRIVATE = "private"

_DM_CHAT_TYPES = frozenset({"dm", "p2p", "private", "direct"})
# 群聊视图只放行这些 metadata 键:facts 由夜间整理从任意会话(含私聊)折叠而来,无法证明安全,群里不带
_GROUP_METADATA_KEYS = ("aliases", VISIBILITY_KEY)
_GROUP_STATE_KEYS = (
    "entity_id", "display_name", "version", "status", "current_decision", "updated_at",
    "provisional", "authoritative", "score",
)


def scope_from_chat_type(chat_type: Any) -> str:
    """只有明确的私聊类型才是 dm;group、forum、空值、缺失、未知值一律 group(fail-closed)。"""
    if isinstance(chat_type, str) and chat_type.strip().lower() in _DM_CHAT_TYPES:
        return SCOPE_DM
    return SCOPE_GROUP


def normalize_scope(scope: Any) -> Optional[str]:
    """服务端入参:None 表示调用方未声明(旧客户端,行为不变);声明了但不是 dm 的一律按 group。"""
    if scope is None:
        return None
    if isinstance(scope, str) and scope.strip().lower() == SCOPE_DM:
        return SCOPE_DM
    return SCOPE_GROUP


def is_group_visible(state: Any) -> bool:
    """只有 metadata.visibility 显式等于 group_ok 的卡群里可带;缺省即私人。"""
    metadata = state.get("metadata") if isinstance(state, dict) else None
    return isinstance(metadata, dict) and metadata.get(VISIBILITY_KEY) == VISIBILITY_GROUP_OK


def visibility_of(state: Dict[str, Any]) -> str:
    return VISIBILITY_GROUP_OK if is_group_visible(state) else VISIBILITY_PRIVATE


def group_view(state: Dict[str, Any]) -> Dict[str, Any]:
    """群聊响应里的卡片白名单拷贝:去掉 facts、墓碑与其他自由文本 metadata。"""
    view = {key: state[key] for key in _GROUP_STATE_KEYS if key in state}
    metadata = state.get("metadata")
    view["metadata"] = (
        {key: metadata[key] for key in _GROUP_METADATA_KEYS if key in metadata}
        if isinstance(metadata, dict) else {}
    )
    return view
