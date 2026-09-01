import json
from pathlib import Path

from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.demo_pipeline import build_demo_topics
from nutrition_signal_radar.ingest_service import import_article_batch


def _article(
    external_id: str,
    account: str,
    title: str,
    url: str,
    content_hash: str,
    reads: int,
) -> NormalizedArticle:
    return NormalizedArticle(
        external_id=external_id,
        account_name=account,
        title=title,
        published_at="2026-03-20T09:30:00+08:00",
        url=url,
        content_snapshot=f"正文-{external_id}",
        content_hash=content_hash,
        read_count=reads,
    )


def test_demo_pipeline_clusters_reprints_and_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    batch = ArticleBatch(
        source_kind="xlsx",
        source_path="D:/source.xlsx",
        source_fingerprint="fingerprint",
        source_rows=3,
        articles=(
            _article("a1", "公众号甲", "某项监管政策发布", "https://example.com/a1", "same", 100),
            _article("a2", "公众号乙", "监管政策正式发布", "https://example.com/a2", "same", 200),
            _article("a3", "公众号丙", "完全不同的研发进展", "https://example.com/a3", "other", 50),
        ),
    )
    import_article_batch(database_path, batch)

    first = build_demo_topics(database_path)
    second = build_demo_topics(database_path)

    assert first.source_articles == 3
    assert first.clusters == 2
    assert first.multi_article_clusters == 1
    assert first.signals == 1
    assert first.candidates == 1
    assert second.clusters == 2

    connection = connect_database(database_path, readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM topic_clusters").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM industry_signals").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM topic_candidates").fetchone()[0] == 1
        signal = connection.execute(
            "SELECT article_count, independent_source_count, evidence_status FROM industry_signals"
        ).fetchone()
        candidate = connection.execute(
            """
            SELECT generated_by, risk_flags_json, status, review_track,
                   track_source, score_breakdown_json
            FROM topic_candidates
            """
        ).fetchone()
        assert tuple(signal) == (2, 1, "needs_evidence")
        assert candidate["generated_by"] == "demo_rules_v3"
        assert candidate["status"] == "candidate_pool"
        assert candidate["review_track"] == "research"
        assert candidate["track_source"] == "rule"
        breakdown = json.loads(candidate["score_breakdown_json"])
        assert breakdown["effective_source_count"] == 2
        assert breakdown["adjusted_reads"] == 200
        assert any("同稿" in risk for risk in json.loads(candidate["risk_flags_json"]))
    finally:
        connection.close()


def test_demo_pipeline_routes_annual_meeting_to_watch_queue(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    articles = (
        _article(
            "event-1",
            "公众号甲",
            "2026 CBA-China年会首波嘉宾官宣",
            "https://example.com/event-1",
            "event-copy",
            1_200,
        ),
        _article(
            "event-2",
            "公众号乙",
            "2026 CBA-China年会首波嘉宾官宣",
            "https://example.com/event-2",
            "event-copy",
            800,
        ),
    )
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="xlsx",
            source_path="D:/source.xlsx",
            source_fingerprint="event-fingerprint",
            source_rows=2,
            articles=articles,
        ),
    )

    build_demo_topics(database_path)

    connection = connect_database(database_path, readonly=True)
    try:
        candidate = connection.execute(
            """
            SELECT review_track, track_reason, proposed_title, score,
                   score_breakdown_json
            FROM topic_candidates
            """
        ).fetchone()
        assert candidate["review_track"] == "watch"
        assert "年会" in candidate["track_reason"]
        assert candidate["proposed_title"].startswith("从“")
        assert candidate["score"] < 55
        breakdown = json.loads(candidate["score_breakdown_json"])
        assert breakdown["decision_relevance"] == 4.0
        assert breakdown["adjusted_reads"] == 1_200
    finally:
        connection.close()


def test_demo_pipeline_prefers_regulatory_analysis_over_event_promotion(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    articles = (
        _article(
            "probiotic-event",
            "公众号甲",
            "天津创源确认参会：益生菌功能食品大会",
            "https://example.com/probiotic-event",
            "probiotic-event-copy",
            8_000,
        ),
        _article(
            "probiotic-regulation",
            "公众号乙",
            "益生菌新规管到了菌株：热销产品无一拿到蓝帽子",
            "https://example.com/probiotic-regulation",
            "probiotic-regulation-analysis",
            0,
        ),
        _article(
            "probiotic-market",
            "公众号丙",
            "益生菌功能食品的下一个市场增量在哪",
            "https://example.com/probiotic-market",
            "probiotic-market-analysis",
            0,
        ),
    )
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="xlsx",
            source_path="http://127.0.0.1:8001",
            source_fingerprint="probiotic-representative-fingerprint",
            source_rows=3,
            articles=articles,
        ),
    )

    result = build_demo_topics(database_path)

    assert result.multi_article_clusters == 1
    assert result.candidates == 1
    connection = connect_database(database_path, readonly=True)
    try:
        cluster = connection.execute(
            "SELECT canonical_name FROM topic_clusters WHERE article_count > 1"
        ).fetchone()
        candidate = connection.execute(
            "SELECT proposed_title, review_track, score_breakdown_json FROM topic_candidates"
        ).fetchone()
        assert cluster["canonical_name"].startswith("益生菌新规")
        assert candidate["proposed_title"].startswith("“益生菌新规")
        assert candidate["review_track"] == "research"
        breakdown = json.loads(candidate["score_breakdown_json"])
        assert breakdown["decision_relevance"] == 25.0
    finally:
        connection.close()


def test_demo_pipeline_routes_digests_and_training_promotions_to_watch(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    articles = (
        _article(
            "digest-1",
            "公众号甲",
            "生物医药行业一周头条",
            "https://example.com/digest-1",
            "digest-copy",
            300,
        ),
        _article(
            "digest-2",
            "公众号乙",
            "生物医药行业一周头条",
            "https://example.com/digest-2",
            "digest-copy",
            200,
        ),
        _article(
            "training-1",
            "公众号甲",
            "制药HPLC分析工程师实训营",
            "https://example.com/training-1",
            "training-copy",
            500,
        ),
        _article(
            "training-2",
            "公众号乙",
            "制药HPLC分析工程师实训营",
            "https://example.com/training-2",
            "training-copy",
            400,
        ),
    )
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="xlsx",
            source_path="D:/source.xlsx",
            source_fingerprint="watch-fingerprint",
            source_rows=4,
            articles=articles,
        ),
    )

    build_demo_topics(database_path)

    connection = connect_database(database_path, readonly=True)
    try:
        candidates = connection.execute(
            "SELECT review_track, track_reason FROM topic_candidates ORDER BY id"
        ).fetchall()
        assert len(candidates) == 2
        assert {candidate["review_track"] for candidate in candidates} == {"watch"}
        reasons = " ".join(candidate["track_reason"] for candidate in candidates)
        assert "资讯汇总" in reasons
        assert "会议、年会、招募或直播" in reasons
    finally:
        connection.close()


def test_demo_pipeline_groups_specific_industry_topic_across_different_angles(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    articles = (
        _article(
            "probiotic-1",
            "公众号甲",
            "一盒情绪益生菌，揭开功能食品的疮疤",
            "https://example.com/probiotic-1",
            "probiotic-analysis",
            0,
        ),
        _article(
            "probiotic-2",
            "公众号乙",
            "合生元、拜奥、万益蓝：现在的益生菌都在怎么玩？",
            "https://example.com/probiotic-2",
            "probiotic-brands",
            0,
        ),
    )
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="xlsx",
            source_path="http://127.0.0.1:8001",
            source_fingerprint="specific-topic-fingerprint",
            source_rows=2,
            articles=articles,
        ),
    )

    result = build_demo_topics(database_path)

    assert result.clusters == 1
    assert result.multi_article_clusters == 1
    assert result.signals == 1
    assert result.candidates == 1

    connection = connect_database(database_path, readonly=True)
    try:
        candidate = connection.execute(
            "SELECT review_track, proposed_title FROM topic_candidates"
        ).fetchone()
        assert candidate["review_track"] == "research"
        assert "一盒情绪益生菌" in candidate["proposed_title"]
        assert "“一盒“" not in candidate["proposed_title"]
        signal = connection.execute(
            "SELECT underlying_event FROM industry_signals"
        ).fetchone()
        assert "围绕“一盒情绪益生菌" in signal["underlying_event"]
        assert "围绕“一盒“" not in signal["underlying_event"]
    finally:
        connection.close()


def test_demo_pipeline_does_not_group_on_generic_health_words(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    articles = (
        _article(
            "generic-1",
            "公众号甲",
            "大健康行业规模与区域市场洞察",
            "https://example.com/generic-1",
            "market-analysis",
            0,
        ),
        _article(
            "generic-2",
            "公众号乙",
            "大健康背景下功能性食品开发方向",
            "https://example.com/generic-2",
            "product-development",
            0,
        ),
    )
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="xlsx",
            source_path="http://127.0.0.1:8001",
            source_fingerprint="generic-topic-fingerprint",
            source_rows=2,
            articles=articles,
        ),
    )

    result = build_demo_topics(database_path)

    assert result.clusters == 2
    assert result.multi_article_clusters == 0
    assert result.candidates == 0


def _article_with_body(
    external_id: str,
    account: str,
    title: str,
    body: str,
    content_hash: str,
    reads: int = 0,
) -> NormalizedArticle:
    return NormalizedArticle(
        external_id=external_id,
        account_name=account,
        title=title,
        published_at="2026-03-20T09:30:00+08:00",
        url=f"https://example.com/{external_id}",
        content_snapshot=body,
        content_hash=content_hash,
        read_count=reads,
    )


def _import_near_dup_fixture(database_path: Path) -> None:
    base = "市场监管部门发布益生菌产品虚假宣传典型案例通报涉及广告用语合规问题"
    edited = base + "并提醒企业规范功效"
    other = "研发团队完成新一代益生菌菌株临床试验研究数据显示改善肠道健康"
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="xlsx",
            source_path="D:/source.xlsx",
            source_fingerprint="near-dup-fingerprint",
            source_rows=3,
            articles=(
                _article_with_body("nd-1", "公众号甲", "益生菌监管案例通报发布", base, "nd-a", 10),
                _article_with_body("nd-2", "公众号乙", "益生菌监管案例通报正式发布", edited, "nd-b", 20),
                _article_with_body("nd-3", "公众号丙", "益生菌菌株临床试验数据发布", other, "nd-c", 30),
            ),
        ),
    )


def _independent_source_count(database_path: Path) -> int:
    connection = connect_database(database_path, readonly=True)
    try:
        return int(
            connection.execute(
                "SELECT independent_source_count FROM industry_signals"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def test_demo_pipeline_merges_near_duplicate_bodies(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    _import_near_dup_fixture(database_path)

    result = build_demo_topics(database_path)

    assert result.clusters == 1
    assert result.signals == 1
    assert _independent_source_count(database_path) == 2


def test_demo_pipeline_keeps_distinct_bodies_separate(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="xlsx",
            source_path="D:/source.xlsx",
            source_fingerprint="distinct-fingerprint",
            source_rows=2,
            articles=(
                _article_with_body(
                    "ds-1",
                    "公众号甲",
                    "益生菌监管案例通报发布",
                    "市场监管部门发布益生菌产品虚假宣传典型案例通报涉及广告用语合规问题",
                    "ds-a",
                    10,
                ),
                _article_with_body(
                    "ds-2",
                    "公众号乙",
                    "益生菌菌株临床试验数据发布",
                    "研发团队完成新一代益生菌菌株临床试验研究数据显示改善肠道健康",
                    "ds-b",
                    20,
                ),
            ),
        ),
    )

    build_demo_topics(database_path)

    assert _independent_source_count(database_path) == 2


def test_demo_pipeline_minhash_branch_matches_exact_branch(
    tmp_path: Path, monkeypatch
) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    _import_near_dup_fixture(database_path)
    monkeypatch.setattr(
        "nutrition_signal_radar.demo_pipeline.MINHASH_SWITCH_THRESHOLD", 2
    )

    build_demo_topics(database_path)

    assert _independent_source_count(database_path) == 2
