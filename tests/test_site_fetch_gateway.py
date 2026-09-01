import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nutrition_signal_radar import cli
from nutrition_signal_radar.cli import main
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.news_fetch_service import fetch_enabled_news_sources
from nutrition_signal_radar.news_registry import (
    configure_news_source,
    list_news_sources,
    seed_news_sources,
    set_news_source_enabled,
)
from nutrition_signal_radar.site_fetch_gateway import (
    SiteFetchError,
    _render_dom_with_browser,
    fetch_site_source,
)


CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 31, 9, 0, 0, tzinfo=CST)


def make_source(**overrides):
    values = dict(
        id=1,
        name="测试源",
        domain="example.gov.cn",
        category="policy",
        entry_url="https://www.example.gov.cn/list",
        fetch_kind="html_list",
        default_priority="P0",
        parser_version="v1",
        enabled=True,
        sync_health="unknown",
        last_fetched_at=None,
        last_item_at=None,
        notes="",
        derived_from=None,
        parser_config="",
    )
    values.update(overrides)
    return values["source"] if "source" in values else _source_dataclass(values)


def _source_dataclass(values: dict):
    from nutrition_signal_radar.news_registry import NewsSource

    return NewsSource(**values)


class FakeResponse:
    def __init__(self, body: str | bytes, status_code: int = 200) -> None:
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.status_code = status_code

    @property
    def text(self) -> str:
        return self._body.decode("utf-8")

    @property
    def content(self) -> bytes:
        return self._body

    def json(self) -> object:
        return json.loads(self.text)


class FakeSession:
    def __init__(self) -> None:
        self.responses: dict[str, FakeResponse] = {}
        self.calls: list[tuple] = []

    def queue(self, url: str, response: FakeResponse) -> None:
        self.responses[url] = response

    def get(self, url: str, *, timeout: float, headers: dict) -> FakeResponse:
        self.calls.append(("GET", url))
        return self._pop(url)

    def post(self, url: str, *, data: dict, timeout: float, headers: dict) -> FakeResponse:
        self.calls.append(("POST", url, dict(data)))
        return self._pop(url)

    def _pop(self, url: str) -> FakeResponse:
        if url not in self.responses:
            raise AssertionError(f"unexpected external request: {url}")
        return self.responses.pop(url)


def _enable(database_path: Path, **kwargs) -> None:
    configure_news_source(database_path, **kwargs)
    set_news_source_enabled(database_path, name=kwargs["name"], enabled=True)


def test_gateway_keeps_only_in_window_items_for_rss(tmp_path: Path) -> None:
    feed = """<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title>
      <item><title>保健食品原料目录迎来新一轮调整意见征求</title>
        <link>https://www.example.gov.cn/a/1</link>
        <pubDate>Mon, 31 Aug 2026 07:30:00 +0800</pubDate>
        <description>征求意见稿公开发布。</description></item>
      <item><title>八月初的旧文章不应进入二十四小时窗口</title>
        <link>https://www.example.gov.cn/a/2</link>
        <pubDate>Mon, 10 Aug 2026 07:30:00 +0800</pubDate></item>
      <item><title>没有日期的条目应当被跳过并计数</title>
        <link>https://www.example.gov.cn/a/3</link></item>
    </channel></rss>"""
    session = FakeSession()
    session.queue("https://www.example.gov.cn/feed.xml", FakeResponse(feed))
    source = make_source(
        fetch_kind="rss",
        entry_url="https://www.example.gov.cn/feed.xml",
    )

    batch = fetch_site_source(
        session=session,
        source=source,
        lookback_hours=24,
        max_items=30,
        raw_dir=tmp_path,
        now=NOW,
    )

    assert [item.title for item in batch.items] == ["保健食品原料目录迎来新一轮调整意见征求"]
    assert batch.items[0].url == "https://www.example.gov.cn/a/1"
    assert batch.items[0].published_at == "2026-08-31 07:30:00"
    assert batch.skipped_out_of_window == 1
    assert batch.skipped_no_date == 1
    assert batch.raw_body_path is not None and batch.raw_body_path.is_file()
    assert batch.raw_meta_path is not None and batch.raw_meta_path.is_file()
    assert batch.raw_body_sha256


def test_gateway_parses_gov_html_list_with_relative_links_and_dates(tmp_path: Path) -> None:
    page = """<html><body>
      <script>var junk = "<a href='/junk'>script 里的假链接不应被抓到呀</a>";</script>
      <ul>
        <li><a href="/xxgk/202608/t20260830_123.htm">关于征求保健食品原料目录意见的公告</a><span>2026-08-30</span></li>
        <li><a href="/xxgk/202608/t20260810_456.htm">八月初的旧通知不应进入抓取窗口的</a><span>2026-08-10</span></li>
        <li><a href="https://other-site.com/news/xxxxxxxxxx">外站链接应当被同域规则过滤掉的</a><span>2026-08-30</span></li>
        <li><a href="#">短链不算</a></li>
      </ul>
    </body></html>"""
    session = FakeSession()
    session.queue("https://www.example.gov.cn/list", FakeResponse(page))
    source = make_source()

    batch = fetch_site_source(
        session=session,
        source=source,
        lookback_hours=24,
        max_items=30,
        raw_dir=tmp_path,
        now=NOW,
    )

    assert [item.title for item in batch.items] == ["关于征求保健食品原料目录意见的公告"]
    assert batch.items[0].url == "https://www.example.gov.cn/xxgk/202608/t20260830_123.htm"
    assert batch.items[0].published_at == "2026-08-30 23:59:59"
    assert batch.skipped_out_of_window == 1
    assert batch.skipped_no_date == 0


def test_gateway_cninfo_api_posts_window_and_cleans_titles(tmp_path: Path) -> None:
    inside_ms = int((NOW - timedelta(hours=1)).timestamp() * 1000)
    old_ms = int((NOW - timedelta(days=30)).timestamp() * 1000)
    payload = json.dumps(
        {
            "announcements": [
                {
                    "announcementTitle": "关于<em>保健食品</em>生产许可的公告",
                    "adjunctUrl": "//static.cninfo.com.cn/finalpage/2026-08-31/123.PDF",
                    "secName": "汤臣倍健",
                    "secCode": "300146",
                    "announcementTime": inside_ms,
                },
                {
                    "announcementTitle": "三十年前的一份旧公告不应入选",
                    "adjunctUrl": "//static.cninfo.com.cn/finalpage/1996-01-01/456.PDF",
                    "secName": "某公司",
                    "secCode": "000001",
                    "announcementTime": old_ms,
                },
            ]
        }
    )
    entry = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    session = FakeSession()
    session.queue(entry, FakeResponse(payload))
    source = make_source(
        name="巨潮资讯网",
        domain="cninfo.com.cn",
        category="disclosure",
        entry_url=entry,
        fetch_kind="api",
        default_priority="P1",
    )

    batch = fetch_site_source(
        session=session,
        source=source,
        lookback_hours=24,
        max_items=30,
        raw_dir=tmp_path,
        now=NOW,
    )

    assert len(batch.items) == 1
    item = batch.items[0]
    assert item.title == "关于保健食品生产许可的公告"
    assert item.url == "https://static.cninfo.com.cn/finalpage/2026-08-31/123.PDF"
    assert item.metadata["news_sec_name"] == "汤臣倍健"
    assert batch.skipped_out_of_window == 1
    post_call = next(call for call in session.calls if call[0] == "POST")
    assert post_call[1] == entry
    assert post_call[2]["seDate"] == "2026-08-30~2026-08-31"
    assert int(post_call[2]["pageSize"]) <= 30


def test_gateway_rejects_api_sources_without_a_configured_parser(tmp_path: Path) -> None:
    session = FakeSession()
    source = make_source(
        name="港交所披露易",
        domain="hkexnews.hk",
        category="disclosure",
        entry_url="https://www1.hkexnews.hk/search/titlesearch.xhtml",
        fetch_kind="api",
    )

    with pytest.raises(SiteFetchError, match="尚未实现"):
        fetch_site_source(
            session=session,
            source=source,
            lookback_hours=24,
            max_items=30,
            raw_dir=tmp_path,
            now=NOW,
        )


def test_service_ingests_idempotently_and_records_health(tmp_path: Path) -> None:
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
    feed = """<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title>
      <item><title>关于征求保健食品功能目录意见的通知公告</title>
        <link>https://www.example.gov.cn/a/9</link>
        <pubDate>Mon, 31 Aug 2026 08:00:00 +0800</pubDate>
        <description>正文摘要。</description></item>
    </channel></rss>"""
    session = FakeSession()
    session.queue("https://www.example.gov.cn/feed.xml", FakeResponse(feed))
    raw_dir = tmp_path / "var" / "raw" / "sites"

    first = fetch_enabled_news_sources(
        database_path,
        session=session,
        lookback_hours=24,
        max_items_per_source=30,
        raw_dir=raw_dir,
        now=NOW,
    )
    assert first.outcomes[0].status == "success"
    assert first.articles_inserted == 1
    assert first.outcomes[0].raw_reference and Path(first.outcomes[0].raw_reference).is_file()

    session.queue("https://www.example.gov.cn/feed.xml", FakeResponse(feed))
    second = fetch_enabled_news_sources(
        database_path,
        session=session,
        lookback_hours=24,
        max_items_per_source=30,
        raw_dir=raw_dir,
        now=NOW,
    )
    assert second.articles_inserted == 0
    assert second.articles_updated == 1

    source = next(
        item for item in list_news_sources(database_path) if item.name == "测试监管网"
    )
    assert source.sync_health == "healthy"
    assert source.last_fetched_at is not None
    assert source.last_item_at == "2026-08-31 08:00:00"
    connection = connect_database(database_path, readonly=True)
    try:
        events = connection.execute(
            "SELECT event_type FROM news_source_events WHERE news_source_id = ? ORDER BY id",
            (source.id,),
        ).fetchall()
        assert [event["event_type"] for event in events][-1] == "fetch_succeeded"
    finally:
        connection.close()


def test_service_isolates_failures_across_sources(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    seed_news_sources(database_path)
    _enable(
        database_path,
        name="坏掉的源",
        domain="broken.example.com",
        category="policy",
        entry_url="https://broken.example.com/feed.xml",
        fetch_kind="rss",
        default_priority="P0",
    )
    _enable(
        database_path,
        name="正常的巨潮",
        domain="cninfo.com.cn",
        category="disclosure",
        entry_url="http://www.cninfo.com.cn/new/hisAnnouncement/query",
        fetch_kind="api",
        default_priority="P1",
    )
    inside_ms = int((NOW - timedelta(hours=2)).timestamp() * 1000)
    session = FakeSession()
    session.queue(
        "https://broken.example.com/feed.xml",
        FakeResponse("server exploded", status_code=500),
    )
    session.queue(
        "http://www.cninfo.com.cn/new/hisAnnouncement/query",
        FakeResponse(
            json.dumps(
                {
                    "announcements": [
                        {
                            "announcementTitle": "健康产业公司年度报告",
                            "adjunctUrl": "//static.cninfo.com.cn/finalpage/2026-08-31/789.PDF",
                            "secName": "健康产业",
                            "secCode": "300999",
                            "announcementTime": inside_ms,
                        }
                    ]
                }
            )
        ),
    )

    report = fetch_enabled_news_sources(
        database_path,
        session=session,
        lookback_hours=24,
        max_items_per_source=30,
        raw_dir=tmp_path / "var" / "raw" / "sites",
        now=NOW,
    )

    assert [outcome.status for outcome in report.outcomes] == ["failed", "success"]
    assert report.articles_inserted == 1
    assert "HTTP 500" in (report.outcomes[0].error or "")
    health_by_name = {source.name: source.sync_health for source in list_news_sources(database_path)}
    assert health_by_name["坏掉的源"] == "error"
    assert health_by_name["正常的巨潮"] == "healthy"


def test_news_fetch_cli_requires_live_and_runs_json_when_live(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    protected_code = main(["--project-root", str(tmp_path), "news", "fetch", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert protected_code == 2
    assert payload["status"] == "protected"
    assert not (tmp_path / "var").exists()

    outcome = {
        "source_name": "测试源",
        "domain": "example.gov.cn",
        "fetch_kind": "rss",
        "status": "success",
        "items_found": 2,
        "articles_inserted": 2,
        "articles_updated": 0,
        "skipped_no_date": 1,
        "skipped_out_of_window": 0,
        "raw_reference": "raw/sites/x",
        "error": None,
    }
    captured: dict[str, object] = {}

    def stub(database_path, **kwargs):
        captured["database_path"] = database_path
        captured["lookback_hours"] = kwargs["lookback_hours"]
        captured["source_names"] = kwargs["source_names"]
        return SimpleNamespace(
            database_path=Path(database_path),
            lookback_hours=kwargs["lookback_hours"],
            outcomes=[SimpleNamespace(**outcome)],
            succeeded=1,
            failed=0,
            articles_inserted=2,
            articles_updated=0,
            as_dict=lambda: {
                "database_path": str(database_path),
                "lookback_hours": kwargs["lookback_hours"],
                "sources_fetched": 1,
                "succeeded": 1,
                "failed": 0,
                "articles_inserted": 2,
                "articles_updated": 0,
                "outcomes": [outcome],
            },
        )

    monkeypatch.setattr(cli, "fetch_enabled_news_sources", stub)
    live_code = main(
        [
            "--project-root",
            str(tmp_path),
            "news",
            "fetch",
            "--live",
            "--lookback-hours",
            "12",
            "--source",
            "测试源",
            "--json",
        ]
    )
    live_payload = json.loads(capsys.readouterr().out)
    assert live_code == 0
    assert live_payload["articles_inserted"] == 2
    assert captured["lookback_hours"] == 12
    assert captured["source_names"] == ("测试源",)


def test_browser_render_opt_in_dispatches_and_parses(tmp_path: Path, monkeypatch) -> None:
    from nutrition_signal_radar.site_fetch_gateway import _render_dom_with_browser

    rendered = (
        "<html><body><ul>"
        '<li><a href="/news/1">市场监管总局关于保健食品备案管理的公告</a><span>2026-08-30</span></li>'
        "</ul></body></html>"
    )
    monkeypatch.setattr(
        "nutrition_signal_radar.site_fetch_gateway._render_dom_with_browser",
        lambda source: rendered,
    )
    session = FakeSession()  # browser path must not consume any queued request
    source = make_source(
        parser_config='{"render":"browser"}',
        entry_url="https://www.example.gov.cn/list",
    )

    batch = fetch_site_source(
        session=session,
        source=source,
        lookback_hours=24,
        max_items=30,
        raw_dir=tmp_path,
        now=NOW,
    )

    assert [item.title for item in batch.items] == ["市场监管总局关于保健食品备案管理的公告"]
    assert batch.items[0].url == "https://www.example.gov.cn/news/1"
    assert batch.raw_body_path is not None and batch.raw_body_path.is_file()


def test_browser_render_rejects_unknown_render_option(tmp_path: Path) -> None:
    source = make_source(parser_config='{"render":"magic"}')
    with pytest.raises(SiteFetchError, match="未知渲染方式"):
        fetch_site_source(
            session=FakeSession(),
            source=source,
            lookback_hours=24,
            max_items=30,
            raw_dir=tmp_path,
            now=NOW,
        )


def test_render_dom_with_browser_renders_local_file(tmp_path: Path) -> None:
    page_file = tmp_path / "page.html"
    page_file.write_text("<html><body><h1>浏览器渲染测试OK</h1></body></html>", encoding="utf-8")
    source = make_source(entry_url=page_file.as_uri())

    try:
        html = _render_dom_with_browser(source)
    except SiteFetchError as exc:
        pytest.skip(f"no local browser for playwright: {exc}")
    assert "浏览器渲染测试OK" in html
