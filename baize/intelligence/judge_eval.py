"""Issue #64:只读抽样与 System 2 准召评测,不修改生产开关或数据库。"""
import argparse
import hashlib
import json
import os
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from baize.core.scratchpad import validate_timestamp
from baize.intelligence.batch_judge import (
    APPROVAL_CONFIDENCE_GATE, BATCH_SIZE, MAX_TEXT_CHARS, _SYSTEM_PROMPT, BatchJudgeRunner,
)
from baize.intelligence.phase_detector import PHASE_CATEGORY, OpenAICompatClient, llm_settings_from_env

KINDS = {"decision", "fact", "plan", "chitchat", "config"}
AUTHORITY_PHASES = {"decision_approved", "rejection"}


def export_dataset(db_path: str, after_date: str, before_date: str, limit: int = 100, seed: int = 20261026) -> Dict[str, Any]:
    validate_timestamp(after_date)
    validate_timestamp(before_date)
    if after_date >= before_date or not 50 <= limit <= 100:
        raise ValueError("需要递增时间窗,样本上限必须为 50~100")
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, session_id, raw_text, created_at FROM scratchpad_log "
            "WHERE role = 'user' AND phase_hint IS NULL AND expired_at IS NULL "
            "AND created_at >= ? AND created_at < ? ORDER BY session_id, id",
            (after_date, before_date),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) < 50:
        raise ValueError("窗口内可用记录不足 50 条")
    chosen = random.Random(seed).sample(rows, min(limit, len(rows)))
    chosen.sort(key=lambda row: (row[1] or "", row[0]))
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "sampling": {"after_date": after_date, "before_date": before_date, "seed": seed, "population": len(rows)},
        "annotation": {"status": "unreviewed", "reviewer": ""},
        "samples": [
            {"id": row[0], "session_id": row[1], "text": row[2], "created_at": row[3],
             "kind": None, "expected_phase": None}
            for row in chosen
        ],
    }


def _samples(dataset: Dict[str, Any]) -> List[Dict[str, Any]]:
    samples = dataset.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("samples 必须是非空数组")
    ids = set()
    for row in samples:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] in ids:
            raise ValueError("样本 ID 必须是唯一整数")
        if not isinstance(row.get("text"), str) or not isinstance(row.get("session_id"), (str, type(None))):
            raise ValueError("样本文本或会话格式错误")
        if row.get("expected_phase") is not None and row["expected_phase"] not in PHASE_CATEGORY:
            raise ValueError("未知的人工 phase 标注")
        if row.get("kind") is not None and row["kind"] not in KINDS:
            raise ValueError("未知的样本类型")
        ids.add(row["id"])
    return sorted(samples, key=lambda row: (row.get("session_id") or "", row["id"]))


def score_trial(samples: List[Dict[str, Any]], predictions: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_id = {row["id"]: row for row in predictions}
    labelled = [row for row in samples if row.get("expected_phase") is not None]
    correct = tp = fp = fn = tn = wrong_phase = 0
    for row in labelled:
        got = by_id.get(row["id"])
        expected = row["expected_phase"] in AUTHORITY_PHASES
        actual = bool(got and got["phase"] in AUTHORITY_PHASES and got["confidence"] >= APPROVAL_CONFIDENCE_GATE)
        correct += bool(got and got["phase"] == row["expected_phase"])
        tp += actual and expected
        fp += actual and not expected
        fn += not actual and expected
        tn += not actual and not expected
        wrong_phase += bool(actual and expected and got["phase"] != row["expected_phase"])
    return {
        "coverage": len(by_id) / len(samples), "labelled": len(labelled),
        "phase_accuracy": correct / len(labelled) if labelled else None,
        "authority": {
            "tp": tp, "fp": fp, "fn": fn, "tn": tn, "wrong_phase": wrong_phase,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
        },
    }


def _sample_digest(samples: List[Dict[str, Any]]) -> str:
    canonical = json.dumps(
        [{key: row.get(key) for key in ("id", "session_id", "text")} for row in samples],
        ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def evaluate(dataset: Dict[str, Any], llm_client: Any, repeats: int = 3, seed: int = 20261026, progress=None) -> Dict[str, Any]:
    if repeats < 1:
        raise ValueError("repeats 必须为正数")
    samples = _samples(dataset)
    runner = BatchJudgeRunner(None, llm_client)
    trials = []
    baseline: Optional[Dict[int, str]] = None
    for index in range(repeats):
        batches = runner._slice([
            {"id": row["id"], "session_id": row.get("session_id"), "raw_text": row["text"]}
            for row in samples
        ])
        if index >= 2:
            # 只测同会话内的批次敏感性,不改变生产切片策略。
            groups: Dict[Any, List[Dict[str, Any]]] = {}
            for batch in batches:
                groups.setdefault(batch[0]["session_id"], []).extend(batch)
            ordered = []
            rng = random.Random(seed + index)
            for rows in groups.values():
                rng.shuffle(rows)
                ordered.extend(rows)
            batches = runner._slice(ordered)
        predictions, failures = [], []
        for number, batch in enumerate(batches, 1):
            try:
                predictions.extend(runner._judge_batch(batch))
            except Exception as error:
                failures.append({"ids": [row["id"] for row in batch], "error_type": type(error).__name__})
            if progress:
                progress(index + 1, number, len(batches))
        phases = {row["id"]: row["phase"] for row in predictions}
        reference = phases if baseline is None else baseline
        common = set(phases) & set(reference)
        agreement = sum(phases[key] == reference[key] for key in common) / len(common) if common else None
        trials.append({
            "name": f"{'base' if index < 2 else 'shuffle'}-{index + 1}",
            **score_trial(samples, predictions), "phase_agreement_with_base": agreement,
            "agreement_compared_count": len(common),
            "failed_batches": failures, "predictions": predictions,
        })
        if baseline is None:
            baseline = phases
    annotation = dataset.get("annotation") or {}
    reviewed = annotation.get("status") == "reviewed" and bool(str(annotation.get("reviewer") or "").strip())
    golden = reviewed and 50 <= len(samples) <= 100 and all(row.get("expected_phase") for row in samples)
    golden = golden and all(row.get("kind") in KINDS for row in samples)
    golden = golden and {row.get("kind") for row in samples} >= KINDS
    blockers = []
    if not golden:
        blockers.append("需要 50~100 条已人工复核、覆盖五类场景的完整 phase 标注")
    if repeats < 3:
        blockers.append("需要至少两轮同序与一轮同会话打乱评测")
    if any(trial["coverage"] != 1 for trial in trials):
        blockers.append("存在失败批次或缺失预测")
    if any(trial["phase_accuracy"] is None or trial["phase_accuracy"] < 0.95 for trial in trials):
        blockers.append("尚未证明每轮相态准确率均达到 95%")
    if any(trial["authority"]["fp"] or trial["authority"]["wrong_phase"] for trial in trials):
        blockers.append("存在非定案进入权威通道或批准/否决方向错误")
    if any(trial["authority"]["precision"] is None for trial in trials):
        blockers.append("存在无权威预测的轮次,不能据此声称精确率 100%")
    labels = json.dumps(
        {"annotation": annotation, "labels": [
            {key: row.get(key) for key in ("id", "kind", "expected_phase")} for row in samples
        ]}, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    return {
        "scope": "judge_preflight_only",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "protocol": {
            "batch_size": BATCH_SIZE, "max_text_chars": MAX_TEXT_CHARS, "seed": seed,
            "prompt_sha256": hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        },
        "sample_count": len(samples), "dataset_sha256": _sample_digest(samples),
        "annotation_sha256": hashlib.sha256(labels).hexdigest(),
        "model": getattr(llm_client, "model", None), "confidence_gate": APPROVAL_CONFIDENCE_GATE,
        "annotation": annotation, "golden_set_ready": bool(golden), "trials": trials,
        "judge_gate_passed": not blockers, "blockers": blockers,
    }


def rescore(dataset: Dict[str, Any], captured: Dict[str, Any]) -> Dict[str, Any]:
    samples = _samples(dataset)
    if _sample_digest(samples) != captured.get("dataset_sha256"):
        raise ValueError("样本 ID、会话或文本发生变化,禁止复用旧预测")
    protocol = captured.get("protocol") or {}
    expected = {
        "batch_size": BATCH_SIZE, "max_text_chars": MAX_TEXT_CHARS,
        "prompt_sha256": hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
    }
    if any(protocol.get(key) != value for key, value in expected.items()):
        raise ValueError("判研协议已变化,需要重新实测")
    trials = captured.get("trials") or []
    if not trials:
        raise ValueError("缺少逐轮预测")
    batch_count = len(BatchJudgeRunner(None, None)._slice([
        {"id": row["id"], "session_id": row.get("session_id")} for row in samples
    ]))

    class Replay:
        model = captured.get("model")
        calls = 0

        def chat(self, system, user):
            trial = trials[self.calls // batch_count]
            self.calls += 1
            by_id = {row["id"]: row for row in trial["predictions"]}
            return json.dumps([by_id[row["id"]] for row in json.loads(user) if row["id"] in by_id])

    report = evaluate(dataset, Replay(), repeats=len(trials), seed=protocol["seed"])
    report["prediction_source"] = "captured; no model calls"
    report["captured_at"] = captured.get("created_at")
    return report


def write_private_json(path: str, data: Dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--db", required=True)
    export.add_argument("--after-date", required=True)
    export.add_argument("--before-date", required=True)
    export.add_argument("--limit", type=int, default=100)
    export.add_argument("--seed", type=int, default=20261026)
    export.add_argument("--output", required=True)
    run = commands.add_parser("run")
    run.add_argument("--dataset", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--repeats", type=int, default=3)
    run.add_argument("--seed", type=int, default=20261026)
    replay = commands.add_parser("rescore")
    replay.add_argument("--dataset", required=True)
    replay.add_argument("--captured", required=True)
    replay.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "export":
        data = export_dataset(args.db, args.after_date, args.before_date, args.limit, args.seed)
    elif args.command == "rescore":
        data = rescore(
            json.loads(Path(args.dataset).read_text(encoding="utf-8")),
            json.loads(Path(args.captured).read_text(encoding="utf-8")),
        )
    else:
        settings = llm_settings_from_env()
        if settings is None:
            parser.error("需要配置 CORTEX_LLM_URL 与 CORTEX_LLM_KEY")
        data = evaluate(
            json.loads(Path(args.dataset).read_text(encoding="utf-8")), OpenAICompatClient(*settings),
            repeats=args.repeats, seed=args.seed,
            progress=lambda trial, batch, total: print(f"trial={trial} batch={batch}/{total}", flush=True),
        )
    write_private_json(args.output, data)
    print(json.dumps({"output": args.output, "judge_gate_passed": data.get("judge_gate_passed", False)}, ensure_ascii=False))
    return 1 if args.command != "export" and not data["judge_gate_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
