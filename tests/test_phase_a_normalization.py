"""Phase A:规范化与幂等键 golden vectors 契约测试(客户端与服务端共用同一实现)"""
import json
from pathlib import Path

from baize.core.normalization import idempotency_key, normalize_content

VECTORS = Path(__file__).with_name("fixtures") / "normalization_vectors.json"


def _load():
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def test_golden_vectors_match():
    for v in _load():
        assert normalize_content(v["content"]) == v["normalized"], v
        assert idempotency_key(v["session_id"], v["content"]) == v["key"], v


def test_equivalent_forms_share_key_and_sessions_differ():
    assert idempotency_key("s1", "hello world") == idempotency_key("s1", "  hello   world \n")
    assert idempotency_key("s1", "hello world") != idempotency_key("s2", "hello world")


def test_key_is_128_bit_hex():
    key = idempotency_key("s", "x")
    assert len(key) == 32
    int(key, 16)


if __name__ == "__main__":
    test_golden_vectors_match()
    test_equivalent_forms_share_key_and_sessions_differ()
    test_key_is_128_bit_hex()
    print("✅ Phase A 规范化契约测试全部通过")
