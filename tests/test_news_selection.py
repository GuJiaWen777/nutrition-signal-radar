import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nutrition_signal_radar.article_contract import (
    ArticleBatch,
    NormalizedArticle,
    external_id_for_article,
)
from nutrition_signal_radar.cli import main
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.news_fetch_service import fetch_enabled_news_sources
from nutrition_signal_radar.news_registry import (
    configure_news_source,
    seed_news_sources,
    set_news_source_enabled,
)
from nutrition_signal_radar.news_selection import (
    evaluate_pending_news_articles,
    list_news_items,
    url_identity_keys,
)
from tests.test_site_fetch_gateway import CST, NOW, FakeResponse, FakeSession


def _enable(database_path: Path, **kwargs) -> None:
    configure_news_source(database_path, **kwargs)
    set_news_source_enabled(database_path, name=kwargs["name"], enabled=True)


def _ingest_rss(database_path: Path, feed: str, *, entry_url: str, name: str) -> None:
    session = FakeSession()
    session.queue(entry_url, FakeResponse(feed))
    fetch_enabled_news_sources(
        database_path,
        session=session,
        lookback_hours=24,
        max_items_per_source=30,
        raw_dir=database_path.parent / "raw" / "sites",
        now=NOW,
    )


def test_selection_applies_admission_rules_sections_and_priorities(tmp_path: Path) -> None:
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
    recent = (NOW - timedelta(hours=1)).astimezone(CST)
    feed = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title>
      <item><title>市场监管总局关于征求保健食品原料目录意见的公告</title>
        <link>https://www.example.gov.cn/a/1</link>
        <pubDate>Mon, {recent:%d %b %Y} 08:00:00 +0800</pubDate>
        <description>为规范保健食品注册备案，现公开征求意见。</description></item>
      <item><title>某地举办蔬菜种植技术培训的通知</title>
        <link>https://www.example.gov.cn/a/2</link>
        <pubDate>Mon, {recent:%d %b %Y} 08:00:00 +0800</pubDate>
        <description>面向种植户的农技推广活动安排。</description></item>
    </channel></rss>"""
    _ingest_rss(database_path, feed, entry_url="https://www.example.gov.cn/feed.xml", name="测试监管网")

    report = evaluate_pending_news_articles(database_path)

    assert report.evaluated == 2
    assert report.selected == 1
    assert report.rejected == 1
    assert report.rejection_counts == {"not_relevant": 1}
    assert report.selected_by_section == {"policy": 1}
    assert report.daily_eligible is False

    items = {item.title: item for item in list_news_items(database_path)}
    selected = items["市场监管总局关于征求保健食品原料目录意见的公告"]
    assert selected.selected is True
    assert selected.section == "policy"
    assert selected.priority == "P0"
    assert selected.url == "https://www.example.gov.cn/a/1"

    # Running again is a no-op: every article is evaluated exactly once.
    second = evaluate_pending_news_articles(database_path)
    assert second.evaluated == 0


def test_selection_rejects_incomplete_and_near_duplicate_items(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    seed_news_sources(database_path)
    _enable(
        database_path,
        name="测试品牌源",
        domain="example-brand.com",
        category="brand",
        entry_url="https://www.example-brand.com/feed.xml",
        fetch_kind="rss",
        default_priority="P1",
    )
    recent = (NOW - timedelta(hours=2)).astimezone(CST)
    feed = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title>
      <item><title>汤臣倍健发布半年度业绩报告净利增长</title>
        <link>https://www.example-brand.com/news/1</link>
        <pubDate>Mon, {recent:%d %b %Y} 07:00:00 +0800</pubDate>
        <description>公司发布半年度业绩报告，保健食品业务稳健增长。</description></item>
      <item><title>汤臣倍健发布半年度业绩报告净利增长!</title>
        <link>https://www.example-brand.com/news/1-dupe</link>
        <pubDate>Mon, {recent:%d %b %Y} 07:30:00 +0800</pubDate>
        <description>同稿转载：公司发布半年度业绩报告，保健食品业务稳健增长。</description></item>
    </channel></rss>"""
    _ingest_rss(database_path, feed, entry_url="https://www.example-brand.com/feed.xml", name="测试品牌源")
    # The fetch gateway drops dateless items, so construct the missing-date
    # case directly at the ingestion boundary.
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="news",
            source_path="direct",
            source_fingerprint="direct-dateless",
            source_rows=1,
            articles=(
                NormalizedArticle(
                    external_id=external_id_for_article(
                        url="https://www.example-brand.com/news/3",
                        account_name="测试品牌源",
                        title="无日期的营养行业动态不应入选统计表",
                        published_at=None,
                        content_hash=None,
                    ),
                    account_name="测试品牌源",
                    title="无日期的营养行业动态不应入选统计表",
                    published_at=None,
                    url="https://www.example-brand.com/news/3",
                    content_snapshot="缺少发布日期的条目必须被拦截。",
                    content_hash=None,
                ),
            ),
        ),
    )

    report = evaluate_pending_news_articles(database_path)

    assert report.selected == 1
    assert report.rejection_counts["duplicate_title"] == 1
    assert report.rejection_counts["missing_date"] == 1
    assert report.daily_eligible is False

    items = {item.title: item for item in list_news_items(database_path)}
    selected = items["汤臣倍健发布半年度业绩报告净利增长"]
    assert selected.section == "brand_finance"
    assert selected.priority == "P1"
    assert any("P1" in reason for reason in selected.priority_reasons)
    duplicate = items["汤臣倍健发布半年度业绩报告净利增长!"]
    assert duplicate.selected is False
    assert duplicate.rejection_reasons[0]["code"] == "duplicate_title"

    selected_only = list_news_items(database_path, selected_only=True)
    assert [item.title for item in selected_only] == ["汤臣倍健发布半年度业绩报告净利增长"]


def test_selection_expo_preview_maps_to_p3_and_plain_expo_to_p2(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    seed_news_sources(database_path)
    _enable(
        database_path,
        name="测试展会源",
        domain="example-expo.com",
        category="expo",
        entry_url="https://www.example-expo.com/feed.xml",
        fetch_kind="rss",
        default_priority="P2",
    )
    recent = (NOW - timedelta(hours=3)).astimezone(CST)
    feed = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title>
      <item><title>HNC健康营养展将于明年六月开展敬请期待</title>
        <link>https://www.example-expo.com/news/preview</link>
        <pubDate>Mon, {recent:%d %b %Y} 06:00:00 +0800</pubDate>
        <description>2027年HNC健康营养展预告正式发布。</description></item>
      <item><title>NHNE健康营养博览会今日在上海开幕</title>
        <link>https://www.example-expo.com/news/opening</link>
        <pubDate>Mon, {recent:%d %b %Y} 06:00:00 +0800</pubDate>
        <description>NHNE健康营养博览会开幕，保健食品与特医食品板块同期亮相。</description></item>
    </channel></rss>"""
    _ingest_rss(database_path, feed, entry_url="https://www.example-expo.com/feed.xml", name="测试展会源")

    report = evaluate_pending_news_articles(database_path)

    assert report.selected == 2
    items = {item.title: item for item in list_news_items(database_path)}
    preview = items["HNC健康营养展将于明年六月开展敬请期待"]
    opening = items["NHNE健康营养博览会今日在上海开幕"]
    assert preview.priority == "P3"
    assert opening.priority == "P2"
    assert report.daily_eligible is False


def test_news_selection_cli_select_and_list_items(tmp_path: Path, capsys) -> None:
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
    recent = (NOW - timedelta(hours=1)).astimezone(CST)
    feed = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title>
      <item><title>市场监管总局发布保健食品备案管理新规</title>
        <link>https://www.example.gov.cn/a/7</link>
        <pubDate>Mon, {recent:%d %b %Y} 08:00:00 +0800</pubDate>
        <description>保健食品备案管理新规正式发布。</description></item>
    </channel></rss>"""
    _ingest_rss(database_path, feed, entry_url="https://www.example.gov.cn/feed.xml", name="测试监管网")

    select_code = main(["--project-root", str(tmp_path), "news", "select", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert select_code == 0
    assert payload["selected"] == 1
    assert payload["daily_eligible"] is False

    list_code = main(["--project-root", str(tmp_path), "news", "list-items", "--json"])
    listed = json.loads(capsys.readouterr().out)
    assert list_code == 0
    assert listed["count"] == 1
    assert listed["items"][0]["section"] == "policy"

    human_code = main(["--project-root", str(tmp_path), "news", "list-items", "--selected-only"])
    human_output = capsys.readouterr().out
    assert human_code == 0
    assert "[P0|政策监管]" in human_output
    assert "selected" in human_output


def test_selection_catches_date_prefixed_repost_variants(tmp_path: Path) -> None:
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
    recent = (NOW - timedelta(hours=1)).astimezone(CST)
    base_title = "关于召开2026年海洋食品安全与营养健康学术会议的通知（第二轮）"
    feed = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title>
      <item><title>{base_title}</title>
        <link>https://www.example.gov.cn/a/1</link>
        <pubDate>Mon, {recent:%d %b %Y} 08:00:00 +0800</pubDate>
        <description>海洋食品安全与营养健康学术会议第二轮通知。</description></item>
      <item><title>【2026-08-31】分会 | {base_title}</title>
        <link>https://www.example.gov.cn/a/2</link>
        <pubDate>Mon, {recent:%d %b %Y} 09:00:00 +0800</pubDate>
        <description>带日期前缀的同稿转载不应重复入选。</description></item>
    </channel></rss>"""
    _ingest_rss(database_path, feed, entry_url="https://www.example.gov.cn/feed.xml", name="测试监管网")

    report = evaluate_pending_news_articles(database_path)

    assert report.selected == 1
    assert report.rejection_counts.get("duplicate_title") == 1
    items = {item.title: item for item in list_news_items(database_path)}
    duplicate = next(
        item for item in items.values() if item.title.startswith("【2026-08-31】")
    )
    assert duplicate.selected is False
    assert duplicate.rejection_reasons[0]["code"] == "duplicate_title"


def test_url_identity_keys_collapse_cms_section_paths() -> None:
    notice = url_identity_keys("https://www.cnsoc.org/notice/182630200.html")
    other_section = url_identity_keys("https://www.cnsoc.org/acadconfg/182630200.html")
    assert notice & other_section
    assert "cnsoc.org#id:182630200" in notice

    short_a = url_identity_keys("https://www.example.gov.cn/a/1")
    short_b = url_identity_keys("https://www.example.gov.cn/a/2")
    assert not (short_a & short_b)


def test_selection_rejects_same_cms_id_on_different_paths(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    seed_news_sources(database_path)
    _enable(
        database_path,
        name="测试科研源",
        domain="cnsoc.example",
        category="research",
        entry_url="https://www.cnsoc.example/feed.xml",
        fetch_kind="rss",
        default_priority="P2",
    )
    recent = (NOW - timedelta(hours=1)).astimezone(CST)
    feed = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title>
      <item><title>关于召开2026年海洋食品安全与营养健康学术会议的通知（第二轮）</title>
        <link>https://www.cnsoc.example/notice/182630200.html</link>
        <pubDate>Mon, {recent:%d %b %Y} 08:00:00 +0800</pubDate>
        <description>营养健康学术会议第二轮通知，请相关单位报名参加。</description></item>
      <item><title>分会发布海洋食品专题会议第二轮报名安排</title>
        <link>https://www.cnsoc.example/acadconfg/182630200.html</link>
        <pubDate>Mon, {recent:%d %b %Y} 09:00:00 +0800</pubDate>
        <description>同一通知在另一栏目页重复出现，营养健康会议报名安排。</description></item>
    </channel></rss>"""
    _ingest_rss(
        database_path, feed, entry_url="https://www.cnsoc.example/feed.xml", name="测试科研源"
    )

    report = evaluate_pending_news_articles(database_path)

    assert report.selected == 1
    assert report.rejection_counts.get("duplicate_url") == 1
    items = {item.url: item for item in list_news_items(database_path)}
    kept = items["https://www.cnsoc.example/notice/182630200.html"]
    duplicate = items["https://www.cnsoc.example/acadconfg/182630200.html"]
    assert kept.selected is True
    assert duplicate.selected is False
    assert duplicate.rejection_reasons[0]["code"] == "duplicate_url"


def test_policy_sources_count_bare_food_terms_as_relevant(tmp_path: Path) -> None:
    """食品安全/食品抽检 is in the docx scope even without 保健/营养 keywords."""

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
    recent = (NOW - timedelta(hours=2)).astimezone(CST)
    feed = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title>
      <item><title>监管部门开展食品安全专项检查行动</title>
        <link>https://www.example.gov.cn/a/1</link>
        <pubDate>Mon, {recent:%d %b %Y} 08:00:00 +0800</pubDate>
        <description>针对食品生产企业的专项检查正式启动。</description></item>
    </channel></rss>"""
    _ingest_rss(database_path, feed, entry_url="https://www.example.gov.cn/feed.xml", name="测试监管网")

    report = evaluate_pending_news_articles(database_path)

    assert report.selected == 1
    item = list_news_items(database_path, selected_only=True)[0]
    assert item.section == "policy"
    assert item.priority == "P0"


def test_expo_source_counts_exhibition_terms_as_relevant(tmp_path: Path) -> None:
    """展会源：参展/亮相类标题即视为相关，不再要求同时命中营养词表。"""

    import sqlite3

    from nutrition_signal_radar.news_registry import seed_news_sources
    from nutrition_signal_radar.news_selection import evaluate_pending_news_articles
    from nutrition_signal_radar.news_registry import configure_news_source, set_news_source_enabled
    from nutrition_signal_radar.ingest_service import import_article_batch
    from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle, external_id_for_article

    database_path = tmp_path / "var" / "radar.sqlite3"
    seed_news_sources(database_path)
    configure_news_source(
        database_path,
        name="测试展会源",
        domain="example-expo.com",
        category="expo",
        entry_url="https://www.example-expo.com/feed",
        fetch_kind="rss",
        default_priority="P2",
    )
    set_news_source_enabled(database_path, name="测试展会源", enabled=True)
    published = "2026-08-31 10:00:00"
    article = NormalizedArticle(
        external_id=external_id_for_article(
            url="https://www.example-expo.com/news/1",
            account_name="测试展会源",
            title="白云山汉方将携硬核产品矩阵亮相2026健康营养春季展",
            published_at=published,
            content_hash=None,
        ),
        account_name="测试展会源",
        title="白云山汉方将携硬核产品矩阵亮相2026健康营养春季展",
        published_at=published,
        url="https://www.example-expo.com/news/1",
        content_snapshot="白云山汉方确认参展，将展示植物提取物与营养健康原料解决方案。",
        content_hash=None,
    )
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="news",
            source_path="direct",
            source_fingerprint="expo-direct",
            source_rows=1,
            articles=(article,),
            account_segment="news_source",
        ),
    )
    report = evaluate_pending_news_articles(database_path)
    assert report.selected == 1


def test_rejected_missing_body_items_reopen_after_body_enrichment(tmp_path: Path) -> None:
    """missing_body 被拒条目在正文补全后自动重开重评。"""

    import sqlite3

    from nutrition_signal_radar.article_contract import (
        ArticleBatch,
        NormalizedArticle,
        external_id_for_article,
    )
    from nutrition_signal_radar.ingest_service import import_article_batch
    from nutrition_signal_radar.news_registry import (
        configure_news_source,
        seed_news_sources,
        set_news_source_enabled,
    )
    from nutrition_signal_radar.news_selection import evaluate_pending_news_articles

    database_path = tmp_path / "var" / "radar.sqlite3"
    seed_news_sources(database_path)
    configure_news_source(
        database_path,
        name="测试监管网",
        domain="example.gov.cn",
        category="policy",
        entry_url="https://www.example.gov.cn/feed",
        fetch_kind="rss",
        default_priority="P0",
    )
    set_news_source_enabled(database_path, name="测试监管网", enabled=True)
    title = "广东开展椰子水生产企业全覆盖风险排查"
    article = NormalizedArticle(
        external_id=external_id_for_article(
            url="https://www.example.gov.cn/y/1",
            account_name="测试监管网",
            title=title,
            published_at="2026-08-31 10:00:00",
            content_hash=None,
        ),
        account_name="测试监管网",
        title=title,
        published_at="2026-08-31 10:00:00",
        url="https://www.example.gov.cn/y/1",
        content_snapshot=title,  # 标题快照：首轮必被 missing_body 拒
        content_hash=None,
    )
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="news",
            source_path="direct",
            source_fingerprint="reopen",
            source_rows=1,
            articles=(article,),
            account_segment="news_source",
        ),
    )
    first = evaluate_pending_news_articles(database_path)
    assert first.rejection_counts.get("missing_body") == 1

    # 模拟 fetch 的正文补全：快照换成真实正文
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE source_articles SET content_snapshot = ? WHERE url = ?",
        (
            "广东省市场监督管理局在全省范围开展椰子水生产企业的食品质量安全"
            "全覆盖风险排查，重点核查食品原料收购、杀菌工艺与标签标识环节，"
            "对不符合要求的企业责令整改并公布结果。",
            "https://www.example.gov.cn/y/1",
        ),
    )
    connection.commit()
    connection.close()

    second = evaluate_pending_news_articles(database_path)
    assert second.evaluated == 1  # 重开并重新评估
    assert second.selected == 1
