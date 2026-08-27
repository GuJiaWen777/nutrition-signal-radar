"""Split the current open monitoring cycle into weekly cycles.

Usage:
    python scripts/split_cycle_into_weeks.py [--db var/radar.sqlite3] [--cycle SLUG] [--dry-run] [--yes]

The default week ranges follow the 2026-07-17 ~ 2026-08-16 split decision:
four weeks aligned to the calendar week ending 2026-08-16 (the first cycle
absorbs the three head days 2026-07-17 ~ 2026-07-19), plus a blank follow-up
cycle 2026-08-17 ~ 2026-08-23 that stays open.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nutrition_signal_radar.cycle_service import get_monitoring_cycle  # noqa: E402
from nutrition_signal_radar.cycle_split import (  # noqa: E402
    DEFAULT_NEXT_CYCLE_RANGE,
    DEFAULT_WEEK_RANGES,
    split_cycle_into_weeks,
    week_slug,
)


def _count(database_path: Path, start: str, end: str) -> int:
    connection = sqlite3.connect(database_path)
    try:
        return int(
            connection.execute(
                """
                SELECT COUNT(*) FROM source_articles
                WHERE substr(published_at, 1, 10) BETWEEN ? AND ?
                """,
                (start, end),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="var/radar.sqlite3")
    parser.add_argument("--cycle", default=None, help="周期次 slug（默认当前开放期次）")
    parser.add_argument("--dry-run", action="store_true", help="只打印拆分计划，不执行")
    parser.add_argument("--yes", action="store_true", help="跳过确认提示")
    args = parser.parse_args()

    database = Path(args.db)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        source = get_monitoring_cycle(connection, slug=args.cycle)
    finally:
        connection.close()
    if source is None:
        print("没有找到可拆分的监测期次")
        raise SystemExit(1)
    if source["status"] != "open":
        print(f"期次 {source['slug']} 已关闭，只有开放中的期次才能拆分")
        raise SystemExit(1)

    print(f"源期次：{source['slug']}（{source['label']}）")
    print("拆分计划：")
    for start, end in DEFAULT_WEEK_RANGES:
        count = _count(database, start, end)
        print(
            f"  {week_slug(start, end)}  监测周次 {start} ~ {end}  "
            f"{count} 篇文章（结束后关闭）"
        )
    next_start, next_end = DEFAULT_NEXT_CYCLE_RANGE
    next_count = _count(database, next_start, next_end)
    print(
        f"  {week_slug(next_start, next_end)}  监测周次 {next_start} ~ {next_end}  "
        f"{next_count} 篇文章（保持开放）"
    )

    if args.dry_run:
        print("--dry-run：未执行任何写入")
        return

    if not args.yes:
        answer = input("继续执行拆分？(y/N) ").strip().lower()
        if answer != "y":
            print("已取消")
            return

    result = split_cycle_into_weeks(database)
    print(f"\n已关闭源期次 {result.source_cycle_slug}，完成拆分：")
    for week in result.weeks:
        print(
            f"  {week.slug}  文章 {week.article_count}  簇 {week.cluster_count}  "
            f"信号 {week.signal_count}  快报 v{week.brief_version}  {week.status}"
        )
    if result.next_cycle:
        week = result.next_cycle
        print(
            f"  {week.slug}  文章 {week.article_count}  簇 {week.cluster_count}  "
            f"信号 {week.signal_count}  快报 v{week.brief_version}  {week.status}"
        )


if __name__ == "__main__":
    main()
