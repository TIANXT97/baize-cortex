"""cortex 插件:cortex_add 工具与本地便签镜像钩子。所有失败降级为入队,绝不阻塞主会话。"""
import json
import logging
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from baize.client.outbox import Outbox
from baize.client.recall import extract_memories
from baize.core.normalization import idempotency_key, normalize_content
from baize.core.scratchpad import CANDIDATE_DECISION, CANDIDATE_FACT, CANDIDATE_HINT
from baize.core.resident import SCOPE_GROUP_OK, TITLE as RESIDENT_TITLE, find_group_unsafe
from baize.core.visibility import SCOPE_DM, SCOPE_GROUP, group_view, is_group_visible, scope_from_chat_type
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
WRITE_FAILED_ORIGIN = "hermes_memory_write_failed"
_MEMORY_TARGET_FILES = {"memory": "MEMORY.md", "user": "USER.md"}
_FAILED_WRITE_ACTIONS = ("add", "replace")
# 超上限被 Hermes 拒绝的报错文案:add 是 "would exceed the limit",replace/批量是 "would put memory at"
_OVER_LIMIT_MARKERS = ("would put memory at", "would exceed the limit")
_MAX_FAILED_SEEN = 256
QUEUED_MESSAGE = "已保存到本地队列，将自动同步，无需重试"
# Issue #56 P2-5(#44 B 项止血):白天快速门直接引用 PhaseDetector 本地规则词表,不再另写一份
GATE_KEYWORDS = _APPROVAL_KEYWORDS + _REJECTION_KEYWORDS

# 只有这些切换原因说明新会话与旧会话在同一个聊天里,才能继承旧会话的 chat_type;resume 等可能换到别的聊天,不继承
_INHERIT_REASONS = frozenset({"compression", "new_session"})
_MAX_TRACKED_SESSIONS = 1024
# Issue #105:常驻层在会话开头取一次,慢了宁可不注入也不拖住开场
RESIDENT_TIMEOUT = 2.0
_MAX_RESIDENT_CACHE = 64

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


def urllib_get(url: str, headers: Dict[str, str], timeout: float):
    request = urllib.request.Request(url, headers=headers, method="GET")
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


def _json_dict(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def failed_memory_writes(messages: Any) -> List[Tuple[str, str]]:
    """本轮(最后一条 user 消息之后)里,Hermes 因超上限拒绝的 memory add/replace:[(MEMORY.md|USER.md, 内容)]。

    只认工具结果 success 不是 true 且报错含超上限文案的调用;批量 operations 整批失败,每个 add/replace 的内容都取。
    patch 的 new_text 只是片段,不是一条完整记忆,不取。"""
    if not isinstance(messages, list):
        return []
    start = 0
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            start = index + 1
    calls: Dict[str, Dict[str, Any]] = {}
    found: List[Tuple[str, str]] = []
    for message in messages[start:]:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict) and function.get("name") == "memory" and call.get("id"):
                    args = _json_dict(function.get("arguments"))
                    if args is not None:
                        calls[str(call["id"])] = args
        elif message.get("role") == "tool" and str(message.get("tool_call_id")) in calls:
            result = _json_dict(_message_text(message.get("content")))
            error = result.get("error") if result else None
            if result is None or result.get("success") is True or not isinstance(error, str):
                continue
            if not any(marker in error for marker in _OVER_LIMIT_MARKERS):
                continue
            args = calls[str(message.get("tool_call_id"))]
            target = _MEMORY_TARGET_FILES.get(str(args.get("target") or "memory"))
            operations = args.get("operations")
            ops = operations if isinstance(operations, list) and operations else [args]
            for op in ops:
                if not isinstance(op, dict) or op.get("action") not in _FAILED_WRITE_ACTIONS or target is None:
                    continue
                content = str(op.get("content") or op.get("new_text") or "").strip()
                if content and (target, content) not in found:
                    found.append((target, content))
    return found


class SessionScopes:
    """session_id -> Hermes chat_type。查不到、空值、认不出的值一律按群聊(fail-closed)。"""

    def __init__(self) -> None:
        self._chat_types: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def bind(self, session_id: str, chat_type: Any) -> None:
        if not session_id or not chat_type:
            return
        with self._lock:
            self._chat_types.pop(session_id, None)
            self._chat_types[session_id] = chat_type
            while len(self._chat_types) > _MAX_TRACKED_SESSIONS:
                self._chat_types.pop(next(iter(self._chat_types)))

    def inherit(self, session_id: str, parent_session_id: str, reason: str) -> None:
        if reason not in _INHERIT_REASONS or not parent_session_id:
            return
        with self._lock:
            known = session_id in self._chat_types
            parent = self._chat_types.get(parent_session_id)
        if not known and parent:
            self.bind(session_id, parent)

    def chat_type(self, session_id: str) -> Any:
        with self._lock:
            return self._chat_types.get(session_id)

    def scope(self, session_id: str) -> str:
        return scope_from_chat_type(self.chat_type(session_id))


def _group_body(body: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """群聊响应的客户端二次过滤:只留非草稿且显式 group_ok 的卡,去掉 facts,其余字段一律丢弃。"""
    kept: List[Dict[str, Any]] = []
    dropped: List[str] = []
    entries = body.get("living_states")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        if entry.get("provisional") or not is_group_visible(entry):
            dropped.append(str(entry.get("entity_id")))
            continue
        kept.append(group_view(entry))
    return {"scope": SCOPE_GROUP, "strategy": body.get("strategy"), "living_states": kept}, dropped


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
        scopes: Optional["SessionScopes"] = None,
        get_transport: Optional[Callable[[str, Dict[str, str], float], Tuple[int, Dict[str, Any], Dict[str, str]]]] = None,
    ):
        self._url = base_url.rstrip("/") + path
        self._recall_url = base_url.rstrip("/") + "/v1/recall"
        self._resident_url = base_url.rstrip("/") + "/v1/resident"
        self._get_transport = get_transport or urllib_get
        self._resident_cache: Dict[str, str] = {}
        self._failed_seen: Dict[Tuple[str, str], bool] = {}
        self._log_url = base_url.rstrip("/") + "/v1/scratchpad/log"
        self._detector = phase_detector or PhaseDetector.from_env()
        self._token = token
        self._session_id = session_id
        self._outbox = outbox
        self._transport = transport or urllib_transport
        self._timeout = timeout
        self._outbox.sender = self._send_envelope
        self._scopes = scopes if scopes is not None else SessionScopes()

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

    def bind_session(
        self, session_id: str, chat_type: Any = None, *, parent_session_id: str = "", reason: str = ""
    ) -> None:
        """记下会话的 chat_type(Hermes initialize / on_session_switch 传入)。显式值优先,否则按原因继承父会话。"""
        self._scopes.bind(session_id, chat_type)
        self._scopes.inherit(session_id, parent_session_id, reason)

    def scope_for(self, session_id: str = "") -> str:
        return self._scopes.scope(session_id or self._session_id)

    def recall_scoped(
        self, query: str, top_k: int = 5, *, session_id: str = "", timeout: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """带会话范围的召回:群聊只拿服务端回显 scope=group 的响应并再过滤一遍;拿不到回显一律不注入。"""
        sid = session_id or self._session_id
        chat_type = self._scopes.chat_type(sid)
        scope = scope_from_chat_type(chat_type)
        status, body, _ = self._transport(
            self._recall_url, {"query": query.strip(), "top_k": top_k, "scope": scope},
            self._headers(), timeout if timeout is not None else self._timeout,
        )
        if not 200 <= status < 300 or not isinstance(body, dict):
            return None
        if scope != SCOPE_GROUP:
            return body
        if body.get("scope") != SCOPE_GROUP:
            log.warning(
                "memory gate CLOSED: session=%s chat_type=%r scope=group 但服务端未回显 scope(旧版本?),本轮不注入",
                sid, chat_type,
            )
            return None
        filtered, dropped = _group_body(body)
        withheld = sorted(set(body.get("withheld_entity_ids") or []) | set(dropped))
        level = logging.WARNING if withheld or chat_type not in ("group", "forum") else logging.INFO
        log.log(
            level,
            "memory gate scope=group: session=%s chat_type=%r 放行 %d 张群可见卡,拦截 %d 个条目 entity_ids=%s",
            sid, chat_type, len(filtered["living_states"]), len(withheld), withheld,
        )
        return filtered

    def resident_block(self, session_id: str = "", timeout: float = RESIDENT_TIMEOUT) -> str:
        """常驻层注入文本(Hermes system_prompt_block 用):每个会话只取一次,结果冻结在缓存里。

        私聊/群聊判断与 recall_scoped 同一把尺(SessionScopes,认不出按群聊)。群聊只接受服务端回显 scope=group
        的响应,并在本地再过滤一遍:只留 group_ok 且通过安全校验的规则。连不上、超时、非 2xx、返回异常一律返回空串,不抛异常。
        """
        sid = session_id or self._session_id
        if sid in self._resident_cache:
            return self._resident_cache[sid]
        try:
            scope = scope_from_chat_type(self._scopes.chat_type(sid))
            status, body, _ = self._get_transport(
                f"{self._resident_url}?scope={scope}", self._headers(), timeout
            )
            if not 200 <= status < 300 or not isinstance(body, dict):
                return ""
            title = body.get("title") if isinstance(body.get("title"), str) and body.get("title") else RESIDENT_TITLE
            if scope == SCOPE_GROUP:
                if body.get("scope") != SCOPE_GROUP:
                    log.warning("resident gate CLOSED: session=%s scope=group 但服务端未回显 scope,不注入", sid)
                    return ""
                texts = []
                for rule in body.get("rules") or []:
                    text = rule.get("text") if isinstance(rule, dict) else None
                    if not isinstance(text, str) or not text or rule.get("scope") != SCOPE_GROUP_OK:
                        log.warning("resident gate: session=%s 群聊响应里有非 group_ok 规则,已丢弃", sid)
                        continue
                    if find_group_unsafe(text):
                        log.warning("resident gate: session=%s group_ok 规则 id=%s 未过本地安全校验,已丢弃", sid, rule.get("id"))
                        continue
                    texts.append(f"- {text}")
                text = "\n".join(texts)
            else:
                text = body.get("text")
                if not isinstance(text, str):
                    return ""
            block = f"{title}\n{text}" if text.strip() else ""
            while len(self._resident_cache) >= _MAX_RESIDENT_CACHE:
                self._resident_cache.pop(next(iter(self._resident_cache)))
            self._resident_cache[sid] = block
            return block
        except Exception as exc:
            log.warning("resident_block 失败,本会话不注入常驻规则: %s", exc)
            return ""

    def prefetch(self, query: str, top_k: int = 5, *, session_id: str = "", as_text: bool = False) -> Any:
        """Recall relevant memories via /v1/recall,按会话范围(dm / group)过滤。

        When as_text=True, formats the memories into a newline-delimited prompt block
        matching Hermes MemoryProvider -> str contract. When False, returns the raw List[Dict].
        """
        if not query or len(query.strip()) < 2:
            return "" if as_text else []
        try:
            body = self.recall_scoped(query, top_k, session_id=session_id)
            memories = extract_memories(body) if body is not None else []
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

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None, **_: Any,
    ) -> None:
        """Hermes MemoryProvider 契约:每轮写 Layer 0,user 行经快速门打标,assistant 行恒不打标。失败只记日志。

        messages 是 Hermes 传来的本轮消息列表(入口打了补丁才会转进来),用来兜住 memory 工具超上限写失败的内容。"""
        session = session_id or self._session_id
        if user_content:
            hint, category, noul = self._classify(user_content)
            self._log_turn(session, "user", user_content, hint, noul, category)
        if assistant_content:
            self._log_turn(session, "assistant", assistant_content, None, 0.0, None)
        if messages:
            self.store_failed_memory_writes(messages, session_id=session)

    def store_failed_memory_writes(self, messages: Any, *, session_id: str = "") -> int:
        """memory 工具因超上限拒绝写入的 add/replace,把内容改存进 cortex(explicit_addition,带 moved_from)。

        Hermes 写失败时不会触发 on_memory_write,这是唯一能拿到这类内容的路。只在私聊会话做(与镜像的群聊隔离一致);
        同一条重复失败只存一次(进程内去重 + 服务端按幂等键与内容去重)。任何异常只记日志。返回本次新提交的条数。"""
        try:
            session = session_id or self._session_id
            if self._scopes.scope(session) != SCOPE_DM:
                return 0
            saved = 0
            for target, content in failed_memory_writes(messages):
                if len(content) > MAX_CONTENT:
                    log.warning("memory 写失败兜底:内容超过 %s 字符,无法存入 cortex(%s)", MAX_CONTENT, target)
                    continue
                seen_key = (target, content)
                if seen_key in self._failed_seen:
                    continue
                while len(self._failed_seen) >= _MAX_FAILED_SEEN:
                    self._failed_seen.pop(next(iter(self._failed_seen)))
                self._failed_seen[seen_key] = True
                reason = f"moved_from={target};write_failed"
                result = self._deliver(
                    content, "fact", reason, None, WRITE_FAILED_ORIGIN, extra={"moved_from": target}
                )
                if result.get("status") in ("ok", "queued") and not result.get("deduplicated"):
                    saved += 1
                elif result.get("status") not in ("ok", "queued"):
                    log.warning("memory 写失败兜底未存入: %s", result.get("message"))
            return saved
        except Exception:
            log.exception("memory 写失败兜底出错,已忽略")
            return 0

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
        self, content: str, category: str, reason: str, supersedes_id: Optional[int], origin: Optional[str],
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = self._payload(content, category, reason, supersedes_id, origin)
        if extra:
            payload.update(extra)
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
