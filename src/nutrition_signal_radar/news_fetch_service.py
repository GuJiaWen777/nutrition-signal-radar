"""Orchestration for the news-line daily fetch (Phase 4, ADR-0014).

For every operator-enabled whitelist source: fetch via the site gateway,
archive the raw snapshot, ingest items through the shared normalized-article
boundary (idempotent), and record per-source health so one failed source never
blocks the others.  This module performs external requests only when the
caller passes a real HTTP session, which the CLI does behind ``--live``.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Sequence

import requests

from .article_contract import ArticleBatch, NormalizedArticle, external_id_for_article
from .ingest_service import import_article_batch
from .news_registry import (
    NewsSource,
    list_enabled_news_sources,
    record_news_source_health,
)
from .site_fetch_gateway import USER_AGENT, fetch_site_source


class NewsFetchError(RuntimeError):
    """Raised when the fetch run itself is misconfigured."""


@dataclass(frozen=True)
class NewsSourceFetchOutcome:
    source_name: str
    domain: str
    fetch_kind: str
    status: str  # success | no_items | failed
    items_found: int
    articles_inserted: int
    articles_updated: int
    skipped_no_date: int
    skipped_out_of_window: int
    raw_reference: str | None
    error: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "source_name": self.source_name,
            "domain": self.domain,
            "fetch_kind": self.fetch_kind,
            "status": self.status,
            "items_found": self.items_found,
            "articles_inserted": self.articles_inserted,
            "articles_updated": self.articles_updated,
            "skipped_no_date": self.skipped_no_date,
            "skipped_out_of_window": self.skipped_out_of_window,
            "raw_reference": self.raw_reference,
            "error": self.error,
        }


@dataclass(frozen=True)
class NewsFetchReport:
    database_path: Path
    lookback_hours: int
    outcomes: tuple[NewsSourceFetchOutcome, ...]
    articles_inserted: int
    articles_updated: int

    @property
    def succeeded(self) -> int:
        return sum(outcome.status in ("success", "no_items") for outcome in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(outcome.status == "failed" for outcome in self.outcomes)

    def as_dict(self) -> dict[str, object]:
        return {
            "database_path": str(self.database_path),
            "lookback_hours": self.lookback_hours,
            "sources_fetched": len(self.outcomes),
            "succeeded": self.succeeded,
            "failed": self.failed,
            "articles_inserted": self.articles_inserted,
            "articles_updated": self.articles_updated,
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
        }


# 标题条目的正文补全：列表页只有标题时，逐条请求详情页抽取正文段落。
# 有界：每源每轮最多 _DETAIL_ENRICH_MAX 条、单条 12 秒超时、失败保持标题降级。
_DETAIL_ENRICH_MAX = 5
_DETAIL_ENRICH_TIMEOUT = 12.0
_DETAIL_ENRICH_MIN_CHARS = 60


def _extract_article_text(html_text: str) -> str:
    cleaned = re.sub(r"<(script|style)[\s\S]*?</>", " ", html_text, flags=re.IGNORECASE)
    paragraphs: list[str] = []
    for match in re.finditer(r"<p[^>]*>([\s\S]*?)</p>", cleaned, re.IGNORECASE):
        text = html.unescape(re.sub(r"<[^>]+>", "", match.group(1)))
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) >= 15:
            paragraphs.append(text)
    return " ".join(paragraphs)[:1500].strip()


def _enrich_headline_items(session, source, items, raw_dir: Path):
    """对 summary 为空的标题条目做单条详情页正文补全（有界、降级安全）。"""

    if source.fetch_kind not in ("html_list", "rss"):
        return items
    enriched: list = []
    fetched = 0
    changed = False
    for item in items:
        url = str(item.url or "")
        if (
            str(item.summary or "").strip()
            or fetched >= _DETAIL_ENRICH_MAX
            or not url.startswith(("http://", "https://"))
        ):
            enriched.append(item)
            continue
        try:
            response = session.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=_DETAIL_ENRICH_TIMEOUT
            )
            if response.status_code != 200:
                enriched.append(item)
                continue
            page = response.text
        except Exception:
            enriched.append(item)
            continue
        fetched += 1
        text = _extract_article_text(page)
        title_norm = re.sub(r"\s+", "", item.title)
        text_norm = re.sub(r"\s+", "", text)
        if len(text_norm) < _DETAIL_ENRICH_MIN_CHARS or text_norm == title_norm:
            enriched.append(item)
            continue
        try:
            detail_dir = Path(raw_dir) / source.domain
            detail_dir.mkdir(parents=True, exist_ok=True)
            stamp = re.sub(r"[^0-9]", "", str(item.published_at or ""))[:8] or "nodate"
            token = f"{abs(hash(url)) % 10**8:08d}"
            (detail_dir / f"detail-{stamp}-{token}.html").write_text(
                page, encoding="utf-8", errors="ignore"
            )
        except OSError:
            pass
        metadata = dict(item.metadata)
        metadata["enriched_body"] = True
        enriched.append(replace(item, summary=text))
        changed = True
    return tuple(enriched) if changed else items


def fetch_enabled_news_sources(
    database_path: Path,
    *,
    session: requests.Session | None = None,
    lookback_hours: int = 24,
    max_items_per_source: int = 30,
    raw_dir: Path,
    source_names: Sequence[str] = (),
    actor: str = "news-fetch",
    now: datetime | None = None,
) -> NewsFetchReport:
    """Fetch every enabled whitelist source with per-source failure isolation."""

    if lookback_hours < 1:
        raise NewsFetchError("lookback_hours must be at least 1")
    if max_items_per_source < 1:
        raise NewsFetchError("max_items_per_source must be at least 1")

    http_session = session if session is not None else requests.Session()
    wanted = {name.strip() for name in source_names if str(name).strip()}
    sources = [
        source
        for source in list_enabled_news_sources(database_path)
        if not wanted or source.name in wanted
    ]

    outcomes: list[NewsSourceFetchOutcome] = []
    total_inserted = 0
    total_updated = 0
    for source in sources:
        outcome = _fetch_one(
            database_path,
            http_session,
            source,
            lookback_hours=lookback_hours,
            max_items=max_items_per_source,
            raw_dir=raw_dir,
            actor=actor,
            now=now,
        )
        outcomes.append(outcome)
        total_inserted += outcome.articles_inserted
        total_updated += outcome.articles_updated

    return NewsFetchReport(
        database_path=Path(database_path).resolve(),
        lookback_hours=lookback_hours,
        outcomes=tuple(outcomes),
        articles_inserted=total_inserted,
        articles_updated=total_updated,
    )


def _fetch_one(
    database_path: Path,
    session,
    source: NewsSource,
    *,
    lookback_hours: int,
    max_items: int,
    raw_dir: Path,
    actor: str,
    now: datetime | None,
) -> NewsSourceFetchOutcome:
    try:
        batch = fetch_site_source(
            session=session,
            source=source,
            lookback_hours=lookback_hours,
            max_items=max_items,
            raw_dir=Path(raw_dir),
            now=now,
        )
    except Exception as exc:  # isolation: one failed source never blocks the others
        reason = f"{source.name}: {exc}"
        record_news_source_health(
            database_path,
            source_id=source.id,
            health="error",
            actor=actor,
            reason=reason[:500],
        )
        return NewsSourceFetchOutcome(
            source_name=source.name,
            domain=source.domain,
            fetch_kind=source.fetch_kind,
            status="failed",
            items_found=0,
            articles_inserted=0,
            articles_updated=0,
            skipped_no_date=0,
            skipped_out_of_window=0,
            raw_reference=None,
            error=str(exc)[:500],
        )

    batch = replace(
        batch,
        items=_enrich_headline_items(session, source, batch.items, Path(raw_dir)),
    )
    latest_item_at = max(
        (item.published_at for item in batch.items if item.published_at),
        default=None,
    )
    articles = tuple(
        NormalizedArticle(
            external_id=external_id_for_article(
                url=item.url,
                account_name=source.name,
                title=item.title,
                published_at=item.published_at,
                content_hash=None,
            ),
            account_name=source.name,
            title=item.title,
            published_at=item.published_at,
            url=item.url,
            # API disclosure items (e.g. cninfo PDFs) carry no summary text; the
            # announcement title itself is the verifiable substance, so fall
            # back to it to keep the body-or-summary completeness gate honest.
            content_snapshot=item.summary or item.title,
            content_hash=None,
            metadata={
                **item.metadata,
                "news_category": source.category,
                "news_domain": source.domain,
                "news_default_priority": source.default_priority,
                "fetch_kind": source.fetch_kind,
            },
            source_reference=str(batch.raw_body_path) if batch.raw_body_path else None,
        )
        for item in batch.items
    )

    try:
        result = import_article_batch(
            database_path,
            ArticleBatch(
                source_kind="news",
                source_path=source.entry_url,
                source_fingerprint=batch.raw_body_sha256 or source.entry_url,
                source_rows=len(articles),
                articles=articles,
                account_segment="news_source",
            ),
        )
    except Exception as exc:  # isolation: an ingest failure is recorded, not raised
        reason = f"{source.name}: ingest failed: {exc}"
        record_news_source_health(
            database_path,
            source_id=source.id,
            health="error",
            latest_item_at=latest_item_at,
            actor=actor,
            reason=reason[:500],
        )
        return NewsSourceFetchOutcome(
            source_name=source.name,
            domain=source.domain,
            fetch_kind=source.fetch_kind,
            status="failed",
            items_found=len(batch.items),
            articles_inserted=0,
            articles_updated=0,
            skipped_no_date=batch.skipped_no_date,
            skipped_out_of_window=batch.skipped_out_of_window,
            raw_reference=str(batch.raw_body_path) if batch.raw_body_path else None,
            error=str(exc)[:500],
        )

    record_news_source_health(
        database_path,
        source_id=source.id,
        health="healthy",
        latest_item_at=latest_item_at,
        actor=actor,
    )
    return NewsSourceFetchOutcome(
        source_name=source.name,
        domain=source.domain,
        fetch_kind=source.fetch_kind,
        status="no_items" if not articles else "success",
        items_found=len(batch.items),
        articles_inserted=result.articles_inserted,
        articles_updated=result.articles_updated,
        skipped_no_date=batch.skipped_no_date,
        skipped_out_of_window=batch.skipped_out_of_window,
        raw_reference=str(batch.raw_body_path) if batch.raw_body_path else None,
        error=None,
    )
