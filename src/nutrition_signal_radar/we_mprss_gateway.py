"""Read-only adapter from a local we-mp-rss SQLite database.

``we-mp-rss`` owns acquisition and its own local database.  The topic radar
never logs into WeChat or writes to that database; it reads a stable subset of
the feed/article tables and emits the existing source-neutral article contract.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .article_contract import ArticleBatch, NormalizedArticle, RowIssue, canonicalize_article_url


DEFAULT_TRACKED_ACCOUNTS = (
    "庶正康讯",
    "营养健康评论",
    "健康产业观察",
    "营养品观察",
    "功能食品圈",
)
_DELETED_ARTICLE_STATUS = 1000
_CST = timezone(timedelta(hours=8))


class WeMpRssImportError(ValueError):
    """Raised when the local we-mp-rss source is missing or incompatible."""


@dataclass(frozen=True)
class WeMpRssAccountHealth:
    """Acquisition coverage for one requested public account."""

    account_name: str
    status: str
    feed_ids: tuple[str, ...]
    article_count: int
    latest_published_at: str | None
    articles_with_body: int
    body_coverage: float
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "account_name": self.account_name,
            "status": self.status,
            "feed_ids": list(self.feed_ids),
            "article_count": self.article_count,
            "latest_published_at": self.latest_published_at,
            "articles_with_body": self.articles_with_body,
            "body_coverage": self.body_coverage,
            "message": self.message,
        }


@dataclass(frozen=True)
class WeMpRssValidationReport:
    """Non-mutating account and article coverage report for a local source DB."""

    source_path: Path
    source_fingerprint: str
    requested_accounts: tuple[str, ...]
    source_feed_count: int
    source_article_count: int
    accounts: tuple[WeMpRssAccountHealth, ...]

    @property
    def matched_accounts(self) -> int:
        return sum(health.status == "matched" for health in self.accounts)

    @property
    def ready_for_import(self) -> bool:
        return bool(self.accounts) and all(
            health.status == "matched" and health.article_count > 0
            for health in self.accounts
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "source_path": str(self.source_path),
            "source_fingerprint": self.source_fingerprint,
            "requested_accounts": list(self.requested_accounts),
            "source_feed_count": self.source_feed_count,
            "source_article_count": self.source_article_count,
            "matched_accounts": self.matched_accounts,
            "ready_for_import": self.ready_for_import,
            "accounts": [health.as_dict() for health in self.accounts],
        }


def validate_we_mprss_source(
    database_path: Path,
    *,
    accounts: Iterable[str] = DEFAULT_TRACKED_ACCOUNTS,
) -> WeMpRssValidationReport:
    """Inspect the local we-mp-rss DB without changing either database."""

    source_path = _require_database(database_path)
    requested = _normalize_requested_accounts(accounts)
    feeds, rows = _read_source(source_path)
    selection = {_normalize_account_name(name): name for name in requested}
    feeds_by_account: dict[str, list[sqlite3.Row]] = {key: [] for key in selection}
    rows_by_account: dict[str, list[sqlite3.Row]] = {key: [] for key in selection}

    for feed in feeds:
        normalized = _normalize_account_name(feed["mp_name"])
        if normalized in feeds_by_account:
            feeds_by_account[normalized].append(feed)
    for row in rows:
        normalized = _normalize_account_name(row["mp_name"])
        if normalized in rows_by_account:
            rows_by_account[normalized].append(row)

    health_rows: list[WeMpRssAccountHealth] = []
    for account_name in requested:
        normalized = _normalize_account_name(account_name)
        matching_feeds = feeds_by_account[normalized]
        matching_rows = rows_by_account[normalized]
        enabled_feed_ids = tuple(
            str(feed["feed_id"])
            for feed in matching_feeds
            if _is_enabled_feed(feed)
        )
        body_count = sum(_body_state(row)[0] == "full" for row in matching_rows)
        latest = max(
            (_published_at(row["publish_time"] or row["create_time"]) for row in matching_rows),
            default=None,
        )
        article_count = len(matching_rows)

        if not matching_feeds:
            status = "not_found"
            message = "该名称尚未通过 we-mp-rss 的公众号搜索/添加确认。"
        elif not enabled_feed_ids:
            status = "disabled"
            message = "公众号已存在于 we-mp-rss，但当前未启用采集。"
        elif article_count == 0:
            status = "pending_sync"
            message = "公众号已添加，但尚未成功写入文章；请先在 we-mp-rss 执行更新。"
        else:
            status = "matched"
            message = "已匹配并有可导入文章。"

        health_rows.append(
            WeMpRssAccountHealth(
                account_name=account_name,
                status=status,
                feed_ids=enabled_feed_ids,
                article_count=article_count,
                latest_published_at=latest,
                articles_with_body=body_count,
                body_coverage=round(body_count / article_count, 4) if article_count else 0.0,
                message=message,
            )
        )

    selected_rows = [
        row
        for row in rows
        if _normalize_account_name(row["mp_name"]) in selection and _is_enabled_feed(row)
    ]
    return WeMpRssValidationReport(
        source_path=source_path,
        source_fingerprint=_fingerprint_database(source_path),
        requested_accounts=requested,
        source_feed_count=sum(
            _normalize_account_name(feed["mp_name"]) in selection for feed in feeds
        ),
        source_article_count=len(selected_rows),
        accounts=tuple(health_rows),
    )


def load_we_mprss_batch(
    database_path: Path,
    *,
    accounts: Iterable[str] = DEFAULT_TRACKED_ACCOUNTS,
) -> ArticleBatch:
    """Convert enabled, requested WeRSS articles into the radar contract.

    This function opens the source in SQLite's read-only mode.  It does not
    call WeChat, does not access the WeRSS API, and does not write source data.
    """

    source_path = _require_database(database_path)
    requested = _normalize_requested_accounts(accounts)
    selected_accounts = {_normalize_account_name(name): name for name in requested}
    _, rows = _read_source(source_path)
    issues: list[RowIssue] = []
    articles: list[NormalizedArticle] = []
    source_rows = 0

    for row_number, row in enumerate(rows, start=1):
        normalized_account = _normalize_account_name(row["mp_name"])
        if normalized_account not in selected_accounts or not _is_enabled_feed(row):
            continue
        source_rows += 1
        article_id = _clean_text(row["article_id"])
        title = _clean_text(row["title"])
        if not article_id:
            issues.append(RowIssue(row_number=row_number, reason="missing we-mp-rss article id"))
            continue
        if not title:
            issues.append(RowIssue(row_number=row_number, reason="missing title"))
            continue

        body_state, content_snapshot = _body_state(row)
        canonical_url = canonicalize_article_url(_clean_text(row["url"]) or None)
        content_hash = _content_hash(content_snapshot) if body_state != "missing" else None
        metrics = _extract_metrics(row["publish_info"])
        articles.append(
            NormalizedArticle(
                external_id=f"we-mp-rss:{_clean_text(row['feed_id'])}:{article_id}",
                account_name=selected_accounts[normalized_account],
                title=title,
                published_at=_published_at(row["publish_time"] or row["create_time"]),
                url=canonical_url,
                content_snapshot=content_snapshot or title,
                content_hash=content_hash,
                read_count=metrics["read_count"],
                like_count=metrics["like_count"],
                share_count=metrics["share_count"],
                recommend_count=metrics["recommend_count"],
                metadata={
                    "source_kind": "we-mp-rss",
                    "we_mprss_feed_id": _clean_text(row["feed_id"]),
                    "we_mprss_article_id": article_id,
                    "body_state": body_state,
                    "we_mprss_has_content": bool(row["has_content"]),
                },
                source_reference=f"we-mp-rss:{_clean_text(row['feed_id'])}:{article_id}",
            )
        )

    deduplicated, collapsed = _collapse_articles(articles)
    return ArticleBatch(
        source_kind="we-mp-rss",
        source_path=str(source_path),
        source_fingerprint=_fingerprint_database(source_path),
        source_rows=source_rows,
        articles=tuple(deduplicated),
        skipped_rows=len(issues),
        collapsed_rows=collapsed,
        issues=tuple(issues),
        account_segment="industry_media",
    )


def _require_database(database_path: Path) -> Path:
    source_path = Path(database_path).expanduser().resolve()
    if not source_path.is_file():
        raise WeMpRssImportError(f"we-mp-rss source database does not exist: {source_path}")
    return source_path


def _read_source(source_path: Path) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    connection = _open_readonly(source_path)
    try:
        _assert_source_schema(connection)
        feeds = connection.execute(
            """
            SELECT id AS feed_id, mp_name, status AS feed_status
            FROM feeds
            ORDER BY mp_name, id
            """
        ).fetchall()
        rows = connection.execute(
            """
            SELECT
                f.id AS feed_id,
                f.mp_name AS mp_name,
                f.status AS feed_status,
                a.id AS article_id,
                a.title AS title,
                a.url AS url,
                a.description AS description,
                a.publish_time AS publish_time,
                a.create_time AS create_time,
                a.content AS content,
                a.content_html AS content_html,
                a.publish_info AS publish_info,
                a.has_content AS has_content
            FROM feeds f
            JOIN articles a ON a.mp_id = f.id
            WHERE COALESCE(a.status, 1) != ?
            ORDER BY a.publish_time DESC, a.create_time DESC, a.id
            """,
            (_DELETED_ARTICLE_STATUS,),
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise WeMpRssImportError(f"could not read we-mp-rss source database: {exc}") from exc
    finally:
        connection.close()
    return feeds, rows


def _open_readonly(source_path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            f"{source_path.as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
    except sqlite3.Error as exc:
        raise WeMpRssImportError(f"could not open we-mp-rss source database: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _assert_source_schema(connection: sqlite3.Connection) -> None:
    required = {
        "feeds": {"id", "mp_name", "status"},
        "articles": {
            "id", "mp_id", "title", "url", "description", "publish_time",
            "create_time", "content", "content_html", "publish_info", "has_content", "status",
        },
    }
    for table, columns in required.items():
        found = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        missing = sorted(columns - found)
        if missing:
            raise WeMpRssImportError(
                f"we-mp-rss source table {table!r} is missing required columns: {', '.join(missing)}"
            )


def _normalize_requested_accounts(accounts: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in accounts:
        value = _clean_text(raw)
        normalized = _normalize_account_name(value)
        if not value or normalized in seen:
            continue
        output.append(value)
        seen.add(normalized)
    if not output:
        raise WeMpRssImportError("at least one public account name is required")
    return tuple(output)


def _normalize_account_name(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", _clean_text(value)).casefold()
    return re.sub(r"\s+", "", normalized)


def _is_enabled_feed(row: Mapping[str, object]) -> bool:
    value = row["feed_status"]
    return value is None or int(value) == 1


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _body_state(row: Mapping[str, object]) -> tuple[str, str]:
    content = _clean_text(row["content"])
    if content:
        return "full", content
    content_html = _clean_text(row["content_html"])
    if content_html:
        return "full", content_html
    description = _clean_text(row["description"])
    if description:
        return "summary", description
    return "missing", ""


def _published_at(value: object) -> str | None:
    try:
        timestamp = int(float(str(value)))
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    if timestamp > 100_000_000_000:
        timestamp //= 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(_CST).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _extract_metrics(raw_value: object) -> dict[str, int]:
    values = {
        "read_count": 0,
        "like_count": 0,
        "share_count": 0,
        "recommend_count": 0,
    }
    try:
        payload = json.loads(str(raw_value)) if raw_value else {}
    except (TypeError, json.JSONDecodeError):
        return values

    aliases = {
        "read_count": {"readnum", "readcount", "readnumber", "readcnt"},
        "like_count": {"likenum", "likecount", "likenumber", "likecnt"},
        "share_count": {"sharenum", "sharecount", "sharenumber", "sharecnt"},
        "recommend_count": {"recommendnum", "recommendcount", "recommendnumber", "recommendcnt"},
    }
    for key, value in _walk_metric_values(payload):
        normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
        for target, names in aliases.items():
            if normalized_key in names:
                values[target] = max(values[target], _as_nonnegative_int(value))
    return values


def _walk_metric_values(value: object) -> Iterable[tuple[str, object]]:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            yield str(key), nested
            yield from _walk_metric_values(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            yield from _walk_metric_values(nested)


def _as_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(float(str(value))))
    except (TypeError, ValueError):
        return 0


def _collapse_articles(
    articles: Iterable[NormalizedArticle],
) -> tuple[list[NormalizedArticle], int]:
    selected: dict[str, NormalizedArticle] = {}
    collapsed = 0
    for article in articles:
        key = article.url or article.external_id
        current = selected.get(key)
        if current is None:
            selected[key] = article
            continue
        collapsed += 1
        selected[key] = NormalizedArticle(
            external_id=current.external_id,
            account_name=current.account_name,
            title=current.title if len(current.title) >= len(article.title) else article.title,
            published_at=current.published_at or article.published_at,
            url=current.url or article.url,
            content_snapshot=(
                current.content_snapshot
                if len(current.content_snapshot) >= len(article.content_snapshot)
                else article.content_snapshot
            ),
            content_hash=current.content_hash or article.content_hash,
            read_count=max(current.read_count, article.read_count),
            like_count=max(current.like_count, article.like_count),
            share_count=max(current.share_count, article.share_count),
            recommend_count=max(current.recommend_count, article.recommend_count),
            metadata=current.metadata,
            source_reference=current.source_reference,
        )
    return list(selected.values()), collapsed


def _fingerprint_database(source_path: Path) -> str:
    digest = hashlib.sha256()
    with source_path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    stat = source_path.stat()
    return f"sha256:{digest.hexdigest()}:{stat.st_size}:{stat.st_mtime_ns}"
