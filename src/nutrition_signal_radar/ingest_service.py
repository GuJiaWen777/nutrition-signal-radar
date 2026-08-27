from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .article_contract import ArticleBatch, NormalizedArticle
from .cycle_service import attach_article_to_cycle, ensure_import_cycle
from .database import connect_database, transaction
from .schema import initialize_schema


SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


class ArticleIdentityConflict(RuntimeError):
    """Raised when external ID and URL point at two different stored articles."""


@dataclass(frozen=True)
class BatchImportResult:
    database_path: Path
    source_path: str
    source_fingerprint: str
    source_rows: int
    normalized_articles: int
    collapsed_rows: int
    skipped_rows: int
    accounts_created: int
    articles_inserted: int
    articles_updated: int
    total_accounts: int
    total_articles: int
    cycle_id: int | None
    cycle_slug: str | None
    in_period_articles: int
    context_articles: int


def import_article_batch(database_path: Path, batch: ArticleBatch) -> BatchImportResult:
    """Persist a source-neutral batch transactionally and idempotently."""
    initialize_schema(database_path)
    connection = connect_database(database_path)
    accounts_created = 0
    articles_inserted = 0
    articles_updated = 0
    cycle_id: int | None = None
    cycle_slug: str | None = None
    in_period_articles = 0
    context_articles = 0
    try:
        with transaction(connection):
            cycle = ensure_import_cycle(
                connection,
                source_kind=batch.source_kind,
                published_at_values=(article.published_at for article in batch.articles),
            )
            if cycle is not None:
                cycle_id = int(cycle["id"])
                cycle_slug = str(cycle["slug"])
            account_ids: dict[str, int] = {}
            latest_by_account: dict[str, str | None] = {}
            for article in batch.articles:
                current = latest_by_account.get(article.account_name)
                if article.published_at and (current is None or article.published_at > current):
                    latest_by_account[article.account_name] = article.published_at
                else:
                    latest_by_account.setdefault(article.account_name, current)

            for account_name in sorted(latest_by_account):
                existed = connection.execute(
                    "SELECT id FROM source_accounts WHERE name = ?", (account_name,)
                ).fetchone()
                connection.execute(
                    f"""
                    INSERT INTO source_accounts(
                        name, segment, validation_status, sync_health,
                        last_synced_at, last_article_at
                    ) VALUES (?, ?, 'matched', 'healthy', ({SQL_NOW}), ?)
                    ON CONFLICT(name) DO UPDATE SET
                        sync_health = 'healthy',
                        last_synced_at = ({SQL_NOW}),
                        last_article_at = CASE
                            WHEN excluded.last_article_at IS NULL THEN source_accounts.last_article_at
                            WHEN source_accounts.last_article_at IS NULL THEN excluded.last_article_at
                            WHEN excluded.last_article_at > source_accounts.last_article_at
                                THEN excluded.last_article_at
                            ELSE source_accounts.last_article_at
                        END,
                        updated_at = ({SQL_NOW})
                    """,
                    (account_name, batch.account_segment, latest_by_account[account_name]),
                )
                account_id = connection.execute(
                    "SELECT id FROM source_accounts WHERE name = ?", (account_name,)
                ).fetchone()["id"]
                account_ids[account_name] = int(account_id)
                if existed is None:
                    accounts_created += 1

            for article in batch.articles:
                existing_id = _find_existing_article(connection, article)
                payload = _article_payload(article, account_ids[article.account_name])
                if existing_id is None:
                    cursor = connection.execute(
                        f"""
                        INSERT INTO source_articles(
                            external_id, account_id, title, published_at, url,
                            content_snapshot, content_hash, raw_snapshot_path,
                            metrics_json, read_count, like_count, share_count,
                            recommend_count, fetched_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ({SQL_NOW}), ({SQL_NOW}))
                        """,
                        payload,
                    )
                    article_id = int(cursor.lastrowid)
                    articles_inserted += 1
                else:
                    connection.execute(
                        f"""
                        UPDATE source_articles SET
                            account_id = ?,
                            title = ?,
                            published_at = COALESCE(?, published_at),
                            url = COALESCE(?, url),
                            content_snapshot = CASE
                                WHEN length(?) >= length(content_snapshot) THEN ?
                                ELSE content_snapshot
                            END,
                            content_hash = CASE
                                WHEN length(?) >= length(content_snapshot) THEN ?
                                ELSE content_hash
                            END,
                            raw_snapshot_path = COALESCE(?, raw_snapshot_path),
                            metrics_json = ?,
                            read_count = MAX(read_count, ?),
                            like_count = MAX(like_count, ?),
                            share_count = MAX(share_count, ?),
                            recommend_count = MAX(recommend_count, ?),
                            fetched_at = ({SQL_NOW}),
                            updated_at = ({SQL_NOW})
                        WHERE id = ?
                        """,
                        (
                            payload[1], payload[2], payload[3], payload[4],
                            payload[5], payload[5], payload[5], payload[6],
                            payload[7], payload[8], payload[9], payload[10],
                            payload[11], payload[12], existing_id,
                        ),
                    )
                    article_id = existing_id
                    articles_updated += 1
                if cycle is not None:
                    role = attach_article_to_cycle(
                        connection,
                        cycle,
                        article_id,
                        article.published_at,
                    )
                    if role == "in_period":
                        in_period_articles += 1
                    else:
                        context_articles += 1

            total_accounts = int(connection.execute("SELECT COUNT(*) FROM source_accounts").fetchone()[0])
            total_articles = int(connection.execute("SELECT COUNT(*) FROM source_articles").fetchone()[0])
    finally:
        connection.close()

    return BatchImportResult(
        database_path=database_path.resolve(),
        source_path=batch.source_path,
        source_fingerprint=batch.source_fingerprint,
        source_rows=batch.source_rows,
        normalized_articles=len(batch.articles),
        collapsed_rows=batch.collapsed_rows,
        skipped_rows=batch.skipped_rows,
        accounts_created=accounts_created,
        articles_inserted=articles_inserted,
        articles_updated=articles_updated,
        total_accounts=total_accounts,
        total_articles=total_articles,
        cycle_id=cycle_id,
        cycle_slug=cycle_slug,
        in_period_articles=in_period_articles,
        context_articles=context_articles,
    )


def _find_existing_article(
    connection: sqlite3.Connection, article: NormalizedArticle
) -> int | None:
    clauses = ["external_id = ?"]
    values: list[str] = [article.external_id]
    if article.url:
        clauses.append("url = ?")
        values.append(article.url)
    rows = connection.execute(
        f"SELECT id FROM source_articles WHERE {' OR '.join(clauses)} ORDER BY id",
        values,
    ).fetchall()
    ids = {int(row["id"]) for row in rows}
    if len(ids) > 1:
        raise ArticleIdentityConflict(
            f"External ID and canonical URL resolve to different articles: {sorted(ids)}"
        )
    return next(iter(ids), None)


def _article_payload(article: NormalizedArticle, account_id: int) -> tuple[object, ...]:
    return (
        article.external_id,
        account_id,
        article.title,
        article.published_at,
        article.url,
        article.content_snapshot,
        article.content_hash,
        article.source_reference,
        json.dumps(article.metadata, ensure_ascii=False, sort_keys=True),
        article.read_count,
        article.like_count,
        article.share_count,
        article.recommend_count,
    )
