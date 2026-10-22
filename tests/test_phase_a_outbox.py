"""Phase A:Outbox 可靠投递(重试不丢、并发 rotate、4xx 分类、崩溃重放、后台线程不外抛)"""
import glob
import json
import os
import tempfile
import threading
import time

from baize.client.outbox import Outbox


def _outbox(d, sender=None, now=None):
    now = now if now is not None else [1000.0]
    return Outbox(os.path.join(d, "outbox.jsonl"), sender=sender, clock=lambda: now[0], rand=lambda: 1.0), now


def _read(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_network_failure_retries_without_loss():
    with tempfile.TemporaryDirectory() as d:
        def failing(env):
            raise ConnectionError("down")

        ob, now = _outbox(d, failing)
        ob.enqueue({"n": 1}, "k1")
        assert ob.flush()["requeued"] == 1 and ob.pending() == 1
        assert not glob.glob(os.path.join(d, "outbox.sending.*"))

        delivered = []
        ob.sender = lambda env: (delivered.append(env["idempotency_key"]) or (200, None))
        ob.flush()
        assert delivered == [] and ob.pending() == 1
        now[0] += 10
        ob.flush()
        assert delivered == ["k1"] and ob.pending() == 0


def test_concurrent_append_and_rotate_loses_no_line():
    with tempfile.TemporaryDirectory() as d:
        delivered = []
        lock = threading.Lock()

        def sender(env):
            with lock:
                delivered.append(env["idempotency_key"])
            return 200, None

        ob, _ = _outbox(d, sender)
        total, writers = 200, 4
        stop = threading.Event()

        def write(offset):
            for i in range(total // writers):
                ob.enqueue({"i": offset + i}, f"k{offset + i}")

        def flusher():
            while not stop.is_set():
                ob.flush()

        threads = [threading.Thread(target=write, args=(w * (total // writers),)) for w in range(writers)]
        fl = threading.Thread(target=flusher)
        fl.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop.set()
        fl.join()
        while ob.pending() or glob.glob(os.path.join(d, "outbox.sending.*")):
            ob.flush()
        assert sorted(delivered) == sorted(f"k{i}" for i in range(total))


def test_400_goes_to_dead_letter():
    with tempfile.TemporaryDirectory() as d:
        ob, _ = _outbox(d, lambda env: (400, None))
        ob.enqueue({"n": 1}, "bad")
        stats = ob.flush()
        assert stats["dead"] == 1 and ob.dead_count == 1 and ob.pending() == 0
        assert len(_read(os.path.join(d, "outbox.dead.jsonl"))) == 1


def test_401_is_kept_and_pauses_delivery():
    with tempfile.TemporaryDirectory() as d:
        calls = []

        def unauthorized(env):
            calls.append(env["idempotency_key"])
            return 401, None

        ob, now = _outbox(d, unauthorized)
        ob.enqueue({"n": 1}, "k1")
        ob.enqueue({"n": 2}, "k2")
        stats = ob.flush()
        assert calls == ["k1"] and stats["dead"] == 0 and ob.pending() == 2

        delivered = []
        ob.sender = lambda env: (delivered.append(env["idempotency_key"]) or (200, None))
        now[0] += 10
        ob.flush()
        assert sorted(delivered) == ["k1", "k2"] and ob.pending() == 0


def test_429_honours_retry_after():
    with tempfile.TemporaryDirectory() as d:
        ob, now = _outbox(d, lambda env: (429, 30.0))
        ob.enqueue({"n": 1}, "k1")
        ob.flush()
        delivered = []
        ob.sender = lambda env: (delivered.append(1) or (200, None))
        now[0] = 1020.0
        ob.flush()
        assert delivered == [] and ob.pending() == 1
        now[0] = 1031.0
        ob.flush()
        assert delivered == [1] and ob.pending() == 0


def test_5xx_backs_off_and_keeps_message():
    with tempfile.TemporaryDirectory() as d:
        ob, now = _outbox(d, lambda env: (503, None))
        ob.enqueue({"n": 1}, "k1")
        ob.flush()
        env = _read(ob.path)[0]
        assert env["attempts"] == 1 and env["next_retry_at"] > now[0]


def test_crash_leftover_sending_file_is_replayed():
    with tempfile.TemporaryDirectory() as d:
        delivered = []
        ob, _ = _outbox(d, lambda env: (delivered.append(env["idempotency_key"]) or (200, None)))
        leftover = os.path.join(d, "outbox.sending.123")
        with open(leftover, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"idempotency_key": "crashed", "payload": {}, "attempts": 0, "next_retry_at": 0}) + "\n")
        ob.flush()
        assert delivered == ["crashed"] and not os.path.exists(leftover)


def test_corrupt_line_goes_to_dead_letter():
    with tempfile.TemporaryDirectory() as d:
        ob, _ = _outbox(d, lambda env: (200, None))
        with open(ob.path, "w", encoding="utf-8") as fh:
            fh.write("{not json\n")
        assert ob.flush()["dead"] == 1


def test_background_thread_swallows_exceptions():
    with tempfile.TemporaryDirectory() as d:
        ob, _ = _outbox(d, lambda env: (200, None))

        def boom():
            raise RuntimeError("内部异常")

        ob.flush = boom
        ob.start(interval=0.01)
        try:
            time.sleep(0.15)
            assert ob._worker.is_alive()
            assert ob._worker.daemon
        finally:
            ob.stop()


def test_file_permission_is_owner_only():
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as d:
        ob, _ = _outbox(d)
        ob.enqueue({"n": 1}, "k1")
        assert os.stat(ob.path).st_mode & 0o777 == 0o600


if __name__ == "__main__":
    test_network_failure_retries_without_loss()
    test_concurrent_append_and_rotate_loses_no_line()
    test_400_goes_to_dead_letter()
    test_401_is_kept_and_pauses_delivery()
    test_429_honours_retry_after()
    test_5xx_backs_off_and_keeps_message()
    test_crash_leftover_sending_file_is_replayed()
    test_corrupt_line_goes_to_dead_letter()
    test_background_thread_swallows_exceptions()
    test_file_permission_is_owner_only()
    print("✅ Phase A Outbox 测试全部通过")
