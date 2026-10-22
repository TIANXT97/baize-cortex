"""cortex 插件:cortex_add 工具与本地便签镜像钩子。所有失败降级为入队,绝不阻塞主会话。"""
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from baize.client.outbox import Outbox
from baize.core.normalization import idempotency_key, normalize_content

log = logging.getLogger("baize.cortex_plugin")

CATEGORIES = {"fact", "preference", "rule", "lesson"}
MAX_CONTENT = 2000
MAX_REASON = 500
MIRROR_TARGETS = {"MEMORY.md", "USER.md"}
MIRROR_ORIGIN = "hermes_builtin_mirror"
QUEUED_MESSAGE = "已保存到本地队列，将自动同步，无需重试"

Transport = Callable[[str, Dict[str, Any], Dict[str, str], float], Tuple[int, Dict[str, Any], Dict[str, str]]]


def _parse_json(raw: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def urllib_transport(url: str, body: Dict[str, Any], headers: Dict[str, str], timeout: float):
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, _parse_json(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, _parse_json(exc.read()), dict(exc.headers)


def _retry_after(headers: Dict[str, str]) -> Optional[float]:
    for name, value in headers.items():
        if name.lower() == "retry-after":
            try:
                return max(0.0, float(value))
            except ValueError:
                return None
    return None


class CortexClient:
    def __init__(
        self,
        base_url: str,
        token: Optional[str],
        session_id: str,
        outbox: Outbox,
        transport: Optional[Transport] = None,
        timeout: float = 3.0,
        path: str = "/v1/cortex/add",
    ):
        self._url = base_url.rstrip("/") + path
        self._token = token
        self._session_id = session_id
        self._outbox = outbox
        self._transport = transport or urllib_transport
        self._timeout = timeout
        self._outbox.sender = self._send_envelope

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _send_envelope(self, envelope: Dict[str, Any]) -> Tuple[int, Optional[float]]:
        status, _, headers = self._transport(self._url, envelope["payload"], self._headers(), self._timeout)
        return status, _retry_after(headers)

    def cortex_add(
        self, content: str, category: str, reason: str, supersedes_id: Optional[int] = None
    ) -> Dict[str, Any]:
        normalized = normalize_content(content or "")
        if not normalized:
            return {"status": "error", "message": "content 不能为空"}
        if len(normalized) > MAX_CONTENT:
            return {"status": "error", "message": f"content 超过 {MAX_CONTENT} 字符"}
        if category not in CATEGORIES:
            return {"status": "error", "message": f"category 必须是 {sorted(CATEGORIES)} 之一"}
        if len(reason or "") > MAX_REASON:
            return {"status": "error", "message": f"reason 超过 {MAX_REASON} 字符"}
        return self._deliver(normalized, category, reason or "", supersedes_id, None)

    def on_memory_write(
        self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        try:
            if (metadata or {}).get("origin") == "writeback":
                return
            if target not in MIRROR_TARGETS or action == "remove":
                return
            normalized = normalize_content(content or "")[:MAX_CONTENT]
            if not normalized:
                return
            reason = f"{MIRROR_ORIGIN}:{action}:{target}"[:MAX_REASON]
            payload = self._payload(normalized, "fact", reason, None, MIRROR_ORIGIN)
            self._outbox.enqueue(payload, payload["idempotency_key"])
        except Exception:
            log.exception("memory 镜像入队失败,已忽略")

    def _payload(
        self, content: str, category: str, reason: str, supersedes_id: Optional[int], origin: Optional[str]
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "session_id": self._session_id,
            "content": content,
            "category": category,
            "reason": reason,
            "idempotency_key": idempotency_key(self._session_id, content),
        }
        if supersedes_id is not None:
            payload["supersedes_id"] = supersedes_id
        if origin:
            payload["origin"] = origin
        return payload

    def _deliver(
        self, content: str, category: str, reason: str, supersedes_id: Optional[int], origin: Optional[str]
    ) -> Dict[str, Any]:
        payload = self._payload(content, category, reason, supersedes_id, origin)
        key = payload["idempotency_key"]
        try:
            status, body, _ = self._transport(self._url, payload, self._headers(), self._timeout)
        except Exception:
            return self._queue(payload, key)
        if 200 <= status < 300:
            return {
                "status": "ok",
                "id": body.get("id"),
                "deduplicated": bool(body.get("deduplicated")),
                "idempotency_key": key,
            }
        if status in (400, 413, 422):
            return {"status": "rejected", "message": str(body.get("detail", "内容不可接受"))}
        return self._queue(payload, key)

    def _queue(self, payload: Dict[str, Any], key: str) -> Dict[str, Any]:
        try:
            self._outbox.enqueue(payload, key)
        except Exception:
            log.exception("outbox 入队失败")
            return {"status": "error", "message": "本地队列写入失败", "idempotency_key": key}
        return {"status": "queued", "idempotency_key": key, "message": QUEUED_MESSAGE}
