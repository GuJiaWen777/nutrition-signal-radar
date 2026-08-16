import json
from pathlib import Path

from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.demo_pipeline import build_demo_topics
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.weekly_service import export_weekly_signal_brief


def _article(external_id: str, account: str, title: str, url: str) -> NormalizedArticle:
    return NormalizedArticle(
        external_id=external_id,
        account_name=account,
        title=title,
        published_at="2026-08-14T09:30:00+08:00",
        url=url,
        content_snapshot=f"正文-{external_id}",
        content_hash=external_id,
        read_count=100,
    )


def test_weekly_signal_brief_is_versioned_and_does_not_start_research(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="redfox",
            source_path="https://redfox.hk/example",
            source_fingerprint="weekly-fixture",
            source_rows=2,
            articles=(
                _article("a1", "公众号甲", "益生菌监管政策发布", "https://example.com/a1"),
                _article("a2", "公众号乙", "益生菌监管政策正式发布", "https://example.com/a2"),
            ),
        ),
    )
    build_demo_topics(database_path)

    first = export_weekly_signal_brief(database_path, max_signals=5)
    second = export_weekly_signal_brief(database_path, max_signals=5)
    markdown = first.markdown_path.read_text(encoding="utf-8")
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))

    assert first.version == 1
    assert second.version == 2
    assert first.directory.name == "v001"
    assert second.directory.name == "v002"
    assert first.markdown_path.is_file()
    assert "本周信号快报" in markdown
    assert "横向扫描" in markdown
    assert "不会改变任何状态" in markdown
    assert "不触发任何微信公众号写入或发布动作" in markdown
    assert manifest["status"] == "draft_for_human_review"
    assert manifest["brief_kind"] == "weekly_industry_signal_brief"

    connection = connect_database(database_path, readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM weekly_signal_briefs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM research_tasks").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM monitoring_cycle_events WHERE event_type = 'weekly_signal_brief_exported'"
        ).fetchone()[0] == 2
    finally:
        connection.close()
