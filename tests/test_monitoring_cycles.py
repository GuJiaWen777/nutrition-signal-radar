import json
from pathlib import Path

import pytest

from nutrition_signal_radar.archive_service import export_cycle_markdown_archive
from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.cycle_service import (
    MonitoringCycleError,
    close_monitoring_cycle,
    create_monitoring_cycle,
    list_monitoring_cycles,
)
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.demo_pipeline import build_demo_topics
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.topic_workflow import TopicTransitionError, transition_candidate


def _article(external_id: str, account: str, published_at: str) -> NormalizedArticle:
    return NormalizedArticle(
        external_id=external_id,
        account_name=account,
        title="同一项功能食品监管变化",
        published_at=published_at,
        url=f"https://example.com/{external_id}",
        content_snapshot="同一事件的规则、行业影响和待核实信息。",
        content_hash="shared-event-copy",
        read_count=100,
    )


def _import(database_path: Path, *articles: NormalizedArticle, fingerprint: str) -> None:
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="xlsx",
            source_path="D:/source.xlsx",
            source_fingerprint=fingerprint,
            source_rows=len(articles),
            articles=tuple(articles),
        ),
    )


def _cycle_with_candidate(tmp_path: Path) -> tuple[Path, int, int]:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    cycle = create_monitoring_cycle(
        database_path,
        slug="2026-w10",
        label="2026 年第 10 周",
        start_date="2026-03-02",
        end_date="2026-03-08",
    )
    _import(
        database_path,
        _article("inside-1", "公众号甲", "2026-03-03T09:00:00+08:00"),
        _article("inside-2", "公众号乙", "2026-03-04T09:00:00+08:00"),
        fingerprint="cycle-1",
    )
    build_demo_topics(database_path, cycle_slug=cycle.slug)
    connection = connect_database(database_path, readonly=True)
    try:
        candidate_id = int(connection.execute("SELECT id FROM topic_candidates").fetchone()[0])
    finally:
        connection.close()
    return database_path, cycle.id, candidate_id


def test_import_roles_and_demo_build_are_scoped_to_open_cycle(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    cycle = create_monitoring_cycle(
        database_path,
        slug="2026-w10",
        label="2026 年第 10 周",
        start_date="2026-03-02",
        end_date="2026-03-08",
    )

    result = import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="xlsx",
            source_path="D:/source.xlsx",
            source_fingerprint="mixed-dates",
            source_rows=4,
            articles=(
                _article("before", "公众号甲", "2026-03-01T09:00:00+08:00"),
                _article("inside-1", "公众号乙", "2026-03-03T09:00:00+08:00"),
                _article("inside-2", "公众号丙", "2026-03-04T09:00:00+08:00"),
                _article("after", "公众号丁", "2026-03-09T09:00:00+08:00"),
            ),
        ),
    )
    built = build_demo_topics(database_path, cycle_slug=cycle.slug)

    assert result.cycle_id == cycle.id
    assert result.in_period_articles == 2
    assert result.context_articles == 2
    assert built.cycle_id == cycle.id
    assert built.source_articles == 2
    assert built.candidates == 1
    connection = connect_database(database_path, readonly=True)
    try:
        roles = dict(
            connection.execute(
                "SELECT role, COUNT(*) FROM monitoring_cycle_articles GROUP BY role"
            ).fetchall()
        )
        assert roles == {"context": 2, "in_period": 2}
        cluster_cycle_ids = {
            int(row[0]) for row in connection.execute("SELECT cycle_id FROM topic_clusters")
        }
        assert cluster_cycle_ids == {cycle.id}
    finally:
        connection.close()


def test_closed_cycle_is_read_only_and_next_cycle_stays_isolated(tmp_path: Path) -> None:
    database_path, first_cycle_id, candidate_id = _cycle_with_candidate(tmp_path)
    close_monitoring_cycle(database_path, first_cycle_id, reason="本期完成")

    with pytest.raises(TopicTransitionError, match="已关闭"):
        transition_candidate(database_path, candidate_id, "pending_decision")
    with pytest.raises(MonitoringCycleError, match="请先新建下一期"):
        _import(
            database_path,
            _article("blocked", "公众号丙", "2026-03-10T09:00:00+08:00"),
            fingerprint="blocked-import",
        )

    second = create_monitoring_cycle(
        database_path,
        slug="2026-w11",
        label="2026 年第 11 周",
        start_date="2026-03-09",
        end_date="2026-03-15",
    )
    _import(
        database_path,
        _article("next-1", "公众号甲", "2026-03-10T09:00:00+08:00"),
        _article("next-2", "公众号乙", "2026-03-11T09:00:00+08:00"),
        fingerprint="cycle-2",
    )
    build_demo_topics(database_path, cycle_slug=second.slug)

    cycles = list_monitoring_cycles(database_path)
    by_slug = {str(cycle["slug"]): cycle for cycle in cycles}
    assert by_slug["2026-w10"]["status"] == "closed"
    assert by_slug["2026-w10"]["candidate_count"] == 1
    assert by_slug["2026-w11"]["status"] == "open"
    assert by_slug["2026-w11"]["candidate_count"] == 1
    connection = connect_database(database_path, readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM monitoring_cycles").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM topic_candidates").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(DISTINCT cycle_id) FROM topic_clusters"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_markdown_archive_is_versioned_and_never_overwrites(tmp_path: Path) -> None:
    database_path, cycle_id, candidate_id = _cycle_with_candidate(tmp_path)
    transition_candidate(database_path, candidate_id, "pending_decision")
    transition_candidate(database_path, candidate_id, "package_drafting")

    first = export_cycle_markdown_archive(database_path, cycle_id)
    second = export_cycle_markdown_archive(database_path, cycle_id)

    assert first.version == 1
    assert second.version == 2
    assert first.directory.name == "v001"
    assert second.directory.name == "v002"
    assert first.directory.is_dir()
    assert second.directory.is_dir()
    assert (first.directory / "topic-selection.md").is_file()
    assert not (first.directory / "articles").exists()
    package_files = list((first.directory / "topic-packages").glob("*.md"))
    assert len(package_files) == 1
    assert "待补证" in package_files[0].read_text(encoding="utf-8")
    selection_archive = (first.directory / "topic-selection.md").read_text(
        encoding="utf-8"
    )
    assert "只记录选题阶段" in selection_archive
    assert "不构成行业周报、深度研究稿或最终文章" in selection_archive
    assert "不会触发研究或发布" in selection_archive
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["export"]["version"] == 1
    assert manifest["cycle"]["slug"] == "2026-w10"
    connection = connect_database(database_path, readonly=True)
    try:
        exports = connection.execute(
            "SELECT version, relative_path FROM cycle_exports ORDER BY version"
        ).fetchall()
        assert [row["version"] for row in exports] == [1, 2]
        assert exports[0]["relative_path"].endswith("2026-w10/v001")
        assert exports[1]["relative_path"].endswith("2026-w10/v002")
    finally:
        connection.close()
