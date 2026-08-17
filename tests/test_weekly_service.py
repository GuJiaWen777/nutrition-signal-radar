import json
from pathlib import Path

from nutrition_signal_radar.agent_gateway import AgentRunResult
from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.demo_pipeline import build_demo_topics
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.weekly_service import (
    WeeklyArticleError,
    confirm_weekly_article,
    export_weekly_signal_brief,
    generate_weekly_article,
    list_weekly_articles,
    list_weekly_signal_briefs,
    read_weekly_article,
    read_weekly_signal_brief,
    reopen_weekly_article,
)


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


def test_weekly_signal_briefs_can_be_listed_and_read(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="redfox",
            source_path="https://redfox.hk/example",
            source_fingerprint="weekly-list-fixture",
            source_rows=2,
            articles=(
                _article("a1", "公众号甲", "益生菌监管政策发布", "https://example.com/a1"),
                _article("a2", "公众号乙", "益生菌监管政策正式发布", "https://example.com/a2"),
            ),
        ),
    )
    build_demo_topics(database_path)
    exported = export_weekly_signal_brief(database_path, max_signals=5)

    briefs = list_weekly_signal_briefs(database_path, cycle_id=exported.cycle_id)
    assert len(briefs) == 1
    assert briefs[0]["version"] == 1
    assert briefs[0]["exists"] is True
    assert briefs[0]["cycle_slug"] == exported.cycle_slug

    read = read_weekly_signal_brief(
        database_path, cycle_id=exported.cycle_id, version=1
    )
    assert read is not None
    assert "本周信号快报" in read["markdown"]
    assert read_weekly_signal_brief(
        database_path, cycle_id=exported.cycle_id, version=99
    ) is None


class FakeArticleRunner:
    def __init__(self, payload: dict | None = None, error: Exception | None = None) -> None:
        self.payload = payload or _article_payload()
        self.error = error
        self.calls: list[object] = []

    def run(self, request: object) -> AgentRunResult:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return AgentRunResult(
            status="completed",
            final_text=json.dumps(self.payload, ensure_ascii=False),
            tool_calls=(),
            event_lines=(),
            exit_code=0,
            stderr="",
        )


def _article_payload() -> dict[str, object]:
    return {
        "title": "营养保健行业周报|监管趋严、原料创新",
        "summary": "本期聚焦监管整治与微生物蛋白产业化。",
        "article_body": (
            "## 一、监管趋严\n\n"
            "7月7日，市场监管总局通报上半年不正当竞争案件查办情况。\n\n"
            "## 二、原料创新\n\n"
            "7月9日，安琪酵母表示酵母蛋白供不应求，新产线正在建设。"
        ),
        "editor_notes": "企业产量数据尚未回到一手公告核验，已从正文移除。",
        "sources": [
            {
                "claim": "市场监管总局公布上半年案件数据",
                "title": "监管总局发布会实录",
                "url": "https://example.com/regulator",
                "publisher": "市场监管总局",
                "source_type": "regulator",
            }
        ],
    }


def _demo_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "var" / "radar.sqlite3"
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="redfox",
            source_path="https://redfox.hk/example",
            source_fingerprint="weekly-article-fixture",
            source_rows=2,
            articles=(
                _article("a1", "公众号甲", "益生菌监管政策发布", "https://example.com/a1"),
                _article("a2", "公众号乙", "益生菌监管政策正式发布", "https://example.com/a2"),
            ),
        ),
    )
    build_demo_topics(database_path)
    return database_path


def test_weekly_article_is_generated_as_publishable_markdown(tmp_path: Path) -> None:
    database_path = _demo_database(tmp_path)
    runner = FakeArticleRunner()

    result = generate_weekly_article(database_path, runner, max_signals=5)

    assert result.version == 1
    assert result.title.startswith("营养保健行业周报")
    assert result.markdown_path.is_file()
    markdown = result.markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# 营养保健行业周报")
    assert "待补证" not in markdown
    assert "公众号发现线索" not in markdown
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["brief_kind"] == "weekly_industry_article"
    assert manifest["status"] == "draft_for_human_review"
    assert manifest["editor_notes"]
    assert manifest["sources"][0]["url"] == "https://example.com/regulator"

    connection = connect_database(database_path, readonly=True)
    try:
        row = connection.execute(
            "SELECT status, confirmed_at FROM weekly_articles WHERE version = 1"
        ).fetchone()
        assert row["status"] == "draft_for_human_review"
        assert row["confirmed_at"] is None
        assert connection.execute(
            "SELECT COUNT(*) FROM monitoring_cycle_events "
            "WHERE event_type = 'weekly_article_generated'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_weekly_article_and_brief_have_independent_versioning(tmp_path: Path) -> None:
    database_path = _demo_database(tmp_path)
    brief = export_weekly_signal_brief(database_path, max_signals=5)
    article = generate_weekly_article(database_path, FakeArticleRunner(), max_signals=5)

    assert brief.version == 1
    assert article.version == 1
    articles = list_weekly_articles(database_path, cycle_id=article.cycle_id)
    assert len(articles) == 1
    assert articles[0]["title"].startswith("营养保健行业周报")


def test_weekly_article_rejects_internal_markers(tmp_path: Path) -> None:
    database_path = _demo_database(tmp_path)
    payload = _article_payload()
    payload["article_body"] += "\n\n该数据仍需待补证。"
    runner = FakeArticleRunner(payload=payload)

    try:
        generate_weekly_article(database_path, runner, max_signals=5)
    except WeeklyArticleError as exc:
        assert "待补证" in str(exc)
    else:
        raise AssertionError("expected WeeklyArticleError")

    connection = connect_database(database_path, readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM weekly_articles").fetchone()[0] == 0
    finally:
        connection.close()


def test_weekly_article_can_be_confirmed_and_reopened(tmp_path: Path) -> None:
    database_path = _demo_database(tmp_path)
    article = generate_weekly_article(database_path, FakeArticleRunner(), max_signals=5)

    confirmed = confirm_weekly_article(
        database_path, cycle_id=article.cycle_id, version=article.version
    )
    assert confirmed.status == "final"
    read = read_weekly_article(
        database_path, cycle_id=article.cycle_id, version=article.version
    )
    assert read is not None
    assert read["status"] == "final"
    assert read["confirmed_at"] is not None
    confirmation = json.loads(
        (Path(str(read["directory_absolute"])) / "confirmation.json").read_text(
            encoding="utf-8"
        )
    )
    assert confirmation["status"] == "final"

    reopened = reopen_weekly_article(
        database_path, cycle_id=article.cycle_id, version=article.version
    )
    assert reopened.status == "draft_for_human_review"
    read = read_weekly_article(
        database_path, cycle_id=article.cycle_id, version=article.version
    )
    assert read["status"] == "draft_for_human_review"
    assert read["confirmed_at"] is None

    connection = connect_database(database_path, readonly=True)
    try:
        events = {
            str(row[0])
            for row in connection.execute(
                "SELECT event_type FROM monitoring_cycle_events "
                "WHERE event_type LIKE 'weekly_article_%'"
            )
        }
        assert {"weekly_article_generated", "weekly_article_confirmed", "weekly_article_reopened"} <= events
    finally:
        connection.close()
