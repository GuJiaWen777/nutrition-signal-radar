"""Split one monitoring cycle into consecutive weekly cycles.

The schema enforces a single open cycle at a time, so each week is processed
while it is the open cycle and then closed; the follow-up cycle is left open.
Operational split of the 30-day drill cycle: weeks are aligned to the calendar
week ending 2026-08-16, with the first cycle absorbing the three head days
(2026-07-17 ~ 2026-07-19) so no article is lost.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .cycle_service import (
    MonitoringCycleError,
    attach_article_to_cycle,
    close_monitoring_cycle,
    create_monitoring_cycle,
    get_monitoring_cycle,
)
from .database import connect_database
from .demo_pipeline import build_demo_topics
from .schema import initialize_schema
from .weekly_service import export_weekly_signal_brief

DEFAULT_WEEK_RANGES = (
    ("2026-07-17", "2026-07-26"),
    ("2026-07-27", "2026-08-02"),
    ("2026-08-03", "2026-08-09"),
    ("2026-08-10", "2026-08-16"),
)
DEFAULT_NEXT_CYCLE_RANGE = ("2026-08-17", "2026-08-23")


@dataclass(frozen=True)
class SplitWeekResult:
    slug: str
    label: str
    start_date: str
    end_date: str
    article_count: int
    cluster_count: int
    signal_count: int
    brief_version: int | None
    status: str


@dataclass(frozen=True)
class CycleSplitResult:
    source_cycle_slug: str
    weeks: tuple[SplitWeekResult, ...]
    next_cycle: SplitWeekResult | None


def week_slug(start: str, end: str) -> str:
    return f"cycle-{start}-{end}"


def _parse_range(start: str, end: str) -> tuple[str, str]:
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    if start_date > end_date:
        raise ValueError(f"周次范围起始晚于结束：{start} ~ {end}")
    return start_date.isoformat(), end_date.isoformat()


def _validate_ranges(week_ranges: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
    if not week_ranges:
        raise ValueError("week_ranges 不能为空")
    parsed = [_parse_range(start, end) for start, end in week_ranges]
    for (previous_start, previous_end), (start, end) in zip(parsed, parsed[1:]):
        if start <= previous_end:
            raise ValueError(
                f"周次范围重叠或乱序：{previous_start}~{previous_end} 与 {start}~{end}"
            )
    return parsed


def _count_articles_in_range(
    connection: sqlite3.Connection, start: str, end: str
) -> list[tuple[int, str | None]]:
    return [
        (int(row["id"]), row["published_at"])
        for row in connection.execute(
            """
            SELECT id, published_at FROM source_articles
            WHERE substr(published_at, 1, 10) BETWEEN ? AND ?
            ORDER BY id
            """,
            (start, end),
        ).fetchall()
    ]


def _article_count_in_range(database: Path, start: str, end: str) -> int:
    connection = connect_database(database, readonly=True)
    try:
        return len(_count_articles_in_range(connection, start, end))
    finally:
        connection.close()


def _cycle_counts(database_path: Path, cycle_id: int) -> tuple[int, int]:
    connection = connect_database(database_path, readonly=True)
    try:
        cluster_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM topic_clusters WHERE cycle_id = ?", (cycle_id,)
            ).fetchone()[0]
        )
        signal_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM industry_signals s
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                WHERE tc.cycle_id = ? AND s.status = 'active'
                """,
                (cycle_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    return cluster_count, signal_count


def split_cycle_into_weeks(
    database_path: Path,
    *,
    cycle_slug: str | None = None,
    week_ranges: tuple[tuple[str, str], ...] = DEFAULT_WEEK_RANGES,
    next_cycle_range: tuple[str, str] | None = DEFAULT_NEXT_CYCLE_RANGE,
    max_signals_per_brief: int = 8,
    actor: str = "operator",
) -> CycleSplitResult:
    """Close one open cycle and rebuild its articles as weekly cycles plus a blank follow-up."""
    database = Path(database_path).resolve()
    initialize_schema(database)
    weeks = _validate_ranges(week_ranges)
    next_range = _parse_range(*next_cycle_range) if next_cycle_range else None
    planned_slugs = [week_slug(start, end) for start, end in weeks]
    if next_range:
        planned_slugs.append(week_slug(next_range[0], next_range[1]))

    connection = connect_database(database, readonly=True)
    try:
        source = get_monitoring_cycle(connection, slug=cycle_slug)
        if source is None:
            raise MonitoringCycleError("没有可拆分的监测期次")
        if source["status"] != "open":
            raise MonitoringCycleError("只有开放中的期次才能拆分；该期次已关闭")
        open_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM monitoring_cycles WHERE status = 'open'"
            ).fetchone()[0]
        )
        if open_count != 1:
            raise MonitoringCycleError(f"预期恰好一个开放期次，实际 {open_count} 个")
        placeholders = ",".join("?" for _ in planned_slugs)
        existing = {
            str(row["slug"])
            for row in connection.execute(
                f"SELECT slug FROM monitoring_cycles WHERE slug IN ({placeholders})",
                planned_slugs,
            )
        }
        if existing:
            raise MonitoringCycleError(
                "目标周次标识已存在，拆分中止：" + "、".join(sorted(existing))
            )
    finally:
        connection.close()

    source_slug = str(source["slug"])
    close_monitoring_cycle(
        database, int(source["id"]), reason="按周拆分", actor=actor
    )

    results: list[SplitWeekResult] = []
    for index, (start, end) in enumerate(weeks):
        slug = week_slug(start, end)
        label = f"监测周次 {start} ~ {end}"
        created = create_monitoring_cycle(
            database,
            slug=slug,
            label=label,
            start_date=start,
            end_date=end,
            data_cutoff=end,
            actor=actor,
        )
        connection = connect_database(database)
        try:
            week_row = get_monitoring_cycle(connection, cycle_id=created.id)
            assert week_row is not None
            for article_id, published_at in _count_articles_in_range(
                connection, start, end
            ):
                attach_article_to_cycle(connection, week_row, article_id, published_at)
        finally:
            connection.close()

        article_count = _article_count_in_range(database, start, end)
        if article_count:
            build_demo_topics(database, cycle_slug=slug)
        cluster_count, signal_count = _cycle_counts(database, created.id)
        brief_version: int | None = None
        if signal_count:
            brief = export_weekly_signal_brief(
                database,
                cycle_slug=slug,
                max_signals=max_signals_per_brief,
                actor=actor,
            )
            brief_version = brief.version
        close_monitoring_cycle(
            database, created.id, reason="按周拆分", actor=actor
        )
        results.append(
            SplitWeekResult(
                slug=slug,
                label=label,
                start_date=start,
                end_date=end,
                article_count=article_count,
                cluster_count=cluster_count,
                signal_count=signal_count,
                brief_version=brief_version,
                status="closed",
            )
        )

    next_result: SplitWeekResult | None = None
    if next_range:
        start, end = next_range
        slug = week_slug(start, end)
        label = f"监测周次 {start} ~ {end}"
        created = create_monitoring_cycle(
            database,
            slug=slug,
            label=label,
            start_date=start,
            end_date=end,
            data_cutoff=end,
            actor=actor,
        )
        connection = connect_database(database)
        try:
            week_row = get_monitoring_cycle(connection, cycle_id=created.id)
            assert week_row is not None
            for article_id, published_at in _count_articles_in_range(
                connection, start, end
            ):
                attach_article_to_cycle(connection, week_row, article_id, published_at)
        finally:
            connection.close()
        article_count = _article_count_in_range(database, start, end)
        if article_count:
            build_demo_topics(database, cycle_slug=slug)
        cluster_count, signal_count = _cycle_counts(database, created.id)
        brief_version: int | None = None
        if signal_count:
            brief = export_weekly_signal_brief(
                database,
                cycle_slug=slug,
                max_signals=max_signals_per_brief,
                actor=actor,
            )
            brief_version = brief.version
        next_result = SplitWeekResult(
            slug=slug,
            label=label,
            start_date=start,
            end_date=end,
            article_count=article_count,
            cluster_count=cluster_count,
            signal_count=signal_count,
            brief_version=brief_version,
            status="open",
        )

    return CycleSplitResult(
        source_cycle_slug=source_slug,
        weeks=tuple(results),
        next_cycle=next_result,
    )
