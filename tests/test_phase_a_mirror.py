"""Phase A:on_memory_write 本地便签镜像钩子"""
import json
import os
import tempfile
import time

from baize.client.outbox import Outbox
from baize.client.plugin import MIRROR_ORIGIN, CortexClient


def _client(d, enqueue_error=False):
    def never(url, body, headers, timeout):
        raise AssertionError("镜像钩子不应同步访问网络")

    ob = Outbox(os.path.join(d, "outbox.jsonl"))
    if enqueue_error:
        def boom(payload, key):
            raise OSError("磁盘已满")

        ob.enqueue = boom
    return CortexClient("http://x", None, "sess", ob, transport=never), ob


def test_successful_write_enqueues_single_entry():
    with tempfile.TemporaryDirectory() as d:
        cc, ob = _client(d)
        cc.on_memory_write("add", "MEMORY.md", "  新的  条目 ", {"source": "hermes"})
        cc.on_memory_write("replace", "USER.md", "用户偏好", None)
        assert ob.pending() == 2
        with open(ob.path, encoding="utf-8") as fh:
            envs = [json.loads(line) for line in fh]
        assert envs[0]["payload"]["content"] == "新的 条目"
        assert envs[0]["payload"]["origin"] == MIRROR_ORIGIN
        assert envs[0]["payload"]["reason"].startswith(MIRROR_ORIGIN)
        assert envs[0]["idempotency_key"] == envs[0]["payload"]["idempotency_key"]


def test_writeback_origin_is_skipped():
    with tempfile.TemporaryDirectory() as d:
        cc, ob = _client(d)
        cc.on_memory_write("add", "MEMORY.md", "回写内容", {"origin": "writeback"})
        assert ob.pending() == 0


def test_other_targets_and_empty_content_are_ignored():
    with tempfile.TemporaryDirectory() as d:
        cc, ob = _client(d)
        cc.on_memory_write("add", "NOTES.md", "x", None)
        cc.on_memory_write("remove", "MEMORY.md", "x", None)
        cc.on_memory_write("add", "MEMORY.md", "   ", None)
        assert ob.pending() == 0


def test_hook_never_blocks_or_raises():
    with tempfile.TemporaryDirectory() as d:
        cc, _ = _client(d, enqueue_error=True)
        start = time.monotonic()
        cc.on_memory_write("add", "MEMORY.md", "内容", None)
        assert time.monotonic() - start < 0.5


if __name__ == "__main__":
    test_successful_write_enqueues_single_entry()
    test_writeback_origin_is_skipped()
    test_other_targets_and_empty_content_are_ignored()
    test_hook_never_blocks_or_raises()
    print("✅ Phase A 镜像钩子测试全部通过")
