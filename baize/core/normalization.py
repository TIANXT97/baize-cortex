"""内容规范化与幂等键:客户端与服务端共用同一份实现,由 golden vectors 锁定。"""
import hashlib
import re
import unicodedata

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_content(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", text).strip())


def idempotency_key(session_id: str, content: str) -> str:
    payload = session_id + "\x00" + normalize_content(content)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
