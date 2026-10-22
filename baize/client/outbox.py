"""可靠投递 Outbox:追加写 jsonl,rotate 后逐行投递,崩溃后可重放。"""
import glob
import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("baize.outbox")

Sender = Callable[[Dict[str, Any]], Tuple[int, Optional[float]]]

_DEAD_STATUSES = {400, 413, 422}
_RETRY_4XX = {401, 403, 408, 429}


class _FileLock:
    def __init__(self, path: str):
        self.path = path
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    os.lseek(self._fd, 0, 0)
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    time.sleep(0.01)
        else:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_EX)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, 0)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


class Outbox:
    def __init__(
        self,
        path: Optional[str] = None,
        sender: Optional[Sender] = None,
        clock: Callable[[], float] = time.time,
        rand: Callable[[], float] = random.random,
        base_delay: float = 1.0,
        max_delay: float = 300.0,
    ):
        self.path = path or os.path.join(os.path.expanduser("~"), ".hermes", "outbox.jsonl")
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.dead_path = os.path.join(os.path.dirname(os.path.abspath(self.path)), "outbox.dead.jsonl")
        self._sending_prefix = self.path[: -len(".jsonl")] + ".sending." if self.path.endswith(".jsonl") else self.path + ".sending."
        self.sender = sender
        self.clock = clock
        self.rand = rand
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.dead_count = 0
        self._thread_lock = threading.Lock()
        self._file_lock = _FileLock(self.path + ".lock")
        self._flush_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def enqueue(self, payload: Dict[str, Any], idempotency_key: str) -> None:
        envelope = {
            "idempotency_key": idempotency_key,
            "payload": payload,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "attempts": 0,
            "next_retry_at": 0,
        }
        with self._guard():
            self._append_lines(self.path, [envelope])

    def pending(self) -> int:
        with self._guard():
            return self._count(self.path)

    def flush(self) -> Dict[str, int]:
        stats = {"acked": 0, "dead": 0, "requeued": 0}
        if self.sender is None or not self._flush_lock.acquire(blocking=False):
            return stats
        try:
            for sending in self._claim():
                self._process(sending, stats)
        finally:
            self._flush_lock.release()
        return stats

    def start(self, interval: float = 5.0) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, args=(interval,), name="cortex-outbox", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=5)

    def _run(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self.flush()
            except BaseException:
                log.exception("outbox flush 异常,已忽略")

    class _Guard:
        def __init__(self, outer: "Outbox"):
            self.outer = outer

        def __enter__(self):
            self.outer._thread_lock.acquire()
            try:
                self.outer._file_lock.acquire()
            except BaseException:
                self.outer._thread_lock.release()
                raise

        def __exit__(self, *exc):
            try:
                self.outer._file_lock.release()
            finally:
                self.outer._thread_lock.release()

    def _guard(self) -> "Outbox._Guard":
        return Outbox._Guard(self)

    @staticmethod
    def _append_lines(path: str, items: List[Any]) -> None:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        for item in items:
            line = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            data = (line.rstrip("\n") + "\n").encode("utf-8")
            fd = os.open(path, flags, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)

    @staticmethod
    def _count(path: str) -> int:
        if not os.path.exists(path):
            return 0
        with open(path, "rb") as fh:
            return sum(1 for line in fh if line.strip())

    def _claim(self) -> List[str]:
        with self._guard():
            if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
                os.replace(self.path, f"{self._sending_prefix}{time.time_ns()}")
            return sorted(glob.glob(self._sending_prefix + "*"))

    def _backoff(self, attempts: int) -> float:
        delay = min(self.max_delay, self.base_delay * (2 ** attempts))
        return delay * (0.5 + 0.5 * self.rand())

    def _dead_letter(self, raw: str, status: Optional[int], stats: Dict[str, int]) -> None:
        with self._guard():
            self._append_lines(self.dead_path, [{"status": status, "envelope": raw.strip()}])
        self.dead_count += 1
        stats["dead"] += 1
        log.warning("outbox 死信 status=%s", status)

    def _process(self, sending: str, stats: Dict[str, int]) -> None:
        with open(sending, "r", encoding="utf-8", errors="replace") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        requeue: List[Dict[str, Any]] = []
        paused = False
        for raw in lines:
            try:
                env = json.loads(raw)
                if not isinstance(env, dict) or "payload" not in env:
                    raise ValueError("非法信封")
            except ValueError:
                self._dead_letter(raw, None, stats)
                continue
            now = self.clock()
            if paused or env.get("next_retry_at", 0) > now:
                requeue.append(env)
                continue
            try:
                status, retry_after = self.sender(env)
            except Exception:
                status, retry_after = None, None

            if status is not None and 200 <= status < 300:
                stats["acked"] += 1
                continue
            if status is not None and (status in _DEAD_STATUSES or (400 <= status < 500 and status not in _RETRY_4XX)):
                self._dead_letter(raw, status, stats)
                continue

            attempts = int(env.get("attempts", 0))
            delay = retry_after if (status == 429 and retry_after) else self._backoff(attempts)
            env["attempts"] = attempts + 1
            env["next_retry_at"] = now + delay
            if status in (401, 403):
                paused = True
            requeue.append(env)

        if requeue:
            with self._guard():
                self._append_lines(self.path, requeue)
            stats["requeued"] += len(requeue)
        os.remove(sending)
