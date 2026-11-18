"""并发测试用的 Hermes 写入方:直接 import Hermes 自带的 MemoryStore,在临时主目录里反复 add。

用法(用 Hermes 自己的 venv 解释器):HERMES_HOME=<临时目录> python _hermes_store_worker.py <hermes安装目录> <add|drift> ...
不 import baize,不启动任何 Hermes 服务;只写 HERMES_HOME 指向的临时目录。
"""
import json
import random
import sys
import time

agent_dir, command = sys.argv[1], sys.argv[2]
sys.path.insert(0, agent_dir)
from tools.memory_tool_store import MemoryStore  # noqa: E402

store = MemoryStore(int(sys.argv[3]), int(sys.argv[4]))
store.load_from_disk()

if command == "add":
    worker_id, count = sys.argv[5], int(sys.argv[6])
    rng = random.Random(worker_id)
    for index in range(count):
        kind = "规则" if index % 2 == 0 else "知识"
        text = f"{kind}H{worker_id}-{index}:并发写入的条目,编号{worker_id}-{index}"
        result = store.add("memory", text)
        print(json.dumps({"text": text, "success": result.get("success") is True}, ensure_ascii=False), flush=True)
        time.sleep(rng.random() * 0.02)
elif command == "fail":
    # 小上限下制造真实的超限失败,把 Hermes 原样的返回结果打印出来(核对写失败兜底认的报错文案)
    store.memory_char_limit = 100
    seed = store.add("memory", "甲" * 40)
    outcomes = {
        "seed": seed,
        "add": store.add("memory", "乙" * 80),
        "replace": store.replace("memory", "甲" * 40, "丙" * 150),
        "batch": store.apply_batch("memory", [{"action": "add", "content": "丁" * 90}]),
        "patch": store.patch("memory", "甲", "戊" * 150),
    }
    print(json.dumps(outcomes, ensure_ascii=False))
elif command == "drift":
    raw = open(store._path_for("memory"), encoding="utf-8").read()
    drift = store._detect_external_drift("memory", raw)
    probe = store.add("memory", "漂移检查之后的写入")
    print(json.dumps({"drift": drift, "add_ok": probe.get("success") is True}, ensure_ascii=False))
