"""Layer 3: 慢波合成睡眠引擎 (Dream Consolidation)

两段式执行:只读分析 -> 先归档落盘 -> 单一短事务回写卡片与图谱并清退已折叠草稿。
Issue #44 PR4:折叠前先跑批量判研(总开关 CORTEX_BATCH_JUDGE,默认 off);
candidate 候选经 System 2 二次核实后并入 facts,严禁参与定案改写(红线 1)。
"""
import argparse
import gzip
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import nullcontext
from typing import Any, Dict, List, Optional, Tuple

from baize.core.living_state import LivingStateManager
from baize.core.scratchpad import (
    CANDIDATE_DECISION, CANDIDATE_FACT, CANDIDATE_HINT, EXPLICIT_HINT, ScratchpadManager,
    batch_judge_enabled,
)
from baize.core.temporal_graph import BiTemporalGraphManager, plan_node_id
from baize.intelligence.batch_judge import BatchJudgeRunner
from baize.intelligence.phase_detector import llm_client_from_env
from baize.storage.db import init_db

logger = logging.getLogger("baize.consolidator")

_ENTITY_RE = re.compile(r"(proj:[\w\-]+)")
_DECISION_RE = re.compile(r"(?:定案|改为|敲定)[::]?\s*(.+)")
_SUPERSEDE_RE = re.compile(r"(?:推翻|废弃|取代)[::]?\s*(.+?)(?:[,,。;;]|$)|(.+?)\s*(?:推翻|废弃|取代)")
_ALIAS_RE = re.compile(r"alias[::]\s*([^\s]+)")
_ALIAS_SPLIT_RE = re.compile(r"[,,、]")
_EXPLORATION_RE = re.compile(r"假设|如果|要不|探讨|尚未批准|[?？]|呢$")
_LEADING_PUNCT = " \t\r\n:："
_GENERAL_ENTITY = "proj:general_decisions"
_DELETE_CHUNK = 500
# Issue #44 CTO 红线:纯通用词禁止作为别名入库,已入库的在慢波下次触碰该卡时清洗(精确匹配,不误伤「时间机器」类长词)
_GENERIC_ALIAS_BLACKLIST = frozenset({
    "记忆", "方案", "项目", "系统", "服务", "配置", "记录", "问题", "时间", "数据", "工具",
})
_LLM_SYSTEM_PROMPT = (
    "从对话中提取最终定案 decision、被推翻的方案 superseded、简洁中文名 display_name "
    "与别名数组 aliases,只输出 JSON。"
)
_CANDIDATE_VERIFY_SYSTEM_PROMPT = (
    "你是记忆核实器。判断候选文本是否为可永久固化的客观事实"
    "(个人档案/硬件配置/网络拓扑/否定修正/长期偏好/明确的既成事实),"
    '疑似计划、猜测、观点一律否决。只输出 JSON:{"is_fact": true|false, "entity": "实体ID或空串"}。'
)
_CANDIDATE_DECISION_VERIFY_SYSTEM_PROMPT = (
    "你是定案核实器。判断候选文本是否为一次正式定案(对某件事明确拍板、同意执行或敲定方案),"
    "定的是哪件事、归属哪个实体;未来计划、犹豫、试探、单纯附和闲聊一律否决。"
    '只输出 JSON:{"is_decision": true|false, "subject": "定的是哪件事或空串", "entity": "实体ID或空串"}。'
)


def _is_authoritative(log: Dict[str, Any]) -> bool:
    return (
        log["role"] == "user"
        and log["phase_hint"] == "approval"
        and not _EXPLORATION_RE.search(log["raw_text"])
    )


def _is_explicit_addition(log: Dict[str, Any]) -> bool:
    return log.get("phase_hint") == EXPLICIT_HINT and not _EXPLORATION_RE.search(log["raw_text"])


def _clean_decision(text: Optional[str]) -> Optional[str]:
    return (text or "").lstrip(_LEADING_PUNCT).strip() or None


def _resolve_explicit_entity(text: str, index: Dict[str, str]) -> str:
    for anchor in _ENTITY_RE.findall(text):
        if anchor in index:
            return index[anchor]
        bare = anchor[len("proj:"):]
        if bare in index:
            return index[bare]
    return _GENERAL_ENTITY


def _merge_facts(current: Optional[Dict[str, Any]], new: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    existing = current["metadata"].get("facts", []) if current else []
    if not isinstance(existing, list):
        existing = []
    seen = {fact.get("id") for fact in existing if isinstance(fact, dict)}
    return existing + [fact for fact in new if fact["id"] not in seen]


def _split_aliases(raw: str) -> List[str]:
    return [a.strip() for a in _ALIAS_SPLIT_RE.split(raw) if a.strip()]


def _filter_generic_aliases(aliases: List[str]) -> List[str]:
    return [a for a in aliases if a not in _GENERIC_ALIAS_BLACKLIST]


def _union(*groups: List[str]) -> List[str]:
    seen: Dict[str, None] = {}
    for group in groups:
        for item in group:
            seen.setdefault(item, None)
    return list(seen)


def _squash(text: Optional[str]) -> str:
    return " ".join((text or "").split())

def _describe_change(plan: Dict[str, Any], before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> Dict[str, Any]:
    """Issue #78:一次卡片回写的变更说明(dry_run 预览与真实执行同口径)。"""
    if before is None:
        kind = "new_card"
    elif plan["decision"] is None:
        kind = "facts_or_aliases_only"
    elif _squash(plan["decision"]) != _squash(before["current_decision"]):
        kind = "decision_replaced"
    else:
        kind = "decision_unchanged"
    def count(state: Optional[Dict[str, Any]], key: str) -> int:
        value = state["metadata"].get(key) if state else None
        return len(value) if isinstance(value, list) else 0
    return {
        "entity_id": after["entity_id"],
        "change": kind,
        "version_from": before["version"] if before else None,
        "version_to": after["version"],
        "decision_from": before["current_decision"] if before else None,
        "decision_to": after["current_decision"],
        "tombstones_added": after["tombstones"][len(before["tombstones"]) if before else 0:],
        "facts_added": count(after, "facts") - count(before, "facts"),
        "aliases_added": count(after, "aliases") - count(before, "aliases"),
        "folded_log_ids": [log["id"] for log in plan["logs"]],
    }


def _entity_index_of(states: List[Dict[str, Any]]) -> Dict[str, str]:
    index: Dict[str, str] = {}
    for state in states:
        index[state["entity_id"]] = state["entity_id"]
        for alias in state["metadata"].get("aliases", []):
            index.setdefault(alias, state["entity_id"])
    return index

class _Snapshot:
    """Issue #87:阶段 1 在锁内取好的内存快照。阶段 2 的分析与模型调用只读它,不再碰共享的数据库连接。"""

    def __init__(
        self,
        phase_logs: List[Dict[str, Any]],
        states: List[Dict[str, Any]],
        recent_entity: Optional[str],
        fingerprints: Dict[str, Tuple[Any, ...]],
    ):
        self.phase_logs = phase_logs
        self.states = states
        self.recent_entity = recent_entity
        self.fingerprints = fingerprints
        self.index = _entity_index_of(states)
        self.catalog = "; ".join(
            f"{state['entity_id']}({','.join(state['metadata'].get('aliases', []))})" for state in states
        )

def _empty_candidates() -> Dict[str, Any]:
    return {"verified": 0, "rejected": [], "skipped": []}

def _open_unique_archive(archive_dir: str, prefix: str) -> Tuple[str, Any]:
    while True:
        path = os.path.join(archive_dir, f"{prefix}_{int(time.time() * 1000)}.jsonl.gz")
        try:
            return path, open(path, "xb")
        except FileExistsError:
            continue


class DreamConsolidator:
    def __init__(
        self,
        conn: sqlite3.Connection,
        archive_dir: str = "cold_archive",
        llm_client: Optional[Any] = None,
    ):
        self.conn = conn
        self.archive_dir = archive_dir
        self.llm_client = llm_client
        self.scratchpad = ScratchpadManager(conn)
        self.living = LivingStateManager(conn)
        self.graph = BiTemporalGraphManager(conn)
        # Issue #87:整理专用互斥,拿不到就返回 busy,不排队、不重复整理
        self._run_mutex = threading.Lock()

    def run_consolidation(
        self, target_date: Optional[str] = None, dry_run: bool = False, lock: Optional[Any] = None
    ) -> Dict[str, Any]:
        """Issue #87:三段式——锁内取快照 -> 锁外只对内存快照分析并调模型 -> 锁内重新检查再回写。

        lock 为共享连接所属的全局锁(HTTP 服务传入);命令行与 MCP 是独立连接,不传。
        真实整理走非阻塞互斥,第二次进来直接返回 busy;dry_run 只读预览,不占互斥。
        """
        if dry_run:
            return self._run(target_date, True, lock)
        if not self._run_mutex.acquire(blocking=False):
            return {
                "status": "busy", "archived_logs": 0, "updated_states": [], "archive_file": None,
                "skipped_explicit": [], "batch_judge": {"status": "busy"}, "candidates": _empty_candidates(),
                "message": "整理正在进行,本次请求未执行",
            }
        try:
            return self._run(target_date, False, lock)
        finally:
            self._run_mutex.release()

    def _run(self, target_date: Optional[str], dry_run: bool, lock: Optional[Any]) -> Dict[str, Any]:
        guard = lock if lock is not None else nullcontext()
        # Issue #78:dry_run 真跑分析与回写,读出结果后整体回滚;不跑批量判研(会写库)、不写冷归档
        judge_report = {"status": "skipped_dry_run"} if dry_run else self._batch_judge_step(lock)
        with guard:
            snap = self._take_snapshot(target_date)
        phase_logs = snap.phase_logs
        explicit_logs = [log for log in phase_logs if log["phase_hint"] == EXPLICIT_HINT]
        candidate_logs = [log for log in phase_logs if log["phase_hint"] == CANDIDATE_HINT]
        # Issue #75:已软删除(expired_at)的定案/推翻行不再参与分析,否则软清理挡不住夜间折叠
        plans = self._analyze([
            log for log in phase_logs
            if log["phase_hint"] not in (EXPLICIT_HINT, CANDIDATE_HINT) and not log.get("expired_at")
        ], snap)
        skipped = self._merge_explicit(plans, explicit_logs, snap)
        candidates = self._merge_candidates(plans, candidate_logs, snap)
        folded = [log for plan in plans for log in plan["logs"]]
        if not folded and not candidates["rejected"]:
            report = {
                "status": "ok", "archived_logs": 0, "updated_states": [],
                "archive_file": None, "skipped_explicit": skipped,
                "batch_judge": judge_report, "candidates": candidates,
            }
            if dry_run:
                report.update(
                    status="dry_run", dry_run=True, would_archive_logs=0, would_update_states=[], changes=[]
                )
            return report
        with guard:
            return self._commit(snap, plans, candidates, skipped, judge_report, target_date, dry_run)

    def _take_snapshot(self, target_date: Optional[str]) -> _Snapshot:
        phase_logs = self.scratchpad.export_pending_phase_logs(target_date)
        states = self.living.list_active_states()
        fingerprints = {
            row[0]: (row[1], row[2], row[3])
            for row in self.conn.execute("SELECT entity_id, version, updated_at, current_decision FROM living_states")
        }
        row = self.conn.execute(
            """
            SELECT entity_id FROM living_states
            WHERE status = 'ACTIVE' AND updated_at >= datetime('now', '-30 minutes')
            ORDER BY updated_at DESC LIMIT 1
            """
        ).fetchone()
        return _Snapshot(phase_logs, states, row[0] if row else None, fingerprints)

    def _commit(
        self,
        snap: _Snapshot,
        plans: List[Dict[str, Any]],
        candidates: Dict[str, Any],
        skipped: List[Dict[str, Any]],
        judge_report: Dict[str, Any],
        target_date: Optional[str],
        dry_run: bool,
    ) -> Dict[str, Any]:
        """阶段 3(调用方持锁):写事务内重新检查,只归档并回写仍然有效的部分。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            plans, rejected, skipped_changed = self._recheck(snap, plans, candidates["rejected"])
            folded = [log for plan in plans for log in plan["logs"]]
            total = len(folded) + len(rejected)
            report: Dict[str, Any]
            if total == 0:
                self.conn.rollback()
                report = {
                    "status": "ok", "archived_logs": 0, "updated_states": [], "archive_file": None,
                    "skipped_explicit": skipped, "batch_judge": judge_report, "candidates": candidates,
                }
                if dry_run:
                    report.update(
                        status="dry_run", dry_run=True, would_archive_logs=0, would_update_states=[], changes=[]
                    )
            elif dry_run:
                changes: List[Dict[str, Any]] = []
                would_update = self._apply(plans, rejected, changes=changes, in_txn=True)
                self.conn.rollback()
                report = {
                    "status": "dry_run", "dry_run": True,
                    "archived_logs": 0, "updated_states": [], "archive_file": None,
                    "would_archive_logs": total,
                    "would_update_states": would_update, "changes": changes,
                    "skipped_explicit": skipped, "batch_judge": judge_report, "candidates": candidates,
                }
            else:
                archive_file = self._write_archive(folded + rejected, f"scratchpad_{target_date or 'all'}")
                updated_states = self._apply(plans, rejected, in_txn=True)
                self.conn.commit()
                report = {
                    "status": "ok",
                    "archived_logs": total,
                    "updated_states": updated_states,
                    "archive_file": archive_file,
                    "skipped_explicit": skipped,
                    "batch_judge": judge_report,
                    "candidates": candidates,
                }
        except Exception:
            self.conn.rollback()
            raise
        if skipped_changed:
            report["skipped_changed"] = skipped_changed
        return report

    def _recheck(
        self, snap: _Snapshot, plans: List[Dict[str, Any]], rejected: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """卡片指纹(版本、更新时间、定案)与快照一致、来源草稿行都还在且未过期,这一组才写;否则整组跳过留到下次。"""
        ids = [log["id"] for plan in plans for log in plan["logs"]] + [log["id"] for log in rejected]
        live: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
        for start in range(0, len(ids), _DELETE_CHUNK):
            chunk = ids[start:start + _DELETE_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            for row_id, phase_hint, expired_at in self.conn.execute(
                f"SELECT id, phase_hint, expired_at FROM scratchpad_log WHERE id IN ({placeholders})", chunk
            ):
                live[row_id] = (phase_hint, expired_at)

        def still_valid(log: Dict[str, Any]) -> bool:
            row = live.get(log["id"])
            return row is not None and row[1] is None and row[0] == log["phase_hint"]

        current = {
            row[0]: (row[1], row[2], row[3])
            for row in self.conn.execute("SELECT entity_id, version, updated_at, current_decision FROM living_states")
        }
        kept: List[Dict[str, Any]] = []
        skipped_changed: List[Dict[str, Any]] = []
        for plan in plans:
            entity_id = plan["entity_id"]
            if current.get(entity_id) != snap.fingerprints.get(entity_id):
                skipped_changed.append({"entity_id": entity_id, "reason": "card_changed"})
            elif not all(still_valid(log) for log in plan["logs"]):
                skipped_changed.append({"entity_id": entity_id, "reason": "source_rows_changed"})
            else:
                kept.append(plan)
        return kept, [log for log in rejected if still_valid(log)], skipped_changed

    def _batch_judge_step(self, lock: Optional[Any] = None) -> Dict[str, Any]:
        # Issue #44 PR4:System 2 判相态准召未经标注样本验证前默认不跑,防夜间误判改写定案
        if not batch_judge_enabled():
            return {"status": "disabled"}
        if self.llm_client is None:
            return {"status": "skipped_no_llm"}
        # Issue #56 P0-2:增强项失败不得拖垮夜间主折叠
        try:
            return BatchJudgeRunner(self.conn, self.llm_client, lock=lock).run()
        except Exception as exc:
            with (lock if lock is not None else nullcontext()):
                self.conn.rollback()
            logger.warning("batch judge step failed: %s", exc)
            return {"status": "failed", "error": str(exc)[:200]}

    def _merge_explicit(
        self, plans: List[Dict[str, Any]], logs: List[Dict[str, Any]], snap: _Snapshot
    ) -> List[Dict[str, Any]]:
        skipped: List[Dict[str, Any]] = []
        by_entity = {plan["entity_id"]: plan for plan in plans}
        index = dict(snap.index)
        index.update({entity_id: entity_id for entity_id in by_entity})
        for log in sorted(logs, key=lambda item: (item["created_at"], item["id"])):
            if log.get("expired_at"):
                skipped.append({"id": log["id"], "reason": "已被后续记录推翻(supersedes_id),不再固化"})
                continue
            if not _is_explicit_addition(log):
                skipped.append({"id": log["id"], "reason": "含探索或疑问措辞,不作为事实固化"})
                continue
            entity_id = _resolve_explicit_entity(log["raw_text"], index)
            plan = by_entity.get(entity_id)
            if plan is None:
                plan = {
                    "entity_id": entity_id, "logs": [], "decision": None, "superseded": None,
                    "display_name": None, "aliases": [], "facts": [],
                }
                by_entity[entity_id] = plan
                plans.append(plan)
            plan["logs"].append(log)
            plan.setdefault("facts", []).append({
                "id": log["id"], "text": log["raw_text"], "category": log.get("category"),
                "reason": log.get("reason"), "created_at": log["created_at"],
            })
        return skipped

    def _merge_candidates(
        self, plans: List[Dict[str, Any]], logs: List[Dict[str, Any]], snap: _Snapshot
    ) -> Dict[str, Any]:
        """红线 1:candidate 经 System 2 二次核实——通过并入 facts,否决归档清退,不参与定案改写。"""
        report: Dict[str, Any] = {"verified": 0, "rejected": [], "skipped": []}
        if not logs:
            return report
        if self.llm_client is None:
            report["skipped"] = [{"id": log["id"], "reason": "无 System 2 可核实,保留待下次"} for log in logs]
            return report
        by_entity = {plan["entity_id"]: plan for plan in plans}
        index = dict(snap.index)
        index.update({entity_id: entity_id for entity_id in by_entity})
        for log in sorted(logs, key=lambda item: (item["created_at"], item["id"])):
            if log.get("expired_at"):
                continue
            kind = CANDIDATE_DECISION if log.get("category") == CANDIDATE_DECISION else CANDIDATE_FACT
            try:
                verdict = self._verify_candidate(log["raw_text"], kind, snap.catalog)
            except Exception:
                report["skipped"].append({"id": log["id"], "reason": "核实调用失败,保留待下次"})
                continue
            if not verdict["ok"]:
                reason = "核实为非正式定案" if kind == CANDIDATE_DECISION else "核实为非客观事实"
                report["rejected"].append({**log, "reject_reason": reason})
                continue
            entity_id = self._resolve_verified_entity(verdict.get("entity"), log["raw_text"], index)
            plan = by_entity.get(entity_id)
            if plan is None:
                plan = {
                    "entity_id": entity_id, "logs": [], "decision": None, "superseded": None,
                    "display_name": None, "aliases": [], "facts": [],
                }
                by_entity[entity_id] = plan
                plans.append(plan)
            plan["logs"].append(log)
            fact = {
                "id": log["id"], "text": log["raw_text"], "category": kind,
                "reason": f"candidate:{log.get('reason') or 'verified'}",
                "created_at": log["created_at"], "noul": log["noul_score"],
            }
            if verdict["subject"]:
                fact["subject"] = verdict["subject"]
            plan.setdefault("facts", []).append(fact)
            report["verified"] += 1
        return report

    def _verify_candidate(self, text: str, kind: str, catalog: str) -> Dict[str, Any]:
        is_decision = kind == CANDIDATE_DECISION
        raw = self.llm_client.chat(
            _CANDIDATE_DECISION_VERIFY_SYSTEM_PROMPT if is_decision else _CANDIDATE_VERIFY_SYSTEM_PROMPT,
            f"候选文本:{text}\n\n已知实体:{catalog or '无'}",
        )
        data = json.loads(raw)
        key = "is_decision" if is_decision else "is_fact"
        if not isinstance(data, dict) or type(data.get(key)) is not bool:
            raise ValueError("candidate verification requires a JSON boolean")
        return {
            "ok": data[key],
            "entity": data.get("entity") or "",
            "subject": (data.get("subject") or "").strip() if is_decision else "",
        }

    def _resolve_verified_entity(
        self, proposed: Any, text: str, index: Dict[str, str]
    ) -> str:
        if isinstance(proposed, str) and proposed in index:
            return index[proposed]
        return _resolve_explicit_entity(text, index)

    def purge_scratchpad(
        self,
        before_id: Optional[int] = None,
        role: Optional[str] = None,
        dry_run: bool = True,
        before_date: Optional[str] = None,
        include_pending_phase: bool = False,
    ) -> Dict[str, Any]:
        rows = self.scratchpad.find_for_purge(before_id, role, before_date, include_pending_phase)
        protected = 0 if include_pending_phase else self.scratchpad.count_pending_for_purge(
            before_id, role, before_date
        )
        ids = [r["id"] for r in rows]
        report: Dict[str, Any] = {
            "dry_run": dry_run,
            "include_pending_phase": include_pending_phase,
            "protected_pending": protected,
            "matched": len(rows),
            "ids": ids,
            "sample": [
                {"id": r["id"], "role": r["role"], "raw_text": r["raw_text"][:60]} for r in rows[:10]
            ],
            "archive_file": None,
            "purged": 0,
        }
        if dry_run or not rows:
            return report
        report["archive_file"] = self._write_archive(rows, "purged")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for start in range(0, len(ids), _DELETE_CHUNK):
                chunk = ids[start:start + _DELETE_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                self.conn.execute(f"DELETE FROM scratchpad_log WHERE id IN ({placeholders})", chunk)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        report["purged"] = len(ids)
        return report

    def backfill_display_names(self, dry_run: bool = True) -> List[Dict[str, str]]:
        changes = []
        for state in self.living.list_active_states():
            if state["display_name"] != state["entity_id"]:
                continue
            aliases = state["metadata"].get("aliases", [])
            if not aliases:
                continue
            changes.append({
                "entity_id": state["entity_id"],
                "old_display_name": state["display_name"],
                "new_display_name": aliases[0],
            })
        if not dry_run:
            for change in changes:
                self.living.rename_display(change["entity_id"], change["new_display_name"])
        return changes

    def _analyze(self, phase_logs: List[Dict[str, Any]], snap: _Snapshot) -> List[Dict[str, Any]]:
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        last_entity: Dict[str, str] = {}
        for log in phase_logs:
            entity = self._extract_entity(log["raw_text"])
            if entity:
                last_entity[log["session_id"]] = entity
            elif _is_authoritative(log):
                entity = last_entity.get(log["session_id"]) or snap.recent_entity or _GENERAL_ENTITY
            if entity:
                groups[entity].append(log)

        plans: List[Dict[str, Any]] = []
        for entity_id, logs in groups.items():
            extracted = self._consolidate_group(logs)
            if extracted and extracted["decision"]:
                plans.append({"entity_id": entity_id, "logs": logs, **extracted})
        return plans

    def _consolidate_group(self, logs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not any(_is_authoritative(log) for log in logs):
            return None
        user_logs = [log for log in logs if log["role"] == "user"]
        if self.llm_client is not None:
            try:
                return self._consolidate_with_llm(user_logs)
            except Exception:
                pass
        return self._consolidate_by_rules(user_logs)

    def _consolidate_with_llm(self, logs: List[Dict[str, Any]]) -> Dict[str, Any]:
        transcript = "\n".join(log["raw_text"] for log in logs)
        raw = self.llm_client.chat(_LLM_SYSTEM_PROMPT, transcript)
        data = json.loads(raw)
        return {
            "decision": _clean_decision(data.get("decision")),
            "superseded": data.get("superseded") or None,
            "display_name": (data.get("display_name") or "").strip() or None,
            "aliases": [a.strip() for a in (data.get("aliases") or []) if a and a.strip()],
        }

    def _consolidate_by_rules(self, logs: List[Dict[str, Any]]) -> Dict[str, Any]:
        decision = None
        superseded = None
        aliases: List[str] = []
        for log in logs:
            text = log["raw_text"]
            if _is_authoritative(log):
                m = _DECISION_RE.search(text)
                if m:
                    decision = _clean_decision(m.group(1))
                a = _ALIAS_RE.search(text)
                if a:
                    aliases.extend(_split_aliases(a.group(1)))
            if log["phase_hint"] == "rejection":
                s = _SUPERSEDE_RE.search(text)
                if s:
                    superseded = (s.group(1) or s.group(2)).strip()
        return {"decision": decision, "superseded": superseded, "display_name": None, "aliases": aliases}

    def _extract_entity(self, text: str) -> Optional[str]:
        m = _ENTITY_RE.search(text)
        return m.group(1) if m else None

    def _write_archive(self, logs: List[Dict[str, Any]], prefix: str) -> str:
        os.makedirs(self.archive_dir, exist_ok=True)
        path, raw = _open_unique_archive(self.archive_dir, prefix)
        with raw:
            with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
                for log in logs:
                    line = json.dumps({
                        "id": log["id"], "session_id": log["session_id"], "role": log["role"],
                        "raw_text": log["raw_text"], "phase_hint": log["phase_hint"],
                        "noul_score": log["noul_score"], "created_at": log["created_at"],
                        "category": log.get("category"), "reason": log.get("reason"),
                    }, ensure_ascii=False) + "\n"
                    gz.write(line.encode("utf-8"))
            raw.flush()
            os.fsync(raw.fileno())
        return path

    def _apply(
        self,
        plans: List[Dict[str, Any]],
        rejected_logs: Optional[List[Dict[str, Any]]] = None,
        changes: Optional[List[Dict[str, Any]]] = None,
        dry_run: bool = False,
        in_txn: bool = False,
    ) -> List[str]:
        # in_txn=True:调用方(_commit)已开写事务并负责提交或回滚
        updated: List[str] = []
        now = self._now()
        if not in_txn:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            for plan in plans:
                entity_id = plan["entity_id"]
                decision = plan["decision"]
                current = self.living.get_state(entity_id)
                display = self.living.resolve_display_name(entity_id, plan["display_name"])
                existing_aliases = current["metadata"].get("aliases", []) if current else []
                aliases = _filter_generic_aliases(_union(existing_aliases, plan["aliases"]))
                metadata: Dict[str, Any] = {}
                if aliases or existing_aliases:
                    metadata["aliases"] = aliases
                facts = _merge_facts(current, plan.get("facts", []))
                if facts:
                    metadata["facts"] = facts

                source_ids = [log["id"] for log in plan["logs"]]
                if decision is None:
                    after = self.living.update_decision(
                        entity_id,
                        display,
                        current["current_decision"] if current else "",
                        metadata=metadata or None,
                        commit=False,
                        source="consolidation",
                        source_log_ids=source_ids,
                    )
                    if changes is not None:
                        changes.append(_describe_change(plan, current, after))
                    updated.append(entity_id)
                    continue

                plan_id = plan_node_id(entity_id, decision)

                self.graph.upsert_node(entity_id, display, "project", commit=False)
                self.graph.upsert_node(plan_id, decision, "rule", commit=False)
                self._retire_plan_edges(entity_id, plan_id, now)
                after = self.living.update_decision(
                    entity_id,
                    display,
                    decision,
                    superseded_plan=plan["superseded"],
                    killed_reason="睡眠合成:被新定案取代" if plan["superseded"] else None,
                    metadata=metadata or None,
                    commit=False,
                    source="consolidation",
                    source_log_ids=source_ids,
                )
                if changes is not None:
                    changes.append(_describe_change(plan, current, after))
                if not self._has_active_decision_edge(entity_id, plan_id):
                    self.graph.add_edge(entity_id, plan_id, "decided_as", valid_at=now, commit=False)
                updated.append(entity_id)

            folded_ids = [log["id"] for plan in plans for log in plan["logs"]]
            folded_ids += [log["id"] for log in (rejected_logs or [])]
            for start in range(0, len(folded_ids), _DELETE_CHUNK):
                chunk = folded_ids[start:start + _DELETE_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                self.conn.execute(f"DELETE FROM scratchpad_log WHERE id IN ({placeholders})", chunk)
            if in_txn:
                pass
            elif dry_run:
                self.conn.rollback()
            else:
                self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return updated

    def _retire_plan_edges(self, entity_id: str, new_plan_id: str, now: str) -> None:
        rows = self.conn.execute(
            """
            SELECT dst_id FROM v_active_graph_edges
            WHERE src_id = ? AND rel_type = 'decided_as' AND dst_id != ?
            """,
            (entity_id, new_plan_id),
        ).fetchall()
        for (dst,) in rows:
            self.graph.invalidate_edge(entity_id, dst, "decided_as", now, commit=False)

    def _has_active_decision_edge(self, entity_id: str, plan_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM v_active_graph_edges
            WHERE src_id = ? AND dst_id = ? AND rel_type = 'decided_as' LIMIT 1
            """,
            (entity_id, plan_id),
        ).fetchone()
        return row is not None

    def _now(self) -> str:
        return self.conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now')").fetchone()[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="白泽·Cortex 慢波睡眠合成")
    parser.add_argument("--db", default="baize.db")
    parser.add_argument("--archive-dir", default="cold_archive")
    parser.add_argument("--date", default=None)
    args = parser.parse_args()
    conn = init_db(args.db)
    try:
        consolidator = DreamConsolidator(conn, archive_dir=args.archive_dir, llm_client=llm_client_from_env())
        report = consolidator.run_consolidation(args.date)
        print(json.dumps(report, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
