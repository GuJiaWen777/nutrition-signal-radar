import hashlib
import json
from pathlib import Path

import pytest

from nutrition_signal_radar.agent_gateway import AgentRunResult, PiAgentRequest
from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.cycle_service import MonitoringCycleError, close_monitoring_cycle
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.demo_pipeline import build_demo_topics
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.weekly_profiles import (
    DECISION_MAKER_PROFILE,
    DEEP_READER_PROFILE,
)
from nutrition_signal_radar.weekly_service import (
    WeeklyArticleError,
    build_weekly_article_prompt,
    confirm_weekly_article,
    delete_weekly_article,
    delete_weekly_signal_brief,
    edit_weekly_article,
    edit_weekly_signal_brief,
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
    assert "使用边界" not in markdown
    assert first.markdown_path.is_file()
    assert "本周信号快报" in markdown
    assert "横向扫描" in markdown
    constraints = manifest["constraints"]
    assert any("不触发任何微信公众号写入或发布动作" in item for item in constraints)
    assert any("不创建研究任务" in item for item in constraints)
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


class _SequenceArticleRunner:
    """Return one payload per call, repeating the last for extra calls."""

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.calls: list[object] = []

    def run(self, request: object) -> AgentRunResult:
        self.calls.append(request)
        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        return AgentRunResult(
            status="completed",
            final_text=json.dumps(payload, ensure_ascii=False),
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


def test_weekly_article_generates_on_closed_cycle(tmp_path: Path) -> None:
    database_path = _demo_database(tmp_path)
    connection = connect_database(database_path, readonly=True)
    try:
        cycle = connection.execute("SELECT * FROM monitoring_cycles").fetchone()
    finally:
        connection.close()
    close_monitoring_cycle(database_path, int(cycle["id"]))

    result = generate_weekly_article(database_path, FakeArticleRunner(), max_signals=5)

    assert result.version == 1
    assert result.markdown_path.is_file()
    connection = connect_database(database_path, readonly=True)
    try:
        row = connection.execute(
            "SELECT status FROM weekly_articles WHERE version = 1"
        ).fetchone()
        assert row["status"] == "draft_for_human_review"
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


def test_weekly_article_markdown_appends_sources_section(tmp_path: Path) -> None:
    database_path = _demo_database(tmp_path)
    article = generate_weekly_article(database_path, FakeArticleRunner(), max_signals=5)

    markdown = article.markdown_path.read_text(encoding="utf-8")

    assert "## 来源" in markdown
    assert (
        "1. [监管总局发布会实录（市场监管总局）](https://example.com/regulator)"
        in markdown
    )
    assert "待补证" not in markdown


def test_weekly_article_generation_softly_avoids_cycle_words_in_body(
    tmp_path: Path,
) -> None:
    database_path = _demo_database(tmp_path)
    payload = _article_payload()
    payload["article_body"] += "\n\n本期监管动态如上，期内共查处多起案件。"
    runner = FakeArticleRunner(payload=payload)

    result = generate_weekly_article(database_path, runner, max_signals=5)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert result.version == 1
    assert "本期监管动态" in result.markdown_path.read_text(encoding="utf-8")
    assert any(
        "内部口径词" in issue
        for issue in manifest["quality_checks"]["remaining_issues"]
    )


def test_weekly_article_edit_accepts_cycle_words_in_body(
    tmp_path: Path,
) -> None:
    database_path = _demo_database(tmp_path)
    article = generate_weekly_article(database_path, FakeArticleRunner(), max_signals=5)
    edited_markdown = "# 营养保健行业周报|监管趋严\n\n本期监管动态如上，人工修订。\n"

    result = edit_weekly_article(
        database_path,
        cycle_id=article.cycle_id,
        version=article.version,
        markdown=edited_markdown,
    )

    assert result["version"] == article.version
    read = read_weekly_article(
        database_path, cycle_id=article.cycle_id, version=article.version
    )
    assert read is not None
    assert "本期监管动态" in read["markdown"]


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


def test_weekly_article_rejects_draft_word_in_body_and_title(tmp_path: Path) -> None:
    database_path = _demo_database(tmp_path)
    payload = _article_payload()
    payload["article_body"] += "\n\n本段为初稿草稿，正式发布前请删除。"
    runner = FakeArticleRunner(payload=payload)

    try:
        generate_weekly_article(database_path, runner, max_signals=5)
    except WeeklyArticleError as exc:
        assert "草稿" in str(exc)
    else:
        raise AssertionError("expected WeeklyArticleError")

    payload = _article_payload()
    payload["title"] = "营养保健行业周报|监管趋严、原料创新（草稿）"
    runner = FakeArticleRunner(payload=payload)
    try:
        generate_weekly_article(database_path, runner, max_signals=5)
    except WeeklyArticleError as exc:
        assert "草稿" in str(exc)
    else:
        raise AssertionError("expected WeeklyArticleError")

    connection = connect_database(database_path, readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM weekly_articles").fetchone()[0] == 0
    finally:
        connection.close()


def test_weekly_signal_brief_can_be_edited_in_place(tmp_path: Path) -> None:
    database_path = _demo_database(tmp_path)
    brief = export_weekly_signal_brief(database_path, max_signals=5)
    edited_markdown = "# 编辑后的快报\n\n人工补充的审阅备注。"

    result = edit_weekly_signal_brief(
        database_path,
        cycle_id=brief.cycle_id,
        version=brief.version,
        markdown=edited_markdown,
    )

    assert result["version"] == brief.version
    read = read_weekly_signal_brief(
        database_path, cycle_id=brief.cycle_id, version=brief.version
    )
    assert read is not None
    assert read["markdown"].strip() == edited_markdown.strip()
    manifest_file = Path(read["directory_absolute"]) / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest["edited"]["edited_by"] == "operator"
    assert "markdown_sha256_before" in manifest["edited"]
    connection = connect_database(database_path, readonly=True)
    try:
        stored_sha = connection.execute(
            "SELECT manifest_sha256 FROM weekly_signal_briefs WHERE cycle_id = ? AND version = ?",
            (brief.cycle_id, brief.version),
        ).fetchone()[0]
        edited_event = connection.execute(
            """
            SELECT 1 FROM monitoring_cycle_events
            WHERE event_type = 'weekly_signal_brief_edited'
            """
        ).fetchone()
    finally:
        connection.close()
    assert stored_sha == hashlib.sha256(manifest_file.read_bytes()).hexdigest()
    assert edited_event is not None


def test_weekly_signal_brief_can_be_deleted(tmp_path: Path) -> None:
    database_path = _demo_database(tmp_path)
    brief = export_weekly_signal_brief(database_path, max_signals=5)

    result = delete_weekly_signal_brief(
        database_path, cycle_id=brief.cycle_id, version=brief.version
    )

    assert result["deleted"] is True
    assert (
        read_weekly_signal_brief(
            database_path, cycle_id=brief.cycle_id, version=brief.version
        )
        is None
    )
    assert not brief.directory.exists()
    connection = connect_database(database_path, readonly=True)
    try:
        deleted_event = connection.execute(
            """
            SELECT 1 FROM monitoring_cycle_events
            WHERE event_type = 'weekly_signal_brief_deleted'
            """
        ).fetchone()
    finally:
        connection.close()
    assert deleted_event is not None


def test_weekly_signal_brief_edit_and_delete_stop_at_closed_cycle(
    tmp_path: Path,
) -> None:
    database_path = _demo_database(tmp_path)
    brief = export_weekly_signal_brief(database_path, max_signals=5)
    close_monitoring_cycle(database_path, brief.cycle_id)

    with pytest.raises(MonitoringCycleError, match="已关闭"):
        edit_weekly_signal_brief(
            database_path,
            cycle_id=brief.cycle_id,
            version=brief.version,
            markdown="# 修改",
        )
    with pytest.raises(MonitoringCycleError, match="已关闭"):
        delete_weekly_signal_brief(
            database_path, cycle_id=brief.cycle_id, version=brief.version
        )
    assert brief.directory.is_dir()


def test_weekly_article_can_be_edited_in_place(tmp_path: Path) -> None:
    database_path = _demo_database(tmp_path)
    article = generate_weekly_article(database_path, FakeArticleRunner(), max_signals=5)
    edited_markdown = "# 营养保健行业周报|监管趋严\n\n人工修订后的正文。\n"

    result = edit_weekly_article(
        database_path,
        cycle_id=article.cycle_id,
        version=article.version,
        markdown=edited_markdown,
    )

    assert result["title"] == "营养保健行业周报|监管趋严"
    read = read_weekly_article(
        database_path, cycle_id=article.cycle_id, version=article.version
    )
    assert read is not None
    assert read["title"] == "营养保健行业周报|监管趋严"
    assert read["markdown"].strip() == edited_markdown.strip()
    connection = connect_database(database_path, readonly=True)
    try:
        edited_event = connection.execute(
            """
            SELECT 1 FROM monitoring_cycle_events
            WHERE event_type = 'weekly_article_edited'
            """
        ).fetchone()
    finally:
        connection.close()
    assert edited_event is not None


def test_weekly_article_edit_rejects_internal_markers_and_final_articles(
    tmp_path: Path,
) -> None:
    database_path = _demo_database(tmp_path)
    article = generate_weekly_article(database_path, FakeArticleRunner(), max_signals=5)
    original_markdown = article.markdown_path.read_text(encoding="utf-8")

    with pytest.raises(WeeklyArticleError, match="内部标记"):
        edit_weekly_article(
            database_path,
            cycle_id=article.cycle_id,
            version=article.version,
            markdown="# 标题\n\n这段内容仍需待补证。",
        )
    assert article.markdown_path.read_text(encoding="utf-8") == original_markdown

    confirm_weekly_article(
        database_path, cycle_id=article.cycle_id, version=article.version
    )
    with pytest.raises(WeeklyArticleError, match="回到待编辑"):
        edit_weekly_article(
            database_path,
            cycle_id=article.cycle_id,
            version=article.version,
            markdown="# 标题\n\n人工修订。",
        )


def test_weekly_article_can_be_deleted_and_stops_at_closed_cycle(
    tmp_path: Path,
) -> None:
    database_path = _demo_database(tmp_path)
    article = generate_weekly_article(database_path, FakeArticleRunner(), max_signals=5)

    result = delete_weekly_article(
        database_path, cycle_id=article.cycle_id, version=article.version
    )

    assert result["deleted"] is True
    assert (
        read_weekly_article(
            database_path, cycle_id=article.cycle_id, version=article.version
        )
        is None
    )
    assert not article.directory.exists()
    connection = connect_database(database_path, readonly=True)
    try:
        deleted_event = connection.execute(
            """
            SELECT 1 FROM monitoring_cycle_events
            WHERE event_type = 'weekly_article_deleted'
            """
        ).fetchone()
    finally:
        connection.close()
    assert deleted_event is not None

    second = generate_weekly_article(database_path, FakeArticleRunner(), max_signals=5)
    close_monitoring_cycle(database_path, second.cycle_id)
    with pytest.raises(MonitoringCycleError, match="已关闭"):
        delete_weekly_article(
            database_path, cycle_id=second.cycle_id, version=second.version
        )
    with pytest.raises(MonitoringCycleError, match="已关闭"):
        edit_weekly_article(
            database_path,
            cycle_id=second.cycle_id,
            version=second.version,
            markdown="# 标题\n\n修改。",
        )
    assert second.directory.is_dir()


def test_weekly_article_profile_style_rules_reach_prompt() -> None:
    prompt = build_weekly_article_prompt(
        PiAgentRequest(
            research_question="写一篇周报文章。",
            profile_key=DECISION_MAKER_PROFILE.key,
        )
    )

    assert "垂直领域主笔" in prompt
    assert "首先" in prompt and "总而言之" in prompt
    assert "1800—2800" in prompt
    assert "参考结构示例" in prompt

    deep_prompt = build_weekly_article_prompt(
        PiAgentRequest(
            research_question="写一篇周报文章。",
            profile_key=DEEP_READER_PROFILE.key,
        )
    )
    assert "2600—3600" in deep_prompt
    assert "深度解读" in deep_prompt


def test_weekly_article_quality_gate_revises_once_and_records(
    tmp_path: Path,
) -> None:
    database_path = _demo_database(tmp_path)
    long_body = (
        "## 一、监管动态\n\n7月17日，市场监管总局就保健食品功能目录征求意见。"
        "行业判断与影响。"
        + "市场信号持续升温。" * 200
        + "\n\n## 二、市场与数据\n\n7月22日，总局部署专项整治，直播话术成为检查重点。"
        + "企业应当重审宣称口径。" * 20
    )
    revised_payload = _article_payload()
    revised_payload["article_body"] = long_body
    runner = _SequenceArticleRunner([_article_payload(), revised_payload])

    result = generate_weekly_article(database_path, runner, max_signals=5)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert len(runner.calls) == 2
    assert manifest["quality_checks"]["revision_rounds"] == 1
    assert manifest["quality_checks"]["remaining_issues"] == []
    assert manifest["profile"]["key"] == "industry_lead"
    assert "市场信号持续升温" in result.markdown_path.read_text(encoding="utf-8")


def test_weekly_article_quality_gate_records_residual_issues(
    tmp_path: Path,
) -> None:
    database_path = _demo_database(tmp_path)
    runner = FakeArticleRunner(payload=_article_payload())

    result = generate_weekly_article(database_path, runner, max_signals=5)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert len(runner.calls) == 2
    assert manifest["quality_checks"]["revision_rounds"] == 1
    assert manifest["quality_checks"]["remaining_issues"]
    assert any(
        "字数" in issue for issue in manifest["quality_checks"]["remaining_issues"]
    )
    assert "体检未消除的剩余问题" in manifest["editor_notes"]


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


def _sections_payload() -> dict[str, object]:
    return {
        "title": "营养健康行业信号周报（面向决策者）",
        "editorial_summary": "本期聚焦监管与企业资本动向。",
        "sections": [
            {
                "section_title": "一、监管与政策信号",
                "interpretation": "合规红线持续收紧。",
                "items": [
                    {
                        "headline": "首个保健食品新功能纳入目录",
                        "date": "2026-07-20",
                        "content": "市场监管总局发布公告，将骨关节健康新功能纳入目录。",
                        "verification": {
                            "status": "已核实",
                            "source_type": "监管原文",
                            "urls": ["https://example.com/samr"],
                        },
                    },
                    {
                        "headline": "网络销售专项整治启动",
                        "date": "2026-07-22",
                        "content": "市场监管总局部署网络食品虚假宣传专项整治。相关立案数据待补证。",
                        "verification": {
                            "status": "待补证",
                            "source_type": "官方公告",
                            "urls": ["https://example.com/notice"],
                        },
                    },
                ],
            },
            {
                "section_title": "二、市场与产业数据信号",
                "items": [
                    {
                        "headline": "线上健康消费增长",
                        "date": "2026-07-25",
                        "content": "第三方报告显示线上健康消费双位数增长。",
                    }
                ],
            },
            {
                "section_title": "三、企业与资本信号",
                "items": [
                    {
                        "headline": "企业战略升级",
                        "date": "2026-08-01",
                        "content": "企业披露战略升级与资本动作。",
                    }
                ],
            },
        ],
        "sources": [],
        "methodology": "事实核查规则：尽可能回到一手来源。",
    }


def test_weekly_article_accepts_section_structured_agent_output(tmp_path: Path) -> None:
    database_path = _demo_database(tmp_path)

    result = generate_weekly_article(
        database_path,
        FakeArticleRunner(payload=_sections_payload()),
        max_signals=5,
    )

    assert result.title == "营养保健行业周报|监管与政策、市场与产业数据、企业与资本"
    markdown = result.markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# 营养保健行业周报|")
    assert "待补证" not in markdown
    assert "## 一、监管与政策信号" in markdown
    assert "专项整治" in markdown
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert "已从可发布正文中移除" in manifest["editor_notes"]
    urls = {source["url"] for source in manifest["sources"]}
    assert {"https://example.com/samr", "https://example.com/notice"} <= urls
    assert manifest["sources"][0]["source_type"] == "regulator"


def test_weekly_article_normalizes_alternate_source_shape(tmp_path: Path) -> None:
    database_path = _demo_database(tmp_path)
    payload = _article_payload()
    payload["sources"] = [
        {
            "title": "监管总局公告",
            "url": "https://example.com/regulation",
            "type": "监管原文",
        }
    ]

    result = generate_weekly_article(
        database_path,
        FakeArticleRunner(payload=payload),
        max_signals=5,
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["sources"][0]["source_type"] == "regulator"
    assert manifest["sources"][0]["url"] == "https://example.com/regulation"


def _compact_weekly_payload() -> dict[str, object]:
    return {
        "title": "营养健康产业信号周报（2026年第30周）",
        "executive_summary": "本周监管主线为原料准入精细化。",
        "signals_summary": [
            {"signal": "原料准入精细化", "level": "强", "detail": "益生菌申报规定征求意见。"},
            {"signal": "多肽监管路径不确定", "level": "强", "detail": "FDA 会议建议多组肽列入清单。"},
        ],
        "sections": [
            {
                "section": "政策监管（中国）",
                "items": [
                    {
                        "headline": "益生菌注册备案新规公开征求意见",
                        "date": "2026-07-24",
                        "detail": "市场监管总局就益生菌原料申报规定征求意见，覆盖菌株溯源与活菌检测。",
                        "impact": "中小代工企业的合规门槛将被抬高。",
                        "source_type": "权威媒体+行业媒体转载；总局官网原文待补证",
                        "sources": ["https://example.com/probiotic"],
                    }
                ],
            }
        ],
        "decision_implications": [
            "益生菌企业：8月24日前提交行业意见。",
            "多肽出口企业：审慎使用膳食补充剂定位。相关事实待补证。",
        ],
        "sources": [
            "https://example.com/probiotic",
            "https://example.com/fda",
        ],
    }


def test_weekly_article_handles_compact_agent_schema(tmp_path: Path) -> None:
    database_path = _demo_database(tmp_path)

    result = generate_weekly_article(
        database_path,
        FakeArticleRunner(payload=_compact_weekly_payload()),
        max_signals=5,
    )

    assert result.title == "营养保健行业周报|原料准入精细化、多肽监管路径不确定"
    markdown = result.markdown_path.read_text(encoding="utf-8")
    assert "待补证" not in markdown
    assert "## 政策监管（中国）" in markdown
    assert "益生菌注册备案新规公开征求意见" in markdown
    assert "## 决策启示" in markdown
    assert "审慎使用膳食补充剂定位" in markdown
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    urls = {source["url"] for source in manifest["sources"]}
    assert {"https://example.com/probiotic", "https://example.com/fda"} <= urls
    assert "已从可发布正文中移除" in manifest["editor_notes"]


def test_weekly_article_renders_string_items_and_skips_empty_sections(
    tmp_path: Path,
) -> None:
    database_path = _demo_database(tmp_path)
    payload = {
        "title": "营养保健行业周报|监管趋严、市场动态",
        "executive_summary": "本期聚焦监管与市场。",
        "sections": [
            {
                "section_title": "一、政策动态",
                "items": [{"headline": "新规征求意见", "detail": "正文内容。"}],
            },
            {
                "section_title": "二、对决策者的建议",
                "items": ["建议企业提前盘点合规档案。", "建议关注新功能申报窗口。"],
            },
            {"section_title": "三、空节", "items": []},
        ],
        "sources": [],
        "uncertain_items": [{"item": "具体数值以正式文本为准。", "status": "待补证"}],
    }

    result = generate_weekly_article(
        database_path,
        FakeArticleRunner(payload=payload),
        max_signals=5,
    )

    markdown = result.markdown_path.read_text(encoding="utf-8")
    assert "## 二、对决策者的建议" in markdown
    assert "- 建议企业提前盘点合规档案。" in markdown
    assert "- 建议关注新功能申报窗口。" in markdown
    assert "三、空节" not in markdown
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert "待人工复核的不确定项" in manifest["editor_notes"]
    assert "具体数值以正式文本为准" in manifest["editor_notes"]
