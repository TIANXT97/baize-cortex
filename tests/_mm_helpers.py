"""Issue #105 PR2 测试共用:临时 Hermes 主目录、库、假模型、哈希与行数快照。条目文本全是现编的短句。"""
import hashlib
import os
import sqlite3
import tempfile
from contextlib import contextmanager

from baize.core.memory_migrator import ENTRY_DELIMITER, MemoryMigrator
from baize.storage.db import init_db

ENV_NAMES = (
    "CORTEX_MEMORY_MIGRATION", "CORTEX_HERMES_HOME", "CORTEX_RESIDENT_DM_BUDGET", "CORTEX_RESIDENT_GROUP_BUDGET",
    "CORTEX_AGENT_TOKENS", "CORTEX_FACT_ROUTING",
)

RULE_WORDS = "务必先确认范围再动手"
KNOW_WORDS = "该项目的目录结构和端口都记录在配置表里"


def pad(head: str, size: int) -> str:
    """生成恰好 size 个字符、以 head 开头的条目。"""
    filler = "补充说明内容" * (size // 6 + 2)
    return (head + filler)[:size]


def rule_entry(index: int, size: int = 120) -> str:
    return pad(f"规则{index}:{RULE_WORDS}。", size)


def know_entry(index: int, size: int = 240) -> str:
    return pad(f"知识{index}:{KNOW_WORDS}。", size)


class FakeLLM:
    """按条目开头判定:以「规则」开头的是 rule,其余是 knowledge;fail_on 里出现的子串报错,garble_on 里的返回乱码。"""

    def __init__(self, fail_on=(), garble_on=(), delay=None, force_kind=None):
        self.calls = []
        self.fail_on = tuple(fail_on)
        self.garble_on = tuple(garble_on)
        self.delay = delay
        self.force_kind = force_kind

    def chat(self, system, user):
        self.calls.append(user)
        if self.delay is not None:
            self.delay()
        if any(mark in user for mark in self.fail_on):
            raise TimeoutError("fake timeout")
        if any(mark in user for mark in self.garble_on):
            return "我觉得这条很重要"
        body = user.split("待分类条目:", 1)[-1]
        kind = self.force_kind or ("rule" if body.startswith("规则") else "knowledge")
        return '{"kind": "%s"}' % kind


@contextmanager
def workspace(memory=None, user=None, config="memory:\n  memory_char_limit: 2200\n  user_char_limit: 1375\n"):
    """临时 Hermes 主目录(memories/ 下有 MEMORY.md、USER.md,config.yaml)+ 库。返回对象带路径与迁移器工厂。"""
    saved = {name: os.environ.pop(name, None) for name in ENV_NAMES}
    with tempfile.TemporaryDirectory() as root:
        home = os.path.join(root, "hermes")
        os.makedirs(os.path.join(home, "memories"), mode=0o700)
        if config is not None:
            with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as fh:
                fh.write(config)
        ws = Workspace(root, home)
        if memory is not None:
            ws.write("MEMORY.md", memory)
        if user is not None:
            ws.write("USER.md", user)
        ws.conn = init_db(ws.db_path)
        os.environ["CORTEX_HERMES_HOME"] = home
        try:
            yield ws
        finally:
            ws.conn.close()
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


class Workspace:
    def __init__(self, root, home):
        self.root = root
        self.home = home
        self.db_path = os.path.join(root, "c.db")
        self.conn = None

    def path(self, name):
        return os.path.join(self.home, "memories", name)

    def write(self, name, entries):
        text = entries if isinstance(entries, str) else ENTRY_DELIMITER.join(entries)
        path = self.path(name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o600)

    def read(self, name):
        with open(self.path(name), encoding="utf-8-sig") as fh:
            return fh.read()

    def entries(self, name):
        return [e for e in (x.strip() for x in self.read(name).split(ENTRY_DELIMITER)) if e]

    def digest(self, name):
        with open(self.path(name), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def migrator(self, llm=None, **kwargs):
        return MemoryMigrator(self.db_path, llm, **kwargs)

    def table_counts(self, skip=("memory_migration_runs",)):
        names = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        return {n: self.conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names if n not in skip}

    def table_digest(self, skip=("memory_migration_runs",)):
        """所有表(除 skip)的全部行内容哈希,比行数更严。"""
        sha = hashlib.sha256()
        names = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        for name in names:
            if name in skip:
                continue
            for row in self.conn.execute(f"SELECT * FROM {name} ORDER BY 1"):
                sha.update(repr(row).encode("utf-8"))
        return sha.hexdigest()

    def reopen(self):
        conn = sqlite3.connect(self.db_path)
        return conn

    def add_card(self, entity_id="proj:user_profile", visibility="private", status="ACTIVE"):
        import json
        meta = json.dumps({"visibility": visibility})
        self.conn.execute(
            "INSERT INTO living_states (entity_id, display_name, version, status, current_decision, metadata_json) "
            "VALUES (?,?,?,?,?,?)", (entity_id, "用户档案", 1, status, "占位", meta),
        )
        self.conn.commit()
