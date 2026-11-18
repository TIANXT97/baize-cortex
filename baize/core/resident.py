"""常驻层(Issue #105 PR1):每个会话开头原样注入的少量硬规则,不靠关键词召回。

- 存储在 `resident_rules`,每次增/改/删都在 `resident_rule_history` 留一行旧值快照;删除是软删除(status=retired)。
- scope 只有 dm(仅私聊注入)与 group_ok(群聊也注入);群聊安全由写入校验保证:含 IP、路径、主机端口、URL、凭据样式的文本
  不能标 group_ok,来自 USER.md 的规则不能标 group_ok。
- 预算超了不报错:按 priority(hard > normal > low)再按 updated_at 新旧注入到预算为止,剩下的写进 report 并打 warning。
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("baize.resident")

SCOPE_DM = "dm"
SCOPE_GROUP_OK = "group_ok"
SCOPES = (SCOPE_DM, SCOPE_GROUP_OK)
PRIORITIES = ("hard", "normal", "low")
STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"

MAX_TEXT_CHARS = 300
MAX_META_CHARS = 64
TITLE = "【常驻规则】"
USER_MD = "user.md"

DEFAULT_DM_BUDGET = 1500
DEFAULT_GROUP_BUDGET = 600

_PRIORITY_RANK = {name: rank for rank, name in enumerate(PRIORITIES)}


class ResidentError(Exception):
    """校验或状态错误;status 直接对应 HTTP 状态码。"""

    def __init__(self, status: int, detail: str, reasons: Optional[List[str]] = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.reasons = reasons or []


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def dm_budget() -> int:
    return _env_int("CORTEX_RESIDENT_DM_BUDGET", DEFAULT_DM_BUDGET)


def group_budget() -> int:
    return _env_int("CORTEX_RESIDENT_GROUP_BUDGET", DEFAULT_GROUP_BUDGET)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 群聊安全校验(纯函数)
#
# 误伤边界:宁可误拦也不漏放,但普通中文句子不能被拦。
# - 路径:斜杠前必须不是字母/数字/中文(所以「私聊/群聊」「A/B」「dm/group_ok」不算),且路径段只认 ASCII
#   (所以「(/群聊/私聊)」也不算);绝对路径至少两段(/etc/x)或第一段是常见系统根目录(/etc);
#   带目录的文件名(dir/x.md)才算,单独的 MEMORY.md 不算。
# - IPv4:四段 0-255 的点分数字;四段点分的版本号会被拦。IPv6:完整八段或带 :: 的压缩写法;12:30:45 这类时间不算。
# - 主机端口:带点的主机名加 2-5 位端口,或单词主机加 4-5 位端口(localhost:8080);「比例 3:2」「10:30」不算。
# - 凭据:已知前缀(sk-/ghp_/gho_/ghs_/ghu_/github_pat_/ou_/oc_+十六进制)、token=/secret=/api_key= 这类赋值、
#   Bearer 串、英文单词 password/passwd(裸词也拦,宁严勿松)、「密码是/为/:」赋值,以及 24 位以上同时含字母和数字的长串。
#   中文「密码」二字本身不算,所以「不要在群里说密码」可以标 group_ok。
# - 不查裸域名(example.com):无法与文件名区分;域名只在带端口或 URL 时拦。邮箱当账号拦。
# ---------------------------------------------------------------------------

_HEX = r"[0-9A-Fa-f]"
_IPV4 = re.compile(r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\w])")
_IPV6 = re.compile(
    rf"(?<![\w:])(?:"
    rf"(?:{_HEX}{{1,4}}:){{7}}{_HEX}{{1,4}}"
    rf"|(?:{_HEX}{{1,4}}:){{1,7}}:(?:{_HEX}{{1,4}}(?::{_HEX}{{1,4}}){{0,6}})?"
    rf"|::(?:{_HEX}{{1,4}}(?::{_HEX}{{1,4}}){{0,6}})"
    rf")(?![\w:])"
)
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://\S+|(?<![\w.])www\.[A-Za-z0-9\-]+\.\S+", re.IGNORECASE)
_HOST_PORT = re.compile(
    r"(?<![\w.\-])(?:(?:[A-Za-z0-9\-]+\.)+[A-Za-z][A-Za-z0-9\-]*:\d{2,5}|[A-Za-z][A-Za-z0-9\-]*:\d{4,5})(?![\w])"
)
_PATH_SEG = r"[A-Za-z0-9_.\-]+"
_PATHS = (
    re.compile(r"(?<![\w/:.~])~/"),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"),
    re.compile(r"(?<![\w.])\.{1,2}/[\w.\-]"),
    re.compile(rf"(?<![\w/:.~])/{_PATH_SEG}(?:/{_PATH_SEG})+"),
    re.compile(r"(?<![\w/:.~])/(?:etc|home|data|usr|var|tmp|opt|root|srv|mnt|bin|proc|sys|dev)(?![\w])"),
    re.compile(rf"(?<![A-Za-z0-9_/:.~\-]){_PATH_SEG}(?:/{_PATH_SEG})*/[A-Za-z0-9_\-]+\.(?:md|db|py|json|ya?ml|sh|conf|env|txt|log|toml|ini|service)(?![\w])"),
)
_CRED_PREFIXES = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}"
    r"|(?:ou|oc)_[0-9a-fA-F]{8,}|AKIA[0-9A-Z]{12,}|xox[abprs]-[A-Za-z0-9\-]{8,})"
)
_CRED_ASSIGN = re.compile(
    r"(?:token|secret|passwd|password|api[_\-]?key|apikey|access[_\-]?key|credential)s?\s*[=:：]"
    r"|bearer\s+[A-Za-z0-9._\-]{8,}|密码\s*(?:是|为|[=:：])",
    re.IGNORECASE,
)
_CRED_WORD = re.compile(r"(?<![A-Za-z])(?:password|passwd)(?![A-Za-z])", re.IGNORECASE)
_LONG_RUN = re.compile(r"[A-Za-z0-9+/=_\-]{24,}")
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")


def _is_random_run(run: str) -> bool:
    return any(c.isdigit() for c in run) and any(c.isalpha() for c in run)


def find_group_unsafe(text: str) -> List[str]:
    """返回命中的原因代码(去重、有序);空列表表示可以标 group_ok。纯函数,不访问数据库。"""
    text = text or ""
    reasons: List[str] = []

    def hit(code: str, matched: bool) -> None:
        if matched and code not in reasons:
            reasons.append(code)

    hit("ip", bool(_IPV4.search(text) or _IPV6.search(text)))
    hit("url", bool(_URL.search(text)))
    hit("host_port", bool(_HOST_PORT.search(text)))
    hit("path", any(p.search(text) for p in _PATHS))
    hit("credential", bool(
        _CRED_PREFIXES.search(text) or _CRED_ASSIGN.search(text) or _CRED_WORD.search(text)
        or any(_is_random_run(r) for r in _LONG_RUN.findall(text))
    ))
    hit("account", bool(_EMAIL.search(text)))
    return reasons


REASON_LABELS = {
    "ip": "含 IP 地址",
    "url": "含 URL",
    "host_port": "含主机名/域名加端口",
    "path": "含文件路径",
    "credential": "含凭据或密钥样式",
    "account": "含账号(邮箱)",
}


def normalize_text(text: Any) -> str:
    """去首尾空白,换行折成单个空格(规则按「- 一行」注入,不允许自带换行伪造结构)。"""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s*[\r\n]+\s*", " ", text.strip())


def _is_user_md(moved_from: Optional[str]) -> bool:
    if not moved_from:
        return False
    return re.split(r"[\\/]", moved_from.strip())[-1].lower() == USER_MD


def validate_rule(text: str, scope: str, priority: str, moved_from: Optional[str] = None) -> List[Tuple[int, str, List[str]]]:
    """校验一条规则的最终状态(新增与修改都用同一个函数)。返回 [(http 状态, 说明, 原因代码)];空列表表示通过。纯函数。"""
    problems: List[Tuple[int, str, List[str]]] = []
    if not text:
        problems.append((422, "text 不能为空", []))
    if len(text) > MAX_TEXT_CHARS:
        problems.append((413, f"text 超过 {MAX_TEXT_CHARS} 字符(当前 {len(text)})", ["too_long"]))
    if scope not in SCOPES:
        problems.append((422, f"scope 必须是 {list(SCOPES)} 之一", []))
    if priority not in PRIORITIES:
        problems.append((422, f"priority 必须是 {list(PRIORITIES)} 之一", []))
    if scope == SCOPE_GROUP_OK:
        reasons = find_group_unsafe(text)
        if reasons:
            labels = "、".join(REASON_LABELS[r] for r in reasons)
            problems.append((422, f"不能标为 group_ok:{labels}", reasons))
        if _is_user_md(moved_from):
            problems.append((422, "来自 USER.md 的规则不能标为 group_ok", ["moved_from_user_md"]))
    return problems


# ---------------------------------------------------------------------------
# 预算裁剪(纯函数)
# ---------------------------------------------------------------------------

def _line(text: str) -> str:
    return f"- {text}"


def select_for_budget(
    rules: List[Dict[str, Any]], budget: int, group_cap: Optional[int] = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按 priority(hard>normal>low)、updated_at 新→旧、id 大→小排序,依次放进预算。

    预算按注入文本的实际长度算(每条 "- " 前缀加换行分隔)。总预算放不下就停,不跳过去塞后面更短的低优先级规则
    (避免低优先级插队);group_cap 只约束 group_ok 子集,放不下的 group_ok 之后同类也不再放,但不拦 dm 规则。
    返回 (injected, omitted),omitted 每项带 reason: budget / group_cap。
    """
    # priority 正序而 updated_at、id 倒序:分三步稳定排序
    ordered = sorted(rules, key=lambda r: r["id"], reverse=True)
    ordered = sorted(ordered, key=lambda r: r["updated_at"], reverse=True)
    ordered = sorted(ordered, key=lambda r: _PRIORITY_RANK.get(r["priority"], len(PRIORITIES)))

    injected: List[Dict[str, Any]] = []
    omitted: List[Dict[str, Any]] = []
    used = 0
    group_used = 0
    stopped = False
    group_blocked = False
    for rule in ordered:
        cost = len(_line(rule["text"])) + (1 if injected else 0)
        is_group = rule["scope"] == SCOPE_GROUP_OK
        if stopped:
            omitted.append({**rule, "reason": "budget"})
            continue
        if used + cost > budget:
            stopped = True
            omitted.append({**rule, "reason": "budget"})
            continue
        if is_group and group_cap is not None:
            group_cost = len(_line(rule["text"])) + (1 if group_used else 0)
            if group_blocked or group_used + group_cost > group_cap:
                group_blocked = True
                omitted.append({**rule, "reason": "group_cap"})
                continue
            group_used += group_cost
        used += cost
        injected.append(rule)
    return injected, omitted


def render_rules(rules: List[Dict[str, Any]]) -> str:
    return "\n".join(_line(r["text"]) for r in rules)


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------

_COLUMNS = (
    "id", "text", "scope", "priority", "source", "moved_from", "batch_id", "created_at", "updated_at", "status",
)
_FIELDS = ("text", "scope", "priority", "source", "moved_from", "batch_id")
_UNSET = object()


def _row(row: Tuple[Any, ...]) -> Dict[str, Any]:
    return dict(zip(_COLUMNS, row))


def _clean_meta(name: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ResidentError(422, f"{name} 必须是字符串")
    value = value.strip()
    if len(value) > MAX_META_CHARS:
        raise ResidentError(422, f"{name} 超过 {MAX_META_CHARS} 字符")
    return value or None


class ResidentManager:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._lock = threading.RLock()

    # -- 读 --------------------------------------------------------------
    def get(self, rule_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM resident_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        return _row(row) if row else None

    def list(self, include_retired: bool = False, scope: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = f"SELECT {', '.join(_COLUMNS)} FROM resident_rules"
        where, args = [], []
        if not include_retired:
            where.append("status = ?")
            args.append(STATUS_ACTIVE)
        if scope:
            where.append("scope = ?")
            args.append(scope)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [_row(r) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def history(self, rule_id: int) -> List[Dict[str, Any]]:
        cols = ("id", "rule_id", "action", "text", "scope", "priority", "source", "moved_from", "batch_id",
                "status", "rule_updated_at", "recorded_at", "note")
        rows = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM resident_rule_history WHERE rule_id = ? ORDER BY id", (rule_id,)
        )
        return [dict(zip(cols, r)) for r in rows]

    def render(self, scope: Optional[str]) -> Dict[str, Any]:
        """scope 认不出一律按群聊(fail-closed):群聊只含 group_ok,上限 group_budget;私聊含全部,group_ok 子集另有上限。"""
        is_dm = isinstance(scope, str) and scope.strip().lower() == "dm"
        title_cost = len(TITLE) + 1
        if is_dm:
            rules = self.list()
            budget, cap = dm_budget(), group_budget()
        else:
            rules = self.list(scope=SCOPE_GROUP_OK)
            budget, cap = group_budget(), None
        injected, omitted = select_for_budget(rules, max(budget - title_cost, 0), cap)
        text = render_rules(injected)
        total = len(TITLE) + 1 + len(text) if text else 0
        report = {
            "total_chars": total,
            "budget": budget,
            "group_budget": group_budget(),
            "eligible": len(rules),
            "injected_ids": [r["id"] for r in injected],
            "omitted_ids": [r["id"] for r in omitted],
            "omitted": [{"id": r["id"], "reason": r["reason"]} for r in omitted],
        }
        if omitted:
            log.warning(
                "resident budget exceeded scope=%s budget=%d: 省掉 %d 条 ids=%s",
                "dm" if is_dm else "group", budget, len(omitted), report["omitted_ids"],
            )
        return {
            "scope": "dm" if is_dm else "group",
            "title": TITLE,
            "text": text,
            "rules": [
                {k: r[k] for k in ("id", "text", "scope", "priority", "updated_at")} for r in injected
            ],
            "report": report,
        }

    # -- 写 --------------------------------------------------------------
    def _snapshot(self, action: str, rule_id: int, old: Optional[Dict[str, Any]], source: str, note: Optional[str]) -> None:
        old = old or {}
        self.conn.execute(
            "INSERT INTO resident_rule_history (rule_id, action, text, scope, priority, source, moved_from, batch_id,"
            " status, rule_updated_at, recorded_at, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rule_id, action, old.get("text"), old.get("scope"), old.get("priority"), old.get("source"),
             old.get("moved_from"), old.get("batch_id"), old.get("status"), old.get("updated_at"), _now(),
             f"{source}" + (f": {note}" if note else "")),
        )

    def _duplicate(self, text: str, exclude_id: Optional[int] = None) -> Optional[int]:
        row = self.conn.execute(
            "SELECT id FROM resident_rules WHERE text = ? AND status = ? AND id != ?",
            (text, STATUS_ACTIVE, exclude_id or 0),
        ).fetchone()
        return row[0] if row else None

    def _check(self, merged: Dict[str, Any]) -> None:
        problems = validate_rule(merged["text"], merged["scope"], merged["priority"], merged.get("moved_from"))
        if problems:
            reasons: List[str] = []
            for _, _, codes in problems:
                reasons.extend(c for c in codes if c not in reasons)
            raise ResidentError(problems[0][0], "; ".join(p[1] for p in problems), reasons)

    def create(
        self, text: str, scope: str = SCOPE_DM, priority: str = "normal", source: Optional[str] = None,
        moved_from: Optional[str] = None, batch_id: Optional[str] = None, dry_run: bool = False,
        actor: str = "api",
    ) -> Dict[str, Any]:
        with self._lock:
            merged = {
                "text": normalize_text(text), "scope": scope, "priority": priority,
                "source": _clean_meta("source", source), "moved_from": _clean_meta("moved_from", moved_from),
                "batch_id": _clean_meta("batch_id", batch_id),
            }
            self._check(merged)
            dup = self._duplicate(merged["text"])
            if dup is not None:
                raise ResidentError(409, f"已存在相同文本的在用规则 id={dup}")
            if dry_run:
                return {"dry_run": True, "rule": {"id": None, "status": STATUS_ACTIVE, **merged}}
            now = _now()
            try:
                cur = self.conn.execute(
                    "INSERT INTO resident_rules (text, scope, priority, source, moved_from, batch_id, created_at,"
                    " updated_at, status) VALUES (?,?,?,?,?,?,?,?,?)",
                    (merged["text"], scope, priority, merged["source"], merged["moved_from"], merged["batch_id"],
                     now, now, STATUS_ACTIVE),
                )
                rule_id = cur.lastrowid
                self._snapshot("create", rule_id, None, actor, None)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            return {"dry_run": False, "rule": self.get(rule_id)}

    def update(self, rule_id: int, dry_run: bool = False, actor: str = "api", **changes: Any) -> Dict[str, Any]:
        """changes 只放要改的字段(text/scope/priority/source/moved_from/batch_id);校验作用于合并后的最终状态。"""
        with self._lock:
            old = self.get(rule_id)
            if old is None:
                raise ResidentError(404, f"规则 {rule_id} 不存在")
            if old["status"] != STATUS_ACTIVE:
                raise ResidentError(409, f"规则 {rule_id} 已停用,不能修改")
            merged = {k: old[k] for k in _FIELDS}
            for key, value in changes.items():
                if key not in _FIELDS:
                    raise ResidentError(422, f"不支持修改字段 {key}")
                if key == "text":
                    merged[key] = normalize_text(value)
                elif key in ("source", "moved_from", "batch_id"):
                    merged[key] = _clean_meta(key, value)
                else:
                    merged[key] = value
            self._check(merged)
            dup = self._duplicate(merged["text"], exclude_id=rule_id)
            if dup is not None:
                raise ResidentError(409, f"已存在相同文本的在用规则 id={dup}")
            after = {**old, **merged}
            if dry_run:
                return {"dry_run": True, "before": old, "rule": after}
            try:
                self._snapshot("update", rule_id, old, actor, None)
                self.conn.execute(
                    "UPDATE resident_rules SET text=?, scope=?, priority=?, source=?, moved_from=?, batch_id=?,"
                    " updated_at=? WHERE id=?",
                    (merged["text"], merged["scope"], merged["priority"], merged["source"], merged["moved_from"],
                     merged["batch_id"], _now(), rule_id),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            return {"dry_run": False, "before": old, "rule": self.get(rule_id)}

    def retire(self, rule_id: int, dry_run: bool = False, actor: str = "api") -> Dict[str, Any]:
        with self._lock:
            old = self.get(rule_id)
            if old is None:
                raise ResidentError(404, f"规则 {rule_id} 不存在")
            if old["status"] == STATUS_RETIRED:
                raise ResidentError(409, f"规则 {rule_id} 已停用")
            if dry_run:
                return {"dry_run": True, "before": old, "rule": {**old, "status": STATUS_RETIRED}}
            try:
                self._snapshot("retire", rule_id, old, actor, None)
                self.conn.execute(
                    "UPDATE resident_rules SET status=?, updated_at=? WHERE id=?", (STATUS_RETIRED, _now(), rule_id)
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            return {"dry_run": False, "before": old, "rule": self.get(rule_id)}
