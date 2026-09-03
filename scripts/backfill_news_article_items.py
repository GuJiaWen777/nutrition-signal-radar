"""One-time v17 backfill: link historical news builds to their news_items.

Schema v17 added ``news_article_items`` so the compose layer can exclude items
consumed by past issues.  Builds made before v17 recorded their items only in
each version's ``manifest.json``; without a backfill those consumed items look
unused and can be re-published.  This script walks every ``news_articles`` row,
reads its manifest, maps manifest items onto ``news_items`` rows (by
``article_id`` + URL, falling back to normalized title) and inserts the missing
junction rows with the article's own ``created_at`` so the day-based exclusion
treats them as consumed at build time.  Idempotent: ``INSERT OR IGNORE``.

Usage:
    python scripts/backfill_news_article_items.py            # report only
    python scripts/backfill_news_article_items.py --write    # apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nutrition_signal_radar.database import connect_database  # noqa: E402
from nutrition_signal_radar.news_selection import normalize_news_title  # noqa: E402
from nutrition_signal_radar.schema import initialize_schema  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Apply inserts (default: report only).")
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()

    database_path = args.project_root / "var" / "radar.sqlite3"
    initialize_schema(database_path)
    connection = connect_database(database_path)
    inserted = 0
    unmatched: list[dict[str, str]] = []
    try:
        articles = connection.execute(
            "SELECT id, slug, version, relative_path, created_at FROM news_articles ORDER BY id"
        ).fetchall()
        for article in articles:
            manifest_path = Path(article["relative_path"]) / "manifest.json"
            if not manifest_path.is_file():
                print(f"skip (no manifest): {article['slug']} v{article['version']}")
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in manifest.get("items") or []:
                news_item_id = _match_news_item(connection, item)
                if news_item_id is None:
                    unmatched.append(
                        {"slug": str(article["slug"]), "title": str(item.get("title") or "")}
                    )
                    continue
                exists = connection.execute(
                    "SELECT 1 FROM news_article_items WHERE article_id = ? AND news_item_id = ?",
                    (article["id"], news_item_id),
                ).fetchone()
                if exists is not None:
                    continue
                if args.write:
                    connection.execute(
                        "INSERT OR IGNORE INTO news_article_items(article_id, news_item_id, created_at) VALUES (?, ?, ?)",
                        (article["id"], news_item_id, article["created_at"]),
                    )
                inserted += 1
            print(
                f"{'wrote' if args.write else 'found'} {inserted} new link(s) so far — at "
                f"{article['slug']} v{article['version']}"
            )
    finally:
        connection.close()

    total = (
        f"{'inserted' if args.write else 'would insert'} {inserted} junction row(s)"
        if not unmatched
        else f"{'inserted' if args.write else 'would insert'} {inserted}; {len(unmatched)} manifest item(s) unmatched"
    )
    print(total)
    for row in unmatched:
        print(f"  unmatched: {row['slug']}: {row['title'][:60]}")
    return 0


def _match_news_item(connection, item: dict) -> int | None:
    article_id = item.get("article_id")
    url = str(item.get("url") or "").strip()
    if article_id is not None:
        if url:
            row = connection.execute(
                "SELECT id FROM news_items WHERE article_id = ? AND url = ?",
                (int(article_id), url),
            ).fetchone()
            if row is not None:
                return int(row["id"])
        row = connection.execute(
            "SELECT id FROM news_items WHERE article_id = ?", (int(article_id),)
        ).fetchone()
        if row is not None:
            return int(row["id"])
    title = normalize_news_title(str(item.get("title") or ""))
    row = connection.execute(
        "SELECT id FROM news_items WHERE title = ? OR ? = '' ORDER BY id DESC LIMIT 1",
        (str(item.get("title") or ""), title),
    ).fetchone()
    return int(row["id"]) if row is not None else None


if __name__ == "__main__":
    raise SystemExit(main())
