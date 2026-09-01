import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nutrition_signal_radar.article_contract import (
    ArticleBatch,
    NormalizedArticle,
    external_id_for_article,
)
from nutrition_signal_radar import cli
from nutrition_signal_radar.cli import main
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.llm_gateway import LlmAnalysisResult
from nutrition_signal_radar.news_compose import build_news_article
from nutrition_signal_radar.news_registry import (
    configure_news_source,
    seed_news_sources,
    set_news_source_enabled,
)
from nutrition_signal_radar.news_selection import evaluate_pending_news_articles
from tests.test_site_fetch_gateway import NOW, FakeResponse, FakeSession


_SUMMARY_SENTENCE = (
    "本次监管动态持续影响营养健康行业的合规要求与竞争格局，"
    "相关企业需要关注后续执行细则并评估自身注册、备案与宣传策略的调整空间。"
)
VALID_SUMMARY = _SUMMARY_SENTENCE * 3
VALID_IMPLICATION = "行业将面临更明确的合规边界，注册、备案与宣传策略需相应调整。"


class FakeComposeGateway:
    def __init__(self, responder) -> None:
        self.calls: list[str] = []
        self._responder = responder

    def analyze(self, system_prompt: str, user_prompt: str, *, json_object: bool = True):
        self.calls.append(user_prompt)
        content = self._responder(user_prompt)
        return LlmAnalysisResult(
            status="completed",
            content=content,
            prompt_tokens=120,
            completion_tokens=60,
            error="",
        )


def _valid_payload() -> str:
    return json.dumps(
        {"summary": VALID_SUMMARY, "implication": VALID_IMPLICATION, "keywords": ["保健食品", "合规"]}
    )


def _enable(database_path: Path, **kwargs) -> None:
    configure_news_source(database_path, **kwargs)
    set_news_source_enabled(database_path, name=kwargs["name"], enabled=True)


def _seed_four_selected_items(database_path: Path, *, base: datetime | None = None) -> None:
    base = base or NOW
    seed_news_sources(database_path)
    _enable(
        database_path,
        name="测试监管网",
        domain="example.gov.cn",
        category="policy",
        entry_url="https://www.example.gov.cn/feed.xml",
        fetch_kind="rss",
        default_priority="P0",
    )
    item = "  <item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate>"
    item += "<description>{summary}</description></item>"
    entries = []
    for offset, (title, link, summary) in enumerate(
        [
            ("市场监管总局就保健食品原料目录公开征求意见", "https://www.example.gov.cn/a/1", "保健食品原料目录征求意见，规范注册备案。"),
            ("监管部门通报保健食品虚假宣传典型案例处罚结果", "https://www.example.gov.cn/a/2", "通报多起虚假宣传处罚，涉及保健食品宣传红线。"),
            ("汤臣倍健发布半年度财报业绩稳健增长", "https://www.example-brand.com/news/1", "公司披露半年度财报，营养健康业务稳健增长。"),
            ("NHNE健康营养博览会今日开幕", "https://www.example-expo.com/news/1", "博览会开幕，保健食品与特医食品板块亮相。"),
        ]
    ):
        published = (base - timedelta(hours=1 + offset)).strftime("%a, %d %b %Y %H:%M:%S +0800")
        entries.append(
            item.format(title=title, link=link, date=published, summary=summary)
        )
    feed = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        + "".join(entries)
        + "</channel></rss>"
    )
    session = FakeSession()
    session.queue("https://www.example.gov.cn/feed.xml", FakeResponse(feed))
    from nutrition_signal_radar.news_fetch_service import fetch_enabled_news_sources

    fetch_result = fetch_enabled_news_sources(
        database_path,
        session=session,
        lookback_hours=24,
        max_items_per_source=30,
        raw_dir=database_path.parent / "raw" / "sites",
        now=NOW,
    )
    assert fetch_result.articles_inserted == 4
    selection = evaluate_pending_news_articles(database_path)
    assert selection.selected == 4


def test_build_produces_versioned_local_artifacts(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    _seed_four_selected_items(database_path)
    gateway = FakeComposeGateway(lambda _prompt: _valid_payload())
    exports_dir = tmp_path / "var" / "exports" / "news-articles"

    first = build_news_article(
        database_path,
        gateway,
        exports_dir=exports_dir,
        llm_model="deepseek-chat",
        now=NOW,
    )
    build_dir = Path(first.detail)

    assert first.status == "built"
    assert first.mode == "daily"
    assert first.version == 1
    assert first.item_count == 4
    assert first.title.startswith("营养健康行业资讯｜")
    expected_files = {
        "cover.png",
        "manifest.json",
        "news-article.html",
        "news-article.md",
        "qc-log.json",
        "source-table.csv",
    }
    if (build_dir / "cover.html").is_file():
        expected_files.add("cover.html")
    assert set(first.files) == expected_files
    html_text = (build_dir / "news-article.html").read_text(encoding="utf-8")
    assert "01｜政策监管" in html_text
    assert "02｜品牌与财报" in html_text
    assert "03｜展会与产业活动" in html_text
    assert "#173B66" in html_text and "#72BF44" in html_text
    assert "查看原文 →" in html_text
    assert "本文根据公开信息整理" in html_text
    markdown = (build_dir / "news-article.md").read_text(encoding="utf-8")
    assert "对行业意味着什么" in markdown
    qc = json.loads((build_dir / "qc-log.json").read_text(encoding="utf-8"))
    assert qc["all_passed"] is True
    assert all(check["summary_length_ok"] for check in qc["item_checks"])
    csv_text = (build_dir / "source-table.csv").read_text(encoding="utf-8-sig")
    assert "是否入选" in csv_text and "是" in csv_text

    second = build_news_article(
        database_path,
        gateway,
        exports_dir=exports_dir,
        llm_model="deepseek-chat",
        now=NOW,
    )
    assert second.version == 2


def test_build_falls_back_to_weekly_and_skips_when_insufficient(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    seed_news_sources(database_path)
    _enable(
        database_path,
        name="测试监管网",
        domain="example.gov.cn",
        category="policy",
        entry_url="https://www.example.gov.cn/feed.xml",
        fetch_kind="rss",
        default_priority="P0",
    )
    # Items published 3 days ago fall outside the 24-hour daily window, so they
    # are ingested directly to exercise the weekly fallback at build time.
    older = NOW - timedelta(days=3)
    articles = tuple(
        NormalizedArticle(
            external_id=external_id_for_article(
                url=link,
                account_name="测试监管网",
                title=title,
                published_at=(older - timedelta(hours=offset)).strftime("%Y-%m-%d %H:%M:%S"),
                content_hash=None,
            ),
            account_name="测试监管网",
            title=title,
            published_at=(older - timedelta(hours=offset)).strftime("%Y-%m-%d %H:%M:%S"),
            url=link,
            content_snapshot="保健食品监管动态与行业合规要求说明。",
            content_hash=None,
        )
        for offset, (title, link) in enumerate(
            [
                ("市场监管总局发布保健食品备案管理新规", "https://www.example.gov.cn/a/1"),
                ("监管部门征求意见拟调整保健食品功能目录", "https://www.example.gov.cn/a/2"),
            ]
        )
    )
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="news",
            source_path="direct",
            source_fingerprint="direct-weekly",
            source_rows=len(articles),
            articles=articles,
            account_segment="news_source",
        ),
    )
    evaluate_pending_news_articles(database_path)

    weekly = build_news_article(
        database_path,
        FakeComposeGateway(lambda _prompt: _valid_payload()),
        exports_dir=tmp_path / "var" / "exports" / "news-articles",
        now=NOW,
    )
    assert weekly.mode == "weekly"
    assert weekly.status == "built"
    assert weekly.title == "本周营养健康行业资讯｜政策、财报与展会动态一览"

    empty_db = tmp_path / "var2" / "radar.sqlite3"
    seed_news_sources(empty_db)
    skipped = build_news_article(
        empty_db,
        FakeComposeGateway(lambda _prompt: _valid_payload()),
        exports_dir=tmp_path / "var" / "exports" / "news-articles",
        now=NOW,
    )
    assert skipped.status == "skipped"
    assert skipped.version == 0
    assert skipped.files == ()


def test_summary_contract_excludes_items_that_keep_failing(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    _seed_four_selected_items(database_path)

    def bad_summary(_prompt: str) -> str:
        return json.dumps({"summary": "太短", "implication": "", "keywords": []})

    gateway = FakeComposeGateway(bad_summary)
    result = build_news_article(
        database_path,
        gateway,
        exports_dir=tmp_path / "var" / "exports" / "news-articles",
        now=NOW,
    )
    assert result.status == "skipped"
    assert "摘要契约" in result.detail
    assert gateway.calls  # the model was consulted before exclusion

    def medical_claim(prompt: str) -> str:
        if "虚假宣传典型案例" in prompt:
            # First attempt violates the medical-claim red line, second is clean.
            if not getattr(medical_claim, "_retried", False):
                medical_claim._retried = True
                return json.dumps(
                    {"summary": "该产品可以治疗慢性病并具有疗效，行业迎来巨震。" * 3, "implication": VALID_IMPLICATION}
                )
        return _valid_payload()

    retry_gateway = FakeComposeGateway(medical_claim)
    result2 = build_news_article(
        database_path,
        retry_gateway,
        exports_dir=tmp_path / "var" / "exports" / "news-articles",
        now=NOW,
    )
    assert result2.status == "built"
    medical_prompts = [call for call in retry_gateway.calls if "虚假宣传典型案例" in call]
    assert len(medical_prompts) >= 2  # the forbidden summary triggered a retry


def test_news_build_cli_gates_and_live_path(tmp_path: Path, capsys, monkeypatch) -> None:
    protected_code = main(["--project-root", str(tmp_path), "news", "build", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert protected_code == 2
    assert payload["status"] == "protected"

    live_code = main(["--project-root", str(tmp_path), "news", "build", "--live", "--json"])
    assert live_code == 1  # analysis backend not configured in the temp project root

    # The CLI build runs on the real clock, so seed relative to actual time
    # to keep the daily window deterministic regardless of when tests run.
    _seed_four_selected_items(
        tmp_path / "var" / "radar.sqlite3",
        base=datetime.now(timezone(timedelta(hours=8))),
    )
    monkeypatch.setenv("RADAR_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("RADAR_LLM_API_KEY", "test-key")
    monkeypatch.setenv("RADAR_LLM_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("RADAR_LLM_MODEL", "deepseek-chat")

    def fake_gateway_factory(**kwargs):
        return FakeComposeGateway(lambda _prompt: _valid_payload())

    monkeypatch.setattr(cli, "LlmGateway", fake_gateway_factory)
    built_code = main(["--project-root", str(tmp_path), "news", "build", "--live", "--json"])
    built = json.loads(capsys.readouterr().out)
    assert built_code == 0
    assert built["status"] == "built"
    assert built["mode"] == "daily"
    assert built["item_count"] == 4
    assert Path(built["detail"]).is_dir()


def test_build_title_strips_markers_and_truncates_at_boundary():
    from nutrition_signal_radar.news_compose import ComposedItem, _build_title

    item = ComposedItem(
        article_id=1, source_name="中国营养学会", section="expo", priority="P2",
        title="【2026-08-31】分会 | 关于召开“2026年海洋食品安全与营养健康学术会议”的通知（第二轮）",
        url=None, published_at=None, summary="", implication="", keywords=(),
    )
    title = _build_title("daily", [item])

    assert title == "营养健康行业资讯｜关于召开“2026年海洋食品安全与营养健康学术会议”"
    assert "【" not in title and "分会 |" not in title

    short = ComposedItem(
        article_id=2, source_name="测试源", section="policy", priority="P0",
        title="保健食品备案新规发布", url=None, published_at=None,
        summary="", implication="", keywords=(),
    )
    assert _build_title("daily", [short]) == "营养健康行业资讯｜保健食品备案新规发布"
