"""cortex 插件:cortex_add 工具与本地便签镜像钩子。所有失败降级为入队,绝不阻塞主会话。"""
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from baize.client.outbox import Outbox
from baize.client.recall import extract_memories
from baize.core.normalization import idempotency_key, normalize_content
from baize.core.scratchpad import CANDIDATE_DECISION, CANDIDATE_FACT, CANDIDATE_HINT
from baize.intelligence.batch_judge import APPROVAL_CONFIDENCE_GATE, CANDIDATE_NOUL_GATE
from baize.intelligence.phase_detector import (
    _APPROVAL_KEYWORDS, _REJECTION_KEYWORDS, PhaseDetector,
)

log = logging.getLogger("baize.cortex_plugin")

CATEGORIES = {"fact", "preference", "rule", "lesson"}
MAX_CONTENT = 2000
MAX_REASON = 500
MIRROR_TARGETS = {"MEMORY.md", "USER.md"}
MIRROR_ORIGIN = "hermes_builtin_mirror"
QUEUED_MESSAGE = "已保存到本地队列，将自动同步，无需重试"
# Issue #56 P2-5(#44 B 项止血):白天快速门直接引用 PhaseDetector 本地规则词表,不再另写一份
GATE_KEYWORDS = _APPROVAL_KEYWORDS + _REJECTION_KEYWORDS

Transport = Callable[[str, Dict[str, Any], Dict[str, str], float], Tuple[int, Dict[str, Any], Dict[str, str]]]

# Issue #44 E 项:敏感信息脱敏在写入前完成。key 名命中即整值掩蔽;值形态(sk-/长hex/长base64)兜底。
_SECRET_KEY_RE = re.compile(r"token|key|secret|password|passwd|credential|auth", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(r"sk-[A-Za-z0-9_-]{8,}|[A-Fa-f0-9]{32,}|[A-Za-z0-9+/]{40,}={0,2}")
_MAX_VALUE_CHARS = 200


def _mask_value(key: str, value: Any) -> str:
    text = "" if value is None else str(value)
    if not text:
        return text
    if _SECRET_KEY_RE.search(key or ""):
        return "<REDACTED>"
    return _SECRET_VALUE_RE.sub("<REDACTED>", text)[:_MAX_VALUE_CHARS]


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
        phase_detector: Optional[Any] = None,
    ):
        self._url = base_url.rstrip("/") + path
        self._recall_url = base_url.rstrip("/") + "/v1/recall"
        self._log_url = base_url.rstrip("/") + "/v1/scratchpad/log"
        self._detector = phase_detector or PhaseDetector.from_env()
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

    def prefetch(self, query: str, top_k: int = 5, *, session_id: str = "", as_text: bool = False) -> Any:
        """Recall relevant memories via /v1/recall.
        
        When as_text=True, formats the memories into a newline-delimited prompt block
        matching Hermes MemoryProvider -> str contract. When False, returns the raw List[Dict].
        """
        if not query or len(query.strip()) < 2:
            return "" if as_text else []
        if session_id and (":group:" in session_id or session_id.startswith("oc_")):
            return "" if as_text else []
        try:
            status, body, _ = self._transport(
                self._recall_url, {"query": query.strip(), "top_k": top_k}, self._headers(), self._timeout
            )
            if not 200 <= status < 300:
                return "" if as_text else []
            memories = extract_memories(body)
            if not as_text:
                return memories
            if not memories:
                return ""
            lines = []
            for item in memories:
                kind = item.get("kind", "fact")
                display = item.get("display_name")
                prefix = f"[{display}] " if display else ""
                lines.append(f"- [{kind}] {prefix}{item.get('text', '')}")
            return "\n".join(lines)
        except Exception:
            log.exception("prefetch 失败,本轮不注入记忆")
            return "" if as_text else []

    def record_config_change(
        self, tool: str, key: str, before: Any, after: Any, stamp: Optional[str] = None
    ) -> Dict[str, Any]:
        """Issue #44 E 项(CTO 会诊 PR2):工具动作成功后的事后沉淀,不依赖事前判定是否定案。

        网关在配置类工具动作成功后调用;before/after 为 None 分别表达「未设置」「已移除」。
        """
        now = stamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        before_text = _mask_value(key, before) or "(未设置)"
        after_text = _mask_value(key, after) or "(已移除)"
        content = (
            f"配置变更: {tool or 'unknown'} 将 {key} 从「{before_text}」改为「{after_text}」({now} UTC)"
        )
        reason = f"config_change:{tool or 'unknown'}"[:MAX_REASON]
        return self._deliver(content, "rule", reason, None, "tool_config_change")

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", **_: Any) -> None:
        """Hermes MemoryProvider 契约:每轮写 Layer 0,user 行经快速门打标,assistant 行恒不打标。失败只记日志。"""
        session = session_id or self._session_id
        if user_content:
            hint, category, noul = self._classify(user_content)
            self._log_turn(session, "user", user_content, hint, noul, category)
        if assistant_content:
            self._log_turn(session, "assistant", assistant_content, None, 0.0, None)

    def _classify(self, text: str) -> Tuple[Optional[str], Optional[str], float]:
        # 快速门未命中留 NULL:批量判开启时自动 pending 等夜间判,关闭时归第 5 层普通轮次
        if not any(k in text for k in GATE_KEYWORDS):
            return None, None, 0.0
        try:
            phase, noul, _, confidence = self._detector.detect_phase_rich(text)
        except Exception:
            log.exception("PhaseDetector 判定失败,本轮不打标")
            return None, None, 0.0
        if phase == "decision_approved":
            if confidence >= APPROVAL_CONFIDENCE_GATE:
                return "approval", None, noul
            return CANDIDATE_HINT, CANDIDATE_DECISION, noul
        if phase == "rejection":
            return "rejection", None, noul
        if noul >= CANDIDATE_NOUL_GATE:
            return CANDIDATE_HINT, CANDIDATE_FACT, noul
        return None, None, noul

    def _log_turn(
        self, session: str, role: str, text: str, hint: Optional[str], noul: float, category: Optional[str]
    ) -> None:
        body: Dict[str, Any] = {
            "session_id": session, "role": role, "raw_text": text, "phase_hint": hint, "noul_score": noul,
        }
        if category:
            body["category"] = category
        try:
            status, resp, _ = self._transport(self._log_url, body, self._headers(), self._timeout)
            if not 200 <= status < 300:
                log.warning("scratchpad 写入被拒(HTTP %s): %s", status, resp.get("detail"))
        except Exception:
            log.exception("scratchpad 写入失败,已忽略")

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
