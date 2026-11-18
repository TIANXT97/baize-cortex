"""常驻规则人审清单导入脚本(Issue #105):JSON 清单 → 调常驻层写接口。默认 dry_run,加 --execute 才真写。

清单格式(见 docs/examples/resident_rules.example.json):
  {"batch_id": "...", "rules": [{"text": "...", "scope": "dm|group_ok", "priority": "hard|normal|low",
                                 "source": "...", "moved_from": "..."}]}

用法: python -m baize.cortex_resident_import manifest.json [--url http://localhost:8768] [--execute]
凭据读环境变量 CORTEX_TOKEN。执行前一定先对整份清单做一遍 dry_run;只要有一条被拒,整批都不写。
已存在相同文本的在用规则(409)记为「已存在」,不算失败,所以同一份清单可以重复执行。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_URL = "http://localhost:8768"
TIMEOUT = 15.0
RULE_KEYS = ("text", "scope", "priority", "source", "moved_from")

Sender = Callable[[str, Dict[str, Any]], Tuple[int, Dict[str, Any]]]


def load_manifest(path: str) -> Tuple[str, List[Dict[str, Any]]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list) or not data["rules"]:
        raise ValueError("清单必须是 {\"batch_id\": ..., \"rules\": [...]} 且 rules 非空")
    batch_id = data.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("清单缺少 batch_id")
    rules = []
    for index, rule in enumerate(data["rules"]):
        if not isinstance(rule, dict) or not isinstance(rule.get("text"), str):
            raise ValueError(f"第 {index + 1} 条缺少 text")
        unknown = set(rule) - set(RULE_KEYS)
        if unknown:
            raise ValueError(f"第 {index + 1} 条含不支持的字段: {sorted(unknown)}")
        rules.append(rule)
    return batch_id.strip(), rules


def http_sender(base_url: str, token: Optional[str]) -> Sender:
    def send(path: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            base_url.rstrip("/") + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
            except ValueError:
                return exc.code, {}
    return send


def _pass(send: Sender, batch_id: str, rules: List[Dict[str, Any]], dry_run: bool) -> List[Dict[str, Any]]:
    results = []
    for index, rule in enumerate(rules, start=1):
        status, body = send("/v1/resident/rules", {**rule, "batch_id": batch_id, "dry_run": dry_run})
        if status == 200:
            outcome = "ok"
        elif status == 409:
            outcome = "exists"
        else:
            outcome = "rejected"
        results.append({
            "index": index, "outcome": outcome, "http": status,
            "detail": body.get("detail") if outcome != "ok" else None,
            "reasons": body.get("reasons") or [],
            "id": (body.get("rule") or {}).get("id") if outcome == "ok" else None,
        })
    return results


def run(send: Sender, batch_id: str, rules: List[Dict[str, Any]], execute: bool) -> Dict[str, Any]:
    preview = _pass(send, batch_id, rules, dry_run=True)
    report: Dict[str, Any] = {"batch_id": batch_id, "mode": "execute" if execute else "dry_run", "preview": preview}
    rejected = [r for r in preview if r["outcome"] == "rejected"]
    report["ok"] = not rejected
    if rejected:
        report["aborted"] = "有规则未通过校验,整批不写"
    elif execute:
        report["applied"] = _pass(send, batch_id, rules, dry_run=False)
        report["ok"] = all(r["outcome"] in ("ok", "exists") for r in report["applied"])
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="白泽·Cortex 常驻规则清单导入(默认 dry_run)")
    parser.add_argument("manifest")
    parser.add_argument("--url", default=os.environ.get("CORTEX_URL", DEFAULT_URL))
    parser.add_argument("--execute", action="store_true", help="先整批 dry_run 全过,再真写;默认只 dry_run")
    args = parser.parse_args(argv)
    try:
        batch_id, rules = load_manifest(args.manifest)
    except (OSError, ValueError) as exc:
        print(f"清单无效: {exc}", file=sys.stderr)
        return 2
    try:
        report = run(http_sender(args.url, os.environ.get("CORTEX_TOKEN") or None), batch_id, rules, args.execute)
    except (urllib.error.URLError, OSError) as exc:
        print(f"连不上 cortex: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
