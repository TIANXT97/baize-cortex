"""错误签名归一化与同签名连续失败熔断。"""
import re
import threading
from typing import Any, Callable, Dict, Optional, Tuple

GUIDANCE = (
    "MEMORY.md 空间不足，本轮禁止继续重试 memory 工具。"
    "请改用 cortex_add 工具将内容沉淀至白泽 Cortex 长期库。"
)

_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ISO_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_EPOCH = re.compile(r"\b\d{10}(?:\d{3})?\b")
_WIN_PATH = re.compile(r"[A-Za-z]:[\\/][^\s'\"]*")
_UNIX_PATH = re.compile(r"(?<![\w/<])~?/[^\s'\"]+")
_HEX = re.compile(r"\b(?=\d*[a-fA-F])[0-9a-fA-F]{8,}\b")
_NUM = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")


def normalize_error(message: str) -> str:
    text = _UUID.sub("<UUID>", message)
    text = _ISO_TS.sub("<TS>", text)
    text = _EPOCH.sub("<TS>", text)
    text = _WIN_PATH.sub("<PATH>", text)
    text = _UNIX_PATH.sub("<PATH>", text)
    text = _HEX.sub("<HEX>", text)
    text = _NUM.sub("<N>", text)
    return _SPACE.sub(" ", text).strip()


def error_signature(tool_name: str, error_class: str, message: str) -> str:
    return f"{tool_name}|{error_class}|{normalize_error(message)}"


def extract_error(result: Any) -> Optional[Tuple[str, str]]:
    if isinstance(result, dict) and (result.get("error") or result.get("success") is False):
        return "ToolError", str(result.get("error") or result.get("message") or "")
    return None


class ToolCircuitBreaker:
    def __init__(self, threshold: int = 2, guidance: str = GUIDANCE):
        self.threshold = threshold
        self.guidance = guidance
        self._lock = threading.Lock()
        self._streak: Dict[Tuple[str, str], Tuple[str, int]] = {}
        self._tripped: Dict[Tuple[str, str], str] = {}

    def before_call(self, session_id: str, tool_name: str) -> Optional[str]:
        with self._lock:
            return self.guidance if (session_id, tool_name) in self._tripped else None

    def record_failure(self, session_id: str, tool_name: str, error_class: str, message: str) -> str:
        signature = error_signature(tool_name, error_class, message)
        key = (session_id, tool_name)
        with self._lock:
            last, count = self._streak.get(key, ("", 0))
            count = count + 1 if last == signature else 1
            self._streak[key] = (signature, count)
            if count >= self.threshold:
                self._tripped[key] = signature
        return signature

    def record_success(self, session_id: str, tool_name: str) -> None:
        key = (session_id, tool_name)
        with self._lock:
            self._streak.pop(key, None)
            self._tripped.pop(key, None)

    def new_turn(self, session_id: str) -> None:
        with self._lock:
            for table in (self._streak, self._tripped):
                for key in [k for k in table if k[0] == session_id]:
                    del table[key]

    def call(self, session_id: str, tool_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        guidance = self.before_call(session_id, tool_name)
        if guidance is not None:
            return {"success": False, "error": guidance, "circuit_open": True}
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            self.record_failure(session_id, tool_name, type(exc).__name__, str(exc))
            raise
        failure = extract_error(result)
        if failure is None:
            self.record_success(session_id, tool_name)
        else:
            self.record_failure(session_id, tool_name, *failure)
            if self.before_call(session_id, tool_name) is not None:
                return {"success": False, "error": self.guidance, "circuit_open": True}
        return result
