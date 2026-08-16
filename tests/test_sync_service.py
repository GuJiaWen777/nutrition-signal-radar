from datetime import date
from pathlib import Path

from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.redfox_gateway import RedFoxAccountBatch
from nutrition_signal_radar.source_registry import configure_source_account
from nutrition_signal_radar.sync_service import (
    backfill_missing_bodies,
    sync_enabled_redfox_sources,
)


def _article(external_id: str, title: str, url: str) -> NormalizedArticle:
    return NormalizedArticle(
        external_id=external_id,
        account_name="provider-name",
        title=title,
        published_at="2026-08-14T09:00:00+08:00",
        url=url,
        content_snapshot=f"正文 {external_id}",
        content_hash=external_id,
        read_count=100,
    )


def _quality_batch() -> RedFoxAccountBatch:
    return RedFoxAccountBatch(
        batch=ArticleBatch(
            source_kind="redfox",
            source_path="https://redfox.hk/story/api/gzhData/queryWorkList",
            source_fingerprint="quality-batch",
            source_rows=2,
            articles=(
                _article("redfox:one", "益生菌监管政策发布", "https://example.com/one"),
                _article("redfox:two", "益生菌监管政策正式发布", "https://example.com/two"),
            ),
        ),
        total=2,
        pages_fetched=1,
        pagination_truncated=False,
        detail_eligible_articles=0,
        detail_requests=0,
        detail_successes=0,
        detail_budget_exhausted=False,
        detail_errors=(),
        articles_with_full_body=2,
        raw_snapshot_paths=(),
    )


class _Gateway:
    def __init__(self) -> None:
        self.quality_calls: list[dict[str, object]] = []
        self.wide_calls: list[dict[str, object]] = []

    def fetch_account_batch(self, **kwargs: object) -> RedFoxAccountBatch:
        self.quality_calls.append(dict(kwargs))
        return _quality_batch()

    def fetch_wide_account_batch(self, **kwargs: object) -> RedFoxAccountBatch:
        self.wide_calls.append(dict(kwargs))
        raise RuntimeError("provider temporarily unavailable")


def test_sync_imports_each_enabled_source_independently_and_rebuilds_signals(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    configure_source_account(
        database_path,
        name="营养健康评论",
        redfox_name="营养健康评论（RedFox）",
        redfox_account="wx_quality",
        redfox_library="quality",
        validation_status="matched",
        enabled=True,
    )
    configure_source_account(
        database_path,
        name="Bonny营养领航",
        redfox_name="Bonny营养领航",
        redfox_account=None,
        redfox_library="wide",
        validation_status="matched",
        enabled=True,
    )
    gateway = _Gateway()

    result = sync_enabled_redfox_sources(
        database_path,
        gateway,
        lookback_days=3,
        max_pages=2,
        max_detail_requests=6,
        as_of=date(2026, 8, 14),
    )

    assert result.attempted_sources == 2
    assert result.successful_sources == 1
    assert result.failed_sources == 1
    assert result.imported_articles == 2
    assert result.signal_build is not None
    assert result.signal_build.signals == 1
    assert gateway.quality_calls[0]["max_detail_requests"] == 6
    assert gateway.wide_calls[0]["account_name"] == "Bonny营养领航"
    assert {outcome.source_name: outcome.status for outcome in result.outcomes} == {
        "营养健康评论": "imported",
        "Bonny营养领航": "error",
    }

    connection = connect_database(database_path, readonly=True)
    try:
        account = connection.execute(
            "SELECT sync_health FROM source_accounts WHERE name = '营养健康评论'"
        ).fetchone()
        failed_account = connection.execute(
            "SELECT sync_health FROM source_accounts WHERE name = 'Bonny营养领航'"
        ).fetchone()
        assert account["sync_health"] == "healthy"
        assert failed_account["sync_health"] == "error"
        assert connection.execute("SELECT COUNT(*) FROM source_articles").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM industry_signals").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM source_registry_events WHERE event_type = 'sync_failed'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_backfill_missing_bodies_targets_only_empty_snapshots(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="redfox",
            source_path="https://redfox.hk/example",
            source_fingerprint="backfill-fixture",
            source_rows=2,
            articles=(
                _article("with-body", "已有正文", "https://example.com/with"),
                NormalizedArticle(
                    external_id="redfox:missing-body",
                    account_name="公众号甲",
                    title="缺正文",
                    published_at="2026-08-12T09:00:00+08:00",
                    url="https://example.com/missing",
                    content_snapshot="",
                    content_hash="missing-body",
                ),
            ),
        ),
    )

    class _DetailGateway:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_work_detail(self, work_uuid: str):
            self.calls.append(work_uuid)
            return {"content": f"回填正文-{work_uuid}"}, None

    gateway = _DetailGateway()
    result = backfill_missing_bodies(database_path, gateway, limit=10)

    assert result.attempted == 1
    assert result.filled == 1
    assert result.failed == 0
    assert gateway.calls == ["missing-body"]
    connection = connect_database(database_path, readonly=True)
    try:
        row = connection.execute(
            "SELECT content_snapshot FROM source_articles WHERE external_id = 'redfox:missing-body'"
        ).fetchone()
        assert row["content_snapshot"] == "回填正文-missing-body"
    finally:
        connection.close()
