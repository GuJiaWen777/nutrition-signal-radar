from pathlib import Path

import pytest

from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.cycle_service import (
    MonitoringCycleError,
    list_monitoring_cycles,
)
from nutrition_signal_radar.cycle_split import (
    DEFAULT_NEXT_CYCLE_RANGE,
    DEFAULT_WEEK_RANGES,
    split_cycle_into_weeks,
    week_slug,
)
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.weekly_service import list_weekly_signal_briefs

_DATES = (
    "2026-07-17",
    "2026-07-18",
    "2026-07-26",
    "2026-07-27",
    "2026-08-02",
    "2026-08-03",
    "2026-08-09",
    "2026-08-10",
    "2026-08-16",
    "2026-08-17",
    "2026-08-18",
)


_TITLES = (
    "益生菌监管案例通报发布",
    "益生菌市场动态观察",
    "益生菌菌株临床试验数据发布",
    "益生菌新品研发进展公布",
    "益生菌行业白皮书正式发布",
    "益生菌功能研究最新成果",
    "益生菌品类增长数据揭晓",
    "益生菌产品抽检结果通报",
    "益生菌原料价格走势分析",
    "益生菌品牌渠道布局观察",
    "益生菌消费趋势报告发布",
)


def _article(day: str, index: int) -> NormalizedArticle:
    return NormalizedArticle(
        external_id=f"split-{index}",
        account_name=f"公众号{index % 3}",
        title=_TITLES[index],
        published_at=f"{day}T09:30:00+08:00",
        url=f"https://example.com/split-{index}",
        content_snapshot=f"益生菌行业观察正文第{index}篇",
        content_hash=f"split-hash-{index}",
        read_count=100 + index,
    )


def _build_fixture(database_path: Path) -> None:
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="xlsx",
            source_path="D:/source.xlsx",
            source_fingerprint="split-fixture",
            source_rows=len(_DATES),
            articles=tuple(_article(day, index) for index, day in enumerate(_DATES)),
        ),
    )


def _in_period_counts(database_path: Path) -> dict[str, int]:
    connection = connect_database(database_path, readonly=True)
    try:
        return {
            str(row["slug"]): int(row["count"])
            for row in connection.execute(
                """
                SELECT mc.slug, COUNT(mca.article_id) AS count
                FROM monitoring_cycles mc
                LEFT JOIN monitoring_cycle_articles mca
                    ON mca.cycle_id = mc.id AND mca.role = 'in_period'
                GROUP BY mc.id
                """
            )
        }
    finally:
        connection.close()


def test_split_cycle_into_weeks_rebuilds_each_week(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    _build_fixture(database_path)

    result = split_cycle_into_weeks(database_path, cycle_slug="demo-2026-07")

    assert result.source_cycle_slug == "demo-2026-07"
    assert [week.slug for week in result.weeks] == [
        week_slug(start, end) for start, end in DEFAULT_WEEK_RANGES
    ]
    assert result.next_cycle is not None
    assert result.next_cycle.slug == week_slug(*DEFAULT_NEXT_CYCLE_RANGE)

    cycles = {cycle["slug"]: cycle for cycle in list_monitoring_cycles(database_path)}
    assert cycles["demo-2026-07"]["status"] == "closed"
    for week in result.weeks:
        assert cycles[week.slug]["status"] == "closed"
        assert week.status == "closed"
        assert week.brief_version == 1
    assert cycles[result.next_cycle.slug]["status"] == "open"

    counts = _in_period_counts(database_path)
    assert counts == {
        "demo-2026-07": 11,
        week_slug(*DEFAULT_WEEK_RANGES[0]): 3,
        week_slug(*DEFAULT_WEEK_RANGES[1]): 2,
        week_slug(*DEFAULT_WEEK_RANGES[2]): 2,
        week_slug(*DEFAULT_WEEK_RANGES[3]): 2,
        week_slug(*DEFAULT_NEXT_CYCLE_RANGE): 2,
    }

    for slug in [week.slug for week in result.weeks] + [result.next_cycle.slug]:
        briefs = list_weekly_signal_briefs(database_path, cycle_slug=slug)
        assert [brief["version"] for brief in briefs] == [1]
        assert briefs[0]["exists"] is True

    connection = connect_database(database_path, readonly=True)
    try:
        event_types = [
            row["event_type"]
            for row in connection.execute(
                "SELECT event_type FROM monitoring_cycle_events"
            )
        ]
        assert event_types.count("cycle_closed") == 5
        assert event_types.count("cycle_created") == 6
        assert event_types.count("weekly_signal_brief_exported") == 5
        open_slugs = [
            row["slug"]
            for row in connection.execute(
                "SELECT slug FROM monitoring_cycles WHERE status = 'open'"
            )
        ]
        assert open_slugs == [result.next_cycle.slug]
    finally:
        connection.close()


def test_split_refuses_closed_cycle(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    _build_fixture(database_path)
    split_cycle_into_weeks(database_path, cycle_slug="demo-2026-07")

    with pytest.raises(MonitoringCycleError, match="已关闭"):
        split_cycle_into_weeks(database_path, cycle_slug="demo-2026-07")


def test_split_rejects_overlapping_ranges_before_writing(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    _build_fixture(database_path)

    with pytest.raises(ValueError, match="重叠"):
        split_cycle_into_weeks(
            database_path,
            cycle_slug="demo-2026-07",
            week_ranges=(
                ("2026-07-17", "2026-07-26"),
                ("2026-07-26", "2026-08-02"),
            ),
        )

    cycles = list_monitoring_cycles(database_path)
    assert [cycle["slug"] for cycle in cycles] == ["demo-2026-07"]
    assert cycles[0]["status"] == "open"


def test_split_without_articles_in_a_week_skips_build(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    _build_fixture(database_path)

    result = split_cycle_into_weeks(
        database_path,
        cycle_slug="demo-2026-07",
        week_ranges=(("2026-07-17", "2026-07-26"), ("2026-09-01", "2026-09-07")),
        next_cycle_range=None,
    )

    assert len(result.weeks) == 2
    assert result.weeks[1].article_count == 0
    assert result.weeks[1].cluster_count == 0
    assert result.weeks[1].brief_version is None
    assert result.next_cycle is None
