from dataclasses import replace
from pathlib import Path

from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.ingest_service import import_article_batch


def _batch(article: NormalizedArticle) -> ArticleBatch:
    return ArticleBatch(
        source_kind="xlsx",
        source_path="D:/read-only/source.xlsx",
        source_fingerprint="fingerprint",
        source_rows=1,
        articles=(article,),
        account_segment="demo_pb",
    )


def test_import_is_idempotent_and_future_adapter_can_match_by_url(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "demo" / "radar.sqlite3"
    article = NormalizedArticle(
        external_id="url:local-fallback",
        account_name="示例公众号",
        title="测试文章",
        published_at="2026-03-08T09:30:00+08:00",
        url="https://mp.weixin.qq.com/s/example",
        content_snapshot="正文",
        content_hash="content-hash",
        read_count=10,
        metadata={"source_kind": "xlsx"},
        source_reference="D:/read-only/source.xlsx#数据列表!2",
    )

    first = import_article_batch(database_path, _batch(article))
    redfox_view = replace(article, external_id="redfox:work-uuid", read_count=30)
    second = import_article_batch(database_path, _batch(redfox_view))

    assert first.accounts_created == 1
    assert first.articles_inserted == 1
    assert first.articles_updated == 0
    assert second.accounts_created == 0
    assert second.articles_inserted == 0
    assert second.articles_updated == 1
    assert second.total_accounts == 1
    assert second.total_articles == 1

    connection = connect_database(database_path, readonly=True)
    try:
        stored = connection.execute(
            "SELECT external_id, url, read_count FROM source_articles"
        ).fetchone()
        account = connection.execute(
            "SELECT segment, validation_status, sync_health FROM source_accounts"
        ).fetchone()
        assert tuple(stored) == (
            "url:local-fallback",
            "https://mp.weixin.qq.com/s/example",
            30,
        )
        assert tuple(account) == ("demo_pb", "matched", "healthy")
    finally:
        connection.close()
