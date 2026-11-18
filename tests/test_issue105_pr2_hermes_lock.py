"""Issue #105 PR2:跟 Hermes 抢同一把锁——Hermes 的 add 与外迁多进程反复交替,两边都不丢、不重复

优先直接用 Hermes 自带 venv 跑它的 tools/memory_tool_store.py(只写临时主目录);装不了就用按它的锁协议写的模拟写入方。
HERMES_AGENT_DIR 可改 Hermes 安装目录(默认 /usr/local/lib/hermes-agent)。
"""
import fcntl
import json
import os
import random
import subprocess
import tempfile
import threading
import time

from baize.core import memory_migrator as mm
from tests._mm_helpers import FakeLLM, know_entry, workspace

AGENT_DIR = os.environ.get("HERMES_AGENT_DIR", "/usr/local/lib/hermes-agent")
HERMES_PY = os.path.join(AGENT_DIR, "venv", "bin", "python")
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_hermes_store_worker.py")
LIMIT_MEMORY, LIMIT_USER = 2200, 1375
WRITERS, ADDS = 4, 40


def hermes_available():
    return os.path.exists(os.path.join(AGENT_DIR, "tools", "memory_tool_store.py")) and os.path.exists(HERMES_PY)


def _start_hermes_writers(home):
    env = {**os.environ, "HERMES_HOME": home, "PYTHONDONTWRITEBYTECODE": "1"}
    return [
        subprocess.Popen(
            [HERMES_PY, WORKER, AGENT_DIR, "add", str(LIMIT_MEMORY), str(LIMIT_USER), str(w), str(ADDS)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
        )
        for w in range(WRITERS)
    ]


class SimulatedWriters:
    """按 Hermes 的锁协议写的模拟写入方:<文件>.lock 上 flock(LOCK_EX),锁内重读,超上限拒绝,临时文件 + os.replace。"""

    def __init__(self, home):
        self.home = home
        self.results = []
        self.threads = [threading.Thread(target=self._run, args=(w,)) for w in range(WRITERS)]
        for t in self.threads:
            t.start()

    def _add(self, text):
        path = os.path.join(self.home, "memories", "MEMORY.md")
        fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = open(path, encoding="utf-8-sig").read() if os.path.exists(path) else ""
            entries = list(dict.fromkeys(e for e in (x.strip() for x in raw.split("\n§\n")) if e))
            if text in entries:
                return True
            if len("\n§\n".join(entries + [text])) > LIMIT_MEMORY:
                return False
            tmp = path + f".tmp{threading.get_ident()}"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n§\n".join(entries + [text]))
            os.replace(tmp, path)
            return True
        finally:
            os.close(fd)

    def _run(self, worker):
        rng = random.Random(worker)
        for index in range(ADDS):
            kind = "规则" if index % 2 == 0 else "知识"
            text = f"{kind}H{worker}-{index}:并发写入的条目,编号{worker}-{index}"
            self.results.append({"text": text, "success": self._add(text)})
            time.sleep(rng.random() * 0.02)

    def alive(self):
        return any(t.is_alive() for t in self.threads)


def _db_texts(ws):
    facts = [r[0] for r in ws.conn.execute(
        "SELECT raw_text FROM scratchpad_log WHERE session_id LIKE 'memory_migration:%' AND expired_at IS NULL")]
    rules = [r[0] for r in ws.conn.execute("SELECT text FROM resident_rules WHERE status = 'active'")]
    return facts, rules


def _moved_texts(ws):
    moved = []
    for (report_json,) in ws.conn.execute("SELECT report_json FROM memory_migration_runs"):
        moved += [i["text"] for i in json.loads(report_json).get("items", []) if i.get("status") == "moved"]
    return moved


def _undone_count(ws):
    total = 0
    for (report_json,) in ws.conn.execute("SELECT report_json FROM memory_migration_runs"):
        total += sum(1 for i in json.loads(report_json).get("items", []) if i.get("status") == "undone")
    return total


def run_race(real_hermes):
    initial = [know_entry(i, 230) for i in range(1, 6)]
    with workspace(memory=initial) as ws:
        os.environ["CORTEX_MEMORY_MIGRATION"] = "apply"
        migrator = ws.migrator(FakeLLM())
        home = ws.home
        if real_hermes:
            procs = _start_hermes_writers(home)
            busy = lambda: any(p.poll() is None for p in procs)  # noqa: E731
        else:
            sim = SimulatedWriters(home)
            busy = sim.alive
        runs = []
        while busy():
            runs.append(migrator.run_file("MEMORY.md", "race"))
            time.sleep(0.005)
        runs.append(migrator.run_file("MEMORY.md", "race"))
        if real_hermes:
            results = []
            for proc in procs:
                out, err = proc.communicate(timeout=60)
                assert proc.returncode == 0, err[-500:]
                results += [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        else:
            results = sim.results
        assert len(results) == WRITERS * ADDS
        hermes_ok = [r["text"] for r in results if r["success"]]
        assert len(hermes_ok) >= WRITERS * ADDS // 2, "外迁腾出了空间,大部分写入应成功"
        file_entries = ws.entries("MEMORY.md")
        facts, rules = _db_texts(ws)
        db_texts = facts + rules
        moved = _moved_texts(ws)
        done = [r for r in runs if r["status"] == "done"]
        assert len(done) >= 2, f"外迁至少跑成功两次,实际 {[r['status'] for r in runs]}"
        assert len(file_entries) == len(set(file_entries)), "文件里没有重复条目"
        assert len(db_texts) == len(set(db_texts)), "库里没有重复"
        assert sorted(moved) == sorted(db_texts), "迁走的每条都在库里,库里也只有迁走的"
        assert not set(file_entries) & set(db_texts), "迁走的不在文件里、文件里的没被迁走"
        assert _undone_count(ws) == 0, "没有因冲突撤销的条目"
        expected = set(initial) | set(hermes_ok)
        assert expected == set(file_entries) | set(db_texts), \
            f"Hermes 写入的每条都在(文件或库);丢了 {sorted(expected - set(file_entries) - set(db_texts))}"
        assert mm.count_chars(ws.read("MEMORY.md")) <= LIMIT_MEMORY
        assert not [n for n in os.listdir(os.path.dirname(ws.path("MEMORY.md"))) if n.startswith(".mem_cortex_")]
        return ws, len(done), len(moved)


def test_race_with_real_hermes_store_or_simulated_writer():
    modes = [False] + ([True] if hermes_available() else [])
    for real in modes:
        run_race(real)
    if not hermes_available():
        print("  (未找到 Hermes 安装,只跑了按锁协议写的模拟写入方)")


def test_cortex_written_file_does_not_trigger_hermes_drift():
    if not hermes_available():
        print("  (未找到 Hermes 安装,跳过 _detect_external_drift 验证)")
        return
    entries = [know_entry(i, 240) for i in range(1, 8)]
    with workspace(memory=entries) as ws:
        os.environ["CORTEX_MEMORY_MIGRATION"] = "apply"
        report = ws.migrator(FakeLLM()).run_file("MEMORY.md", "drift")
        assert report["status"] == "done" and ws.read("MEMORY.md") != "\n§\n".join(entries)
        env = {**os.environ, "HERMES_HOME": ws.home, "PYTHONDONTWRITEBYTECODE": "1"}
        out = subprocess.run(
            [HERMES_PY, WORKER, AGENT_DIR, "drift", str(LIMIT_MEMORY), str(LIMIT_USER)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert out.returncode == 0, out.stderr[-500:]
        result = json.loads(out.stdout.strip().splitlines()[-1])
        assert result["drift"] is None, "cortex 写回的文件不能触发 Hermes 的外部漂移保护"
        assert result["add_ok"] is True, "写回之后 Hermes 照常 add"
        baks = [n for n in os.listdir(os.path.dirname(ws.path("MEMORY.md"))) if ".bak." in n]
        assert baks == [], "Hermes 没有因漂移落备份"


def test_hermes_reads_cortex_rewritten_file_back_identically():
    if not hermes_available():
        return
    entries = [know_entry(i, 240) for i in range(1, 8)]
    with workspace(memory=entries) as ws:
        os.environ["CORTEX_MEMORY_MIGRATION"] = "apply"
        ws.migrator(FakeLLM()).run_file("MEMORY.md", "readback")
        env = {**os.environ, "HERMES_HOME": ws.home, "PYTHONDONTWRITEBYTECODE": "1"}
        script = (
            "import sys,json;sys.path.insert(0,sys.argv[1]);"
            "from tools.memory_tool_store import MemoryStore;s=MemoryStore(2200,1375);s.load_from_disk();"
            "print(json.dumps(s.memory_entries,ensure_ascii=False))"
        )
        out = subprocess.run([HERMES_PY, "-c", script, AGENT_DIR], capture_output=True, text=True, env=env, timeout=60)
        assert json.loads(out.stdout.strip().splitlines()[-1]) == ws.entries("MEMORY.md")


def test_failed_write_markers_match_real_hermes_results():
    """写失败兜底认的报错文案,直接拿 Hermes 真实 store 的失败结果核对。"""
    if not hermes_available():
        print("  (未找到 Hermes 安装,跳过真实报错文案核对)")
        return
    from baize.client.plugin import failed_memory_writes
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "HERMES_HOME": home, "PYTHONDONTWRITEBYTECODE": "1"}
        out = subprocess.run(
            [HERMES_PY, WORKER, AGENT_DIR, "fail", str(LIMIT_MEMORY), str(LIMIT_USER)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert out.returncode == 0, out.stderr[-500:]
        outcomes = json.loads(out.stdout.strip().splitlines()[-1])
    assert outcomes["seed"]["success"] is True
    for name in ("add", "replace", "batch", "patch"):
        assert outcomes[name]["success"] is False, name
    args = {
        "add": {"action": "add", "target": "memory", "content": "乙" * 80},
        "replace": {"action": "replace", "target": "memory", "old_text": "甲" * 40, "content": "丙" * 150},
        "batch": {"target": "memory", "operations": [{"action": "add", "content": "丁" * 90}]},
        "patch": {"action": "patch", "target": "memory", "old_text": "甲", "new_text": "戊" * 150},
    }
    expected = {"add": "乙" * 80, "replace": "丙" * 150, "batch": "丁" * 90, "patch": None}
    for name, payload in args.items():
        messages = [
            {"role": "user", "content": "记一下"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "memory", "arguments": json.dumps(payload)}}]},
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps(outcomes[name], ensure_ascii=False)},
        ]
        found = failed_memory_writes(messages)
        assert found == ([("MEMORY.md", expected[name])] if expected[name] else []), (name, outcomes[name]["error"], found)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"✅ Issue #105 PR2 抢锁并发测试全部通过({len(tests)} 项,Hermes 真实 store: {hermes_available()})")
