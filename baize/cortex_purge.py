"""草稿流冷归档一键脚本:默认 dry-run,加 --execute 才会先写 cold_archive 再删除。

用法: python -m baize.cortex_purge --db baize.db --days 30 [--dry-run | --execute]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from baize.core.consolidator import DreamConsolidator
from baize.core.scratchpad import TIMESTAMP_FORMAT
from baize.storage.db import init_db


def cutoff_from_days(days: int, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).strftime(TIMESTAMP_FORMAT)


def _non_negative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"天数必须是整数: {raw}")
    if value < 0:
        raise argparse.ArgumentTypeError("天数不能为负数")
    return value


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="白泽·Cortex 草稿流冷归档(默认 dry-run)")
    parser.add_argument("--db", default="baize.db")
    parser.add_argument("--archive-dir", default="cold_archive")
    parser.add_argument("--days", type=_non_negative_int, default=30)
    parser.add_argument("--role", default=None)
    parser.add_argument(
        "--include-pending-phase",
        action="store_true",
        help="连同 phase_hint 非空的待固化记录(approval / explicit_addition 等)一并归档,默认豁免",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只列出将被归档的记录(默认)")
    mode.add_argument("--execute", action="store_true", help="先写入 cold_archive 再删除")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.db):
        print(f"数据库不存在: {args.db}", file=sys.stderr)
        return 2
    conn = init_db(args.db)
    try:
        report = DreamConsolidator(conn, archive_dir=args.archive_dir).purge_scratchpad(
            role=args.role,
            dry_run=not args.execute,
            before_date=cutoff_from_days(args.days),
            include_pending_phase=args.include_pending_phase,
        )
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
