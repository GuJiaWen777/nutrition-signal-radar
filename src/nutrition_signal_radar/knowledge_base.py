"""Durable local knowledge-base snapshots of stored source articles.

The live knowledge base is the SQLite ``source_articles`` table itself
(normalized metadata plus the best available content snapshot).  These
commands turn that state into immutable, searchable local exports so the
knowledge base survives schema changes and can be inspected outside the app.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .database import connect_database
from .schema import initialize_schema


@dataclass(frozen=True)
class KnowledgeBaseStats:
    database_path: Path
    articles: int
    articles_with_body: int
    accounts: int
    earliest_published_at: str | None
    latest_published_at: str | None
    body_coverage: float

    def as_dict(self) -> dict[str, object]:
        return {
            "database_path": str(self.database_path),
            "articles": self.articles,
            "articles_with_body": self.articles_with_body,
            "accounts": self.accounts,
            "earliest_published_at": self.earliest_published_at,
            "latest_published_at": self.latest_published_at,
            "body_coverage": round(self.body_coverage, 4),
        }


@dataclass(frozen=True)
class KnowledgeBaseExportResult:
    database_path: Path
    version: int
    directory: Path
    manifest_path: Path
    articles_path: Path
    exported_articles: int
    with_body: int

    def as_dict(self) -> dict[str, object]:
        return {
            "database_path": str(self.database_path),
            "version": self.version,
            "directory": str(self.directory),
            "manifest_path": str(self.manifest_path),
            "articles_path": str(self.articles_path),
            "exported_articles": self.exported_articles,
            "with_body": self.with_body,
            "status": "immutable_local_snapshot",
        }


def knowledge_base_stats(database_path: Path) -> KnowledgeBaseStats:
    """Report how much normalized article content is already stored locally."""

    initialize_schema(database_path)
    connection = connect_database(Path(database_path).resolve(), readonly=True)
    try:
        row = connection.execute(
            """
            SELECT COUNT(*) AS articles,
                   SUM(CASE WHEN length(trim(a.content_snapshot)) > 0 THEN 1 ELSE 0 END) AS with_body,
                   COUNT(DISTINCT a.account_id) AS accounts,
                   MIN(a.published_at) AS first_at,
                   MAX(a.published_at) AS last_at
            FROM source_articles a
            """
        ).fetchone()
    finally:
        connection.close()
    articles = int(row["articles"])
    with_body = int(row["with_body"] or 0)
    return KnowledgeBaseStats(
        database_path=Path(database_path).resolve(),
        articles=articles,
        articles_with_body=with_body,
        accounts=int(row["accounts"]),
        earliest_published_at=row["first_at"],
        latest_published_at=row["last_at"],
        body_coverage=(with_body / articles) if articles else 0.0,
    )


def export_knowledge_base(
    database_path: Path,
    *,
    max_articles: int | None = None,
) -> KnowledgeBaseExportResult:
    """Write an immutable local snapshot of stored articles and bodies."""

    if max_articles is not None and max_articles < 1:
        raise ValueError("max_articles must be at least 1 when provided")
    initialize_schema(database_path)
    database = Path(database_path).resolve()
    connection = connect_database(database, readonly=True)
    try:
        rows = list(
            connection.execute(
                """
                SELECT a.id, a.external_id, a.title, a.published_at, a.url,
                       a.content_snapshot, a.content_hash, a.fetched_at, a.updated_at,
                       a.read_count, a.like_count, a.share_count, a.recommend_count,
                       a.raw_snapshot_path, sa.name AS account_name,
                       sa.redfox_name, sa.redfox_library
                FROM source_articles a
                JOIN source_accounts sa ON sa.id = a.account_id
                ORDER BY a.published_at IS NULL, a.published_at, a.id
                LIMIT ?
                """,
                (max_articles if max_articles is not None else -1,),
            )
        )
    finally:
        connection.close()

    root = database.parent / "knowledge-base"
    root.mkdir(parents=True, exist_ok=True)
    version = _next_version(root)
    final_directory = root / f"export-v{version:03d}"
    temporary_directory = Path(
        tempfile.mkdtemp(prefix=f".export-v{version:03d}-", dir=root)
    )
    exported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        articles_path = temporary_directory / "articles.jsonl"
        with_body = 0
        with articles_path.open("w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                has_body = bool(str(row["content_snapshot"] or "").strip())
                if has_body:
                    with_body += 1
                payload = {
                    "id": int(row["id"]),
                    "external_id": row["external_id"],
                    "account_name": row["account_name"],
                    "redfox_name": row["redfox_name"],
                    "redfox_library": row["redfox_library"],
                    "title": row["title"],
                    "published_at": row["published_at"],
                    "url": row["url"],
                    "content_snapshot": row["content_snapshot"],
                    "content_hash": row["content_hash"],
                    "fetched_at": row["fetched_at"],
                    "updated_at": row["updated_at"],
                    "metrics": {
                        "read_count": int(row["read_count"] or 0),
                        "like_count": int(row["like_count"] or 0),
                        "share_count": int(row["share_count"] or 0),
                        "recommend_count": int(row["recommend_count"] or 0),
                    },
                    "raw_snapshot_path": row["raw_snapshot_path"],
                    "has_body": has_body,
                    "evidence_role": "wechat_discovery",
                    "evidence_status": "needs_evidence",
                }
                stream.write(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
                )
        manifest = {
            "kind": "local_article_knowledge_base",
            "version": version,
            "exported_at": exported_at,
            "database_relative_path": database.name,
            "counts": {
                "articles": len(rows),
                "with_body": with_body,
                "body_coverage": round(with_body / len(rows), 4) if rows else 0.0,
            },
            "constraints": [
                "公众号文章仅作发现线索；事实性主张仍需回到一手来源补证。",
                "本快照只读且不覆盖旧版本；不触发任何微信公众号写入或发布动作。",
            ],
            "files": [
                {
                    "path": "articles.jsonl",
                    "sha256": _sha256(articles_path),
                    "bytes": articles_path.stat().st_size,
                }
            ],
        }
        manifest_path = temporary_directory / "manifest.json"
        _write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        temporary_directory.replace(final_directory)
    except Exception:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise

    return KnowledgeBaseExportResult(
        database_path=database,
        version=version,
        directory=final_directory,
        manifest_path=final_directory / "manifest.json",
        articles_path=final_directory / "articles.jsonl",
        exported_articles=len(rows),
        with_body=with_body,
    )


def _next_version(root: Path) -> int:
    if not root.exists():
        return 1
    versions = []
    for path in root.glob("export-v[0-9][0-9][0-9]"):
        try:
            versions.append(int(path.name.rsplit("v", 1)[1]))
        except (IndexError, ValueError):
            continue
    return max(versions, default=0) + 1


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
