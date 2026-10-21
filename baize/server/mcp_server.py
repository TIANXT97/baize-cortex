"""白泽·Cortex 原生 MCP Server (stdio, JSON-RPC 2.0)

启动: python -m baize.server.mcp_server [--db PATH]
未安装官方 mcp 库,使用内置的 JSON-RPC 2.0 实现,消息按行分隔。
"""
import argparse
import json
import sqlite3
import sys
from typing import Any, Dict, Optional, TextIO

from baize.core.consolidator import DreamConsolidator
from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import ScratchpadManager
from baize.core.temporal_graph import BiTemporalGraphManager
from baize.intelligence.ppr_engine import PPREngine
from baize.intelligence.recall_router import CognitiveRecallRouter
from baize.storage.db import init_db

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "baize-cortex"
SERVER_VERSION = "2.0.0-alpha"


class BaizeTools:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.living = LivingStateManager(conn)
        self.scratchpad = ScratchpadManager(conn)
        self.graph = BiTemporalGraphManager(conn)
        self.ppr = PPREngine()
        self.router = CognitiveRecallRouter(conn, self.ppr)

    def recall(self, args: Dict[str, Any]):
        return self.router.recall(args["query"], args.get("top_k", 5))

    def get_living_state(self, args: Dict[str, Any]):
        return self.living.get_state(args["entity_id"])

    def list_active_states(self, args: Dict[str, Any]):
        return self.living.list_active_states()

    def update_decision(self, args: Dict[str, Any]):
        return self.living.update_decision(
            args["entity_id"],
            args["display_name"],
            args["new_decision"],
            args.get("superseded_plan"),
            args.get("killed_reason"),
        )

    def scratchpad_log(self, args: Dict[str, Any]):
        log_id = self.scratchpad.append_log(
            args["session_id"], args["role"], args["raw_text"], args.get("phase_hint")
        )
        return {"id": log_id}

    def recall_ppr(self, args: Dict[str, Any]):
        energy = self.ppr.run_ppr(self.graph.get_active_subgraph(), args["seed_nodes"])
        return [{"node_id": node, "energy": score} for node, score in energy.items()]

    def consolidate_sleep(self, args: Dict[str, Any]):
        return DreamConsolidator(self.conn, archive_dir=args.get("archive_dir", "cold_archive")).run_consolidation(
            args.get("target_date")
        )


def _schema(properties: Dict[str, Any], required: list) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


_STR = {"type": "string"}

TOOLS: Dict[str, Dict[str, Any]] = {
    "baize_get_living_state": {
        "description": "获取指定实体的现役活体状态卡(含墓碑)",
        "inputSchema": _schema({"entity_id": _STR}, ["entity_id"]),
        "handler": BaizeTools.get_living_state,
    },
    "baize_list_active_states": {
        "description": "列出所有 ACTIVE 状态的活体状态卡",
        "inputSchema": _schema({}, []),
        "handler": BaizeTools.list_active_states,
    },
    "baize_update_decision": {
        "description": "更新定案,被取代的方案自动堆叠进墓碑区",
        "inputSchema": _schema(
            {
                "entity_id": _STR,
                "display_name": _STR,
                "new_decision": _STR,
                "superseded_plan": _STR,
                "killed_reason": _STR,
            },
            ["entity_id", "display_name", "new_decision"],
        ),
        "handler": BaizeTools.update_decision,
    },
    "baize_scratchpad_log": {
        "description": "追加一条白天瞬态草稿记录",
        "inputSchema": _schema(
            {"session_id": _STR, "role": _STR, "raw_text": _STR, "phase_hint": _STR},
            ["session_id", "role", "raw_text"],
        ),
        "handler": BaizeTools.scratchpad_log,
    },
    "baize_recall_ppr": {
        "description": "在现役有效子图上执行吸引子能量游走,返回排序结果",
        "inputSchema": _schema(
            {"seed_nodes": {"type": "object", "additionalProperties": {"type": "number"}}},
            ["seed_nodes"],
        ),
        "handler": BaizeTools.recall_ppr,
    },
    "baize_consolidate_sleep": {
        "description": "执行慢波睡眠合成:折叠相变候选、固化活体卡、切断旧图谱边,并冷归档热草稿",
        "inputSchema": _schema(
            {"target_date": _STR, "archive_dir": _STR},
            [],
        ),
        "handler": BaizeTools.consolidate_sleep,
    },
    "baize_recall": {
        "description": "统一认知召回:活体卡直达或 PPR 能量游走收敛至现役定案",
        "inputSchema": _schema(
            {"query": _STR, "top_k": {"type": "integer"}},
            ["query"],
        ),
        "handler": BaizeTools.recall,
    },
}


def _result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_content(text: str, is_error: bool) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call_tool(req_id: Any, params: Dict[str, Any], tools: BaizeTools) -> Dict[str, Any]:
    spec = TOOLS.get(params.get("name"))
    if spec is None:
        return _error(req_id, -32602, f"Unknown tool: {params.get('name')}")
    try:
        payload = spec["handler"](tools, params.get("arguments") or {})
    except Exception as exc:
        return _result(req_id, _tool_content(str(exc), is_error=True))
    return _result(req_id, _tool_content(json.dumps(payload, ensure_ascii=False), is_error=False))


def handle_message(msg: Any, tools: BaizeTools) -> Optional[Dict[str, Any]]:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(None, -32600, "Invalid Request")
    if "id" not in msg:
        return None
    req_id = msg["id"]
    method = msg["method"]
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _error(req_id, -32602, "Invalid params")
    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": [
            {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
            for name, spec in TOOLS.items()
        ]})
    if method == "tools/call":
        return _call_tool(req_id, params, tools)
    return _error(req_id, -32601, f"Method not found: {method}")


def serve(db_path: str, stdin: TextIO, stdout: TextIO) -> None:
    conn = init_db(db_path)
    tools = BaizeTools(conn)
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                response = _error(None, -32700, "Parse error")
            else:
                response = handle_message(msg, tools)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="白泽·Cortex MCP stdio server")
    parser.add_argument("--db", default="baize.db")
    args = parser.parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    serve(args.db, sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
