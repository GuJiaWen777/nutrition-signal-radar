from pathlib import Path

import pytest

from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.cycle_service import close_monitoring_cycle
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.demo_pipeline import build_demo_topics
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.topic_workflow import TopicWorkflowError, set_signal_priority
from nutrition_signal_radar.weekly_service import export_weekly_signal_brief

_TITLE_A = "益生菌监管案例通报发布"
_TITLE_B = "体重管理新规征求意见稿发布"


def _article(external_id: str, account: str, title: str) -> NormalizedArticle:
    return NormalizedArticle(
        external_id=external_id,
        account_name=account,
        title=title,
        published_at="2026-03-20T09:30:00+08:00",
        url=f"https://example.com/{external_id}",
        content_snapshot=f"正文内容-{external_id}",
        content_hash=f"hash-{external_id}",
        read_count=100,
    )


def _database(tmp_path: Path) -> Path:
    database_path = tmp_path / "var" / "radar.sqlite3"
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="redfox",
            source_path="https://redfox.hk/example",
            source_fingerprint="signal-priority-fixture",
            source_rows=4,
            articles=(
                _article("p-a1", "公众号甲", _TITLE_A),
                _article("p-a2", "公众号乙", _TITLE_A),
                _article("p-b1", "公众号甲", _TITLE_B),
                _article("p-b2", "公众号乙", _TITLE_B),
            ),
        ),
    )
    build_demo_topics(database_path)
    return database_path


def _signal_ids(database_path: Path) -> dict[str, int]:
    connection = connect_database(database_path, readonly=True)
    try:
        return {
            str(row["signal_title"]): int(row["id"])
            for row in connection.execute(
                "SELECT id, signal_title FROM industry_signals"
            )
        }
    finally:
        connection.close()


def test_signal_priority_reorders_brief_and_survives_rebuild(
    tmp_path: Path,
) -> None:
    database_path = _database(tmp_path)
    ids = _signal_ids(database_path)
    assert _TITLE_A in ids and _TITLE_B in ids

    set_signal_priority(database_path, ids[_TITLE_B], 0)
    set_signal_priority(database_path, ids[_TITLE_A], 1)

    build_demo_topics(database_path)  # rule rebuild must preserve human order
    connection = connect_database(database_path, readonly=True)
    try:
        priorities = {
            row["id"]: row["priority"]
            for row in connection.execute("SELECT id, priority FROM industry_signals")
        }
    finally:
        connection.close()
    assert priorities[ids[_TITLE_B]] == 0
    assert priorities[ids[_TITLE_A]] == 1

    brief = export_weekly_signal_brief(database_path, max_signals=8)
    markdown = brief.markdown_path.read_text(encoding="utf-8")
    assert markdown.index(_TITLE_B) < markdown.index(_TITLE_A)

    event = connection_check(database_path)
    assert event == 2


def test_signal_priority_none_restores_rule_order(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    ids = _signal_ids(database_path)
    set_signal_priority(database_path, ids[_TITLE_B], 0)
    set_signal_priority(database_path, ids[_TITLE_B], None)

    connection = connect_database(database_path, readonly=True)
    try:
        row = connection.execute(
            "SELECT priority FROM industry_signals WHERE id = ?", (ids[_TITLE_B],)
        ).fetchone()
        assert row["priority"] is None
    finally:
        connection.close()


def test_signal_priority_stops_at_closed_cycle(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    ids = _signal_ids(database_path)
    connection = connect_database(database_path, readonly=True)
    try:
        cycle = connection.execute(
            """
            SELECT mc.id FROM monitoring_cycles mc
            JOIN topic_clusters tc ON tc.cycle_id = mc.id
            JOIN industry_signals s ON s.cluster_id = tc.id
            WHERE s.id = ?
            """,
            (ids[_TITLE_A],),
        ).fetchone()
    finally:
        connection.close()
    close_monitoring_cycle(database_path, int(cycle["id"]))

    with pytest.raises(TopicWorkflowError, match="已关闭"):
        set_signal_priority(database_path, ids[_TITLE_A], 0)


def test_signal_priority_rejects_negative_values(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    ids = _signal_ids(database_path)
    with pytest.raises(ValueError, match="priority"):
        set_signal_priority(database_path, ids[_TITLE_A], -1)


def connection_check(database_path: Path) -> int:
    connection = connect_database(database_path, readonly=True)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM monitoring_cycle_events "
                "WHERE event_type = 'signal_priority_set'"
            ).fetchone()[0]
        )
    finally:
        connection.close()
