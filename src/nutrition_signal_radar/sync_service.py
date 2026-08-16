"""Bounded formal RedFox sync for explicitly enabled source accounts.

This service never discovers or enables accounts.  It only reads entries that
an operator has already configured as ``matched`` and ``enabled`` in the local
source registry, then imports each account in its own transaction so one source
failure does not roll back the rest of the run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Protocol

from .article_contract import ArticleBatch
from .demo_pipeline import DemoBuildResult, build_demo_topics
from .database import connect_database, transaction
from .ingest_service import BatchImportResult, import_article_batch
from .redfox_gateway import RedFoxAccountBatch, RedFoxGatewayError, _body_from_work
from .source_registry import (
    SourceAccount,
    list_enabled_redfox_sources,
    record_source_sync_health,
)


_CST = timezone(timedelta(hours=8), name="Asia/Shanghai")
SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


class RedFoxSyncGateway(Protocol):
    def fetch_account_batch(
        self,
        *,
        account: str,
        account_name: str,
        publish_time_start: str,
        publish_time_end: str,
        max_pages: int,
        max_detail_requests: int,
    ) -> RedFoxAccountBatch: ...

    def fetch_wide_account_batch(
        self,
        *,
        account_name: str,
        publish_time_start: str,
        publish_time_end: str,
        max_pages: int = 3,
    ) -> RedFoxAccountBatch: ...


class RedFoxDetailGateway(Protocol):
    def get_work_detail(
        self, work_uuid: str
    ) -> tuple[Mapping[str, object], str | None]: ...


@dataclass(frozen=True)
class BodyBackfillResult:
    attempted: int
    filled: int
    failed: int

    def as_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "filled": self.filled,
            "failed": self.failed,
        }


def backfill_missing_bodies(
    database_path: Path,
    gateway: RedFoxDetailGateway,
    *,
    limit: int = 200,
) -> BodyBackfillResult:
    """Fill locally stored articles whose body snapshot is still empty.

    Unlike a normal sync, this targets only already-imported rows missing a
    body, so repeated runs do not waste detail calls on the newest articles.
    """

    if limit < 1:
        raise ValueError("limit must be at least 1")
    connection = connect_database(Path(database_path).resolve(), readonly=True)
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, external_id
                FROM source_articles
                WHERE length(trim(content_snapshot)) = 0
                  AND external_id IS NOT NULL
                ORDER BY published_at DESC, id
                LIMIT ?
                """,
                (limit,),
            )
        ]
    finally:
        connection.close()

    filled = 0
    failed = 0
    write_connection = connect_database(Path(database_path).resolve())
    try:
        for row in rows:
            try:
                work_uuid = str(row["external_id"])
                if work_uuid.startswith("redfox:"):
                    work_uuid = work_uuid[len("redfox:") :]
                if not work_uuid:
                    failed += 1
                    continue
                detail, _raw_path = gateway.get_work_detail(work_uuid)
                _state, content = _body_from_work(detail)
                if not content:
                    failed += 1
                    continue
                with transaction(write_connection):
                    write_connection.execute(
                        f"""
                        UPDATE source_articles
                        SET content_snapshot = ?, updated_at = ({SQL_NOW})
                        WHERE id = ?
                        """,
                        (content, int(row["id"])),
                    )
                filled += 1
            except (RedFoxGatewayError, ValueError, OSError):
                failed += 1
    finally:
        write_connection.close()
    return BodyBackfillResult(attempted=len(rows), filled=filled, failed=failed)


@dataclass(frozen=True)
class SourceSyncOutcome:
    source_id: int
    source_name: str
    redfox_library: str
    status: str
    normalized_articles: int
    articles_inserted: int
    articles_updated: int
    detail_requests: int
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "redfox_library": self.redfox_library,
            "status": self.status,
            "normalized_articles": self.normalized_articles,
            "articles_inserted": self.articles_inserted,
            "articles_updated": self.articles_updated,
            "detail_requests": self.detail_requests,
            "message": self.message,
        }


@dataclass(frozen=True)
class RedFoxSyncResult:
    database_path: Path
    publish_time_start: str
    publish_time_end: str
    max_pages_per_source: int
    max_detail_requests_total: int
    outcomes: tuple[SourceSyncOutcome, ...]
    signal_build: DemoBuildResult | None

    @property
    def attempted_sources(self) -> int:
        return len(self.outcomes)

    @property
    def successful_sources(self) -> int:
        return sum(outcome.status in {"imported", "no_articles"} for outcome in self.outcomes)

    @property
    def failed_sources(self) -> int:
        return sum(outcome.status == "error" for outcome in self.outcomes)

    @property
    def imported_articles(self) -> int:
        return sum(outcome.normalized_articles for outcome in self.outcomes)

    def as_dict(self) -> dict[str, object]:
        return {
            "database_path": str(self.database_path),
            "publish_time_start": self.publish_time_start,
            "publish_time_end": self.publish_time_end,
            "max_pages_per_source": self.max_pages_per_source,
            "max_detail_requests_total": self.max_detail_requests_total,
            "attempted_sources": self.attempted_sources,
            "successful_sources": self.successful_sources,
            "failed_sources": self.failed_sources,
            "imported_articles": self.imported_articles,
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "signal_build": _signal_build_payload(self.signal_build),
        }


def sync_enabled_redfox_sources(
    database_path: Path,
    gateway: RedFoxSyncGateway,
    *,
    lookback_days: int = 3,
    max_pages: int = 3,
    max_detail_requests: int = 30,
    as_of: date | None = None,
) -> RedFoxSyncResult:
    """Sync enabled sources, then rebuild rule-based signals for their open cycle.

    The rebuild is deterministic and only updates clusters, signals and
    candidates.  It never approves a topic, starts research, or writes a
    publishing artifact.
    """

    if lookback_days < 1:
        raise ValueError("lookback_days must be at least 1")
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    if max_detail_requests < 0:
        raise ValueError("max_detail_requests cannot be negative")

    end_date = as_of or datetime.now(_CST).date()
    start_date = end_date - timedelta(days=lookback_days - 1)
    sources = list_enabled_redfox_sources(database_path)
    remaining_details = max_detail_requests
    remaining_quality_sources = sum(source.redfox_library == "quality" for source in sources)
    outcomes: list[SourceSyncOutcome] = []
    cycle_slugs: set[str] = set()

    for source in sources:
        detail_allowance = 0
        if source.redfox_library == "quality":
            detail_allowance = _fair_detail_allowance(
                remaining_details, remaining_quality_sources
            )
            # Reserve the allowance before the network call.  If a provider
            # failure happens after accepting a body request, later sources
            # still cannot make the total run exceed its acknowledged cap.
            remaining_quality_sources -= 1
            remaining_details = max(0, remaining_details - detail_allowance)
        try:
            batch = _fetch_source_batch(
                gateway,
                source,
                publish_time_start=start_date.isoformat(),
                publish_time_end=end_date.isoformat(),
                max_pages=max_pages,
                detail_allowance=detail_allowance,
            )
            configured_batch = _batch_for_source(batch.batch, source)
            if not configured_batch.articles:
                record_source_sync_health(
                    database_path,
                    account_id=source.id,
                    health="warning",
                    reason="回溯窗口内没有符合精确来源和日期条件的文章。",
                )
                outcomes.append(
                    SourceSyncOutcome(
                        source_id=source.id,
                        source_name=source.name,
                        redfox_library=source.redfox_library,
                        status="no_articles",
                        normalized_articles=0,
                        articles_inserted=0,
                        articles_updated=0,
                        detail_requests=batch.detail_requests,
                        message="窗口内没有符合精确来源和日期条件的文章；已记录为需关注。",
                    )
                )
                continue

            imported = import_article_batch(database_path, configured_batch)
            record_source_sync_health(
                database_path,
                account_id=source.id,
                health="healthy",
                reason=(
                    f"已同步 {imported.normalized_articles} 篇；"
                    f"新增 {imported.articles_inserted}，更新 {imported.articles_updated}。"
                ),
            )
            if imported.cycle_slug and imported.in_period_articles:
                cycle_slugs.add(imported.cycle_slug)
            outcomes.append(
                SourceSyncOutcome(
                    source_id=source.id,
                    source_name=source.name,
                    redfox_library=source.redfox_library,
                    status="imported",
                    normalized_articles=imported.normalized_articles,
                    articles_inserted=imported.articles_inserted,
                    articles_updated=imported.articles_updated,
                    detail_requests=batch.detail_requests,
                    message="已幂等导入；仍须人工审阅生成的行业信号和候选选题。",
                )
            )
        except Exception as exc:  # One external source must not stop the other sources.
            message = _safe_error(exc)
            try:
                record_source_sync_health(
                    database_path,
                    account_id=source.id,
                    health="error",
                    reason=message,
                )
            except Exception:
                # Preserve the original provider/import failure as the useful result.
                pass
            outcomes.append(
                SourceSyncOutcome(
                    source_id=source.id,
                    source_name=source.name,
                    redfox_library=source.redfox_library,
                    status="error",
                    normalized_articles=0,
                    articles_inserted=0,
                    articles_updated=0,
                    detail_requests=0,
                    message=message,
                )
            )

    signal_build: DemoBuildResult | None = None
    if cycle_slugs:
        # There can be only one open monitoring cycle, but choosing explicitly
        # keeps the side effect tied to the cycle the imports actually joined.
        signal_build = build_demo_topics(database_path, cycle_slug=sorted(cycle_slugs)[-1])

    return RedFoxSyncResult(
        database_path=Path(database_path).resolve(),
        publish_time_start=start_date.isoformat(),
        publish_time_end=end_date.isoformat(),
        max_pages_per_source=max_pages,
        max_detail_requests_total=max_detail_requests,
        outcomes=tuple(outcomes),
        signal_build=signal_build,
    )


def _fetch_source_batch(
    gateway: RedFoxSyncGateway,
    source: SourceAccount,
    *,
    publish_time_start: str,
    publish_time_end: str,
    max_pages: int,
    detail_allowance: int,
) -> RedFoxAccountBatch:
    if source.redfox_library == "quality":
        if not source.redfox_name or not source.redfox_account:
            raise ValueError("优质库来源缺少已确认的 RedFox 名称或 account 标识")
        return gateway.fetch_account_batch(
            account=source.redfox_account,
            account_name=source.redfox_name,
            publish_time_start=publish_time_start,
            publish_time_end=publish_time_end,
            max_pages=max_pages,
            max_detail_requests=detail_allowance,
        )
    if source.redfox_library == "wide":
        if not source.redfox_name:
            raise ValueError("广域库来源缺少已确认的 RedFox 精确展示名")
        return gateway.fetch_wide_account_batch(
            account_name=source.redfox_name,
            publish_time_start=publish_time_start,
            publish_time_end=publish_time_end,
            max_pages=max_pages,
        )
    raise ValueError(f"未知 RedFox 库类型：{source.redfox_library}")


def _batch_for_source(batch: ArticleBatch, source: SourceAccount) -> ArticleBatch:
    """Keep imported articles attached to the configured source-row identity."""

    return replace(
        batch,
        articles=tuple(replace(article, account_name=source.name) for article in batch.articles),
    )


def _fair_detail_allowance(remaining: int, remaining_sources: int) -> int:
    if remaining <= 0 or remaining_sources <= 0:
        return 0
    return remaining // remaining_sources + int(remaining % remaining_sources > 0)


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return f"{type(exc).__name__}: {text}"[:500]


def _signal_build_payload(result: DemoBuildResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "cycle_id": result.cycle_id,
        "cycle_slug": result.cycle_slug,
        "clusters": result.clusters,
        "signals": result.signals,
        "candidates": result.candidates,
        "generator": result.generator,
    }
