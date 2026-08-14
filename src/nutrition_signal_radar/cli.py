from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .config import ConfigError, load_settings
from .demo_pipeline import build_demo_topics
from .doctor import build_doctor_report, render_doctor_report
from .ingest_service import import_article_batch
from .schema import initialize_schema, inspect_schema
from .web_app import run_demo_server
from .we_mprss_gateway import (
    DEFAULT_TRACKED_ACCOUNTS,
    WeMpRssImportError,
    load_we_mprss_batch,
    validate_we_mprss_source,
)
from .xlsx_gateway import XlsxImportError, load_xlsx_batch


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nutrition and functional-food industry signal radar")
    parser.add_argument(
        "--project-root",
        type=Path,
        help="Extension root containing .env (defaults to the installed project root).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="Inspect local prerequisites without calling external services.")
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    database = commands.add_parser("db", help="Initialize or inspect the extension SQLite database.")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    database_init = database_commands.add_parser("init", help="Apply all pending database migrations.")
    database_init.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    database_status = database_commands.add_parser("status", help="Inspect schema status without modifying the database.")
    database_status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    demo = commands.add_parser("demo", help="Build a disposable demo from a local read-only data source.")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    demo_import = demo_commands.add_parser(
        "import-xlsx", help="Import a P&B-style workbook into the isolated demo database."
    )
    demo_import.add_argument("--input", type=Path, required=True, help="Read-only .xlsx input path.")
    demo_import.add_argument("--sheet", default="数据列表", help="Worksheet name (default: 数据列表).")
    demo_import.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    demo_build = demo_commands.add_parser(
        "build", help="Aggregate imported articles into explainable demo signals and topic candidates."
    )
    demo_build.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    demo_serve = demo_commands.add_parser(
        "serve", help="Serve the isolated local demo board on 127.0.0.1."
    )
    demo_serve.add_argument("--port", type=int, default=8765, help="Local port (default: 8765).")
    demo_serve.add_argument("--open", action="store_true", help="Open the board in the default browser.")

    we_mprss = commands.add_parser(
        "we-mp-rss",
        help="Validate and import articles from the isolated local we-mp-rss runtime.",
    )
    we_mprss_commands = we_mprss.add_subparsers(dest="we_mprss_command", required=True)
    we_mprss_validate = we_mprss_commands.add_parser(
        "validate", help="Read the local source DB and report coverage for the tracked accounts."
    )
    we_mprss_validate.add_argument(
        "--source-db", type=Path, help="Read-only we-mp-rss SQLite path (defaults to the native pilot DB)."
    )
    we_mprss_validate.add_argument(
        "--account", action="append", dest="accounts", help="Tracked account name; repeat to override the five defaults."
    )
    we_mprss_validate.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    we_mprss_import = we_mprss_commands.add_parser(
        "import", help="Import a validated local source DB into the isolated WeRSS pilot board."
    )
    we_mprss_import.add_argument(
        "--source-db", type=Path, help="Read-only we-mp-rss SQLite path (defaults to the native pilot DB)."
    )
    we_mprss_import.add_argument(
        "--account", action="append", dest="accounts", help="Tracked account name; repeat to override the five defaults."
    )
    we_mprss_import.add_argument(
        "--allow-partial", action="store_true", help="Allow import when one or more requested accounts have not synced yet."
    )
    we_mprss_import.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    we_mprss_build = we_mprss_commands.add_parser(
        "build", help="Aggregate imported WeRSS articles into explainable rule-based pilot signals."
    )
    we_mprss_build.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    we_mprss_serve = we_mprss_commands.add_parser(
        "serve", help="Serve the isolated local WeRSS pilot board on 127.0.0.1."
    )
    we_mprss_serve.add_argument("--port", type=int, default=8766, help="Local port (default: 8766).")
    we_mprss_serve.add_argument("--open", action="store_true", help="Open the board in the default browser.")

    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.project_root)
    except ConfigError as exc:
        parser.error(str(exc))

    if args.command == "doctor":
        report = build_doctor_report(settings)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(render_doctor_report(report))
        return 0 if report["capabilities"]["scaffold_ready"] else 1

    if args.command == "db" and args.database_command == "init":
        result = initialize_schema(settings.database_path)
        payload = {
            "database_path": str(settings.database_path),
            "starting_version": result.starting_version,
            "ending_version": result.ending_version,
            "applied_versions": list(result.applied_versions),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            applied = ", ".join(str(version) for version in result.applied_versions) or "none"
            print(f"Database: {settings.database_path}")
            print(f"Schema version: {result.ending_version}; applied now: {applied}")
        return 0

    if args.command == "db" and args.database_command == "status":
        status = inspect_schema(settings.database_path)
        payload = {
            "database_path": str(settings.database_path),
            "state": status.state,
            "current_version": status.current_version,
            "latest_version": status.latest_version,
            "detail": status.detail,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Database: {settings.database_path}")
            print(f"State: {status.state}; {status.detail}")
        return 0 if status.state in {"not_initialized", "up_to_date"} else 1

    if args.command == "demo" and args.demo_command == "import-xlsx":
        try:
            batch = load_xlsx_batch(args.input, sheet_name=args.sheet)
            result = import_article_batch(settings.demo_database_path, batch)
        except XlsxImportError as exc:
            print(f"Demo import failed: {exc}", file=sys.stderr)
            return 1

        payload = {
            "database_path": str(result.database_path),
            "cycle_id": result.cycle_id,
            "cycle_slug": result.cycle_slug,
            "source_path": result.source_path,
            "source_fingerprint": result.source_fingerprint,
            "source_rows": result.source_rows,
            "normalized_articles": result.normalized_articles,
            "collapsed_rows": result.collapsed_rows,
            "skipped_rows": result.skipped_rows,
            "accounts_created": result.accounts_created,
            "articles_inserted": result.articles_inserted,
            "articles_updated": result.articles_updated,
            "total_accounts": result.total_accounts,
            "total_articles": result.total_articles,
            "in_period_articles": result.in_period_articles,
            "context_articles": result.context_articles,
            "issues": [
                {"row_number": issue.row_number, "reason": issue.reason}
                for issue in batch.issues[:20]
            ],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Demo database: {result.database_path}")
            print(f"Read-only input: {result.source_path}")
            print(
                "Rows: "
                f"{result.source_rows}; normalized: {result.normalized_articles}; "
                f"duplicates collapsed: {result.collapsed_rows}; skipped: {result.skipped_rows}"
            )
            print(
                f"Accounts: {result.total_accounts} total ({result.accounts_created} created); "
                f"articles: {result.total_articles} total "
                f"({result.articles_inserted} inserted, {result.articles_updated} updated)"
            )
        return 0

    if args.command == "demo" and args.demo_command == "build":
        try:
            result = build_demo_topics(settings.demo_database_path)
        except ValueError as exc:
            print(f"Demo build failed: {exc}", file=sys.stderr)
            return 1
        payload = {
            "database_path": str(result.database_path),
            "cycle_id": result.cycle_id,
            "cycle_slug": result.cycle_slug,
            "source_articles": result.source_articles,
            "clusters": result.clusters,
            "multi_article_clusters": result.multi_article_clusters,
            "signals": result.signals,
            "candidates": result.candidates,
            "locked_clusters_preserved": result.locked_clusters_preserved,
            "stale_clusters_removed": result.stale_clusters_removed,
            "generator": result.generator,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Demo database: {result.database_path}")
            print(
                f"Articles: {result.source_articles}; clusters: {result.clusters} "
                f"({result.multi_article_clusters} multi-article)"
            )
            print(
                f"Signals: {result.signals}; candidates: {result.candidates}; "
                f"locked clusters preserved: {result.locked_clusters_preserved}; "
                f"stale demo clusters removed: {result.stale_clusters_removed}"
            )
            print(f"Generator: {result.generator} (deterministic demo rules; no LLM call)")
        return 0

    if args.command == "demo" and args.demo_command == "serve":
        if not 1 <= args.port <= 65535:
            print("Demo server port must be between 1 and 65535", file=sys.stderr)
            return 1
        try:
            run_demo_server(
                settings.demo_database_path,
                port=args.port,
                open_browser=args.open,
            )
        except (FileNotFoundError, OSError) as exc:
            print(f"Demo server failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "we-mp-rss":
        source_database = (args.source_db or settings.native_we_mprss_database_path).resolve() if args.we_mprss_command in {"validate", "import"} else None
        accounts = tuple(args.accounts or DEFAULT_TRACKED_ACCOUNTS) if args.we_mprss_command in {"validate", "import"} else ()

        if args.we_mprss_command == "validate":
            try:
                report = validate_we_mprss_source(source_database, accounts=accounts)
            except WeMpRssImportError as exc:
                print(f"WeRSS validation failed: {exc}", file=sys.stderr)
                return 1
            payload = report.as_dict()
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"Read-only WeRSS source: {payload['source_path']}")
                print(
                    f"Matched accounts: {payload['matched_accounts']}/{len(report.requested_accounts)}; "
                    f"eligible articles: {payload['source_article_count']}"
                )
                for health in report.accounts:
                    print(
                        f"- {health.account_name}: {health.status}; {health.article_count} articles; "
                        f"body coverage {health.body_coverage:.0%}; {health.message}"
                    )
            return 0 if report.ready_for_import else 1

        if args.we_mprss_command == "import":
            try:
                report = validate_we_mprss_source(source_database, accounts=accounts)
                if not report.ready_for_import and not args.allow_partial:
                    print(
                        "WeRSS import stopped: all requested accounts must be matched and synced first; "
                        "run `we-mp-rss validate` or pass --allow-partial for an explicit partial pilot.",
                        file=sys.stderr,
                    )
                    return 1
                batch = load_we_mprss_batch(source_database, accounts=accounts)
                if not batch.articles:
                    print("WeRSS import stopped: no eligible articles were found.", file=sys.stderr)
                    return 1
                result = import_article_batch(settings.we_mprss_pilot_database_path, batch)
            except WeMpRssImportError as exc:
                print(f"WeRSS import failed: {exc}", file=sys.stderr)
                return 1
            payload = {
                "database_path": str(result.database_path),
                "cycle_id": result.cycle_id,
                "cycle_slug": result.cycle_slug,
                "source_path": result.source_path,
                "source_fingerprint": result.source_fingerprint,
                "source_rows": result.source_rows,
                "normalized_articles": result.normalized_articles,
                "collapsed_rows": result.collapsed_rows,
                "skipped_rows": result.skipped_rows,
                "accounts_created": result.accounts_created,
                "articles_inserted": result.articles_inserted,
                "articles_updated": result.articles_updated,
                "total_accounts": result.total_accounts,
                "total_articles": result.total_articles,
                "in_period_articles": result.in_period_articles,
                "context_articles": result.context_articles,
                "validation": report.as_dict(),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"WeRSS pilot database: {result.database_path}")
                print(
                    f"Articles: {result.total_articles} total "
                    f"({result.articles_inserted} inserted, {result.articles_updated} updated)"
                )
            return 0

        if args.we_mprss_command == "build":
            try:
                result = build_demo_topics(settings.we_mprss_pilot_database_path)
            except ValueError as exc:
                print(f"WeRSS pilot build failed: {exc}", file=sys.stderr)
                return 1
            payload = {
                "database_path": str(result.database_path),
                "cycle_id": result.cycle_id,
                "cycle_slug": result.cycle_slug,
                "source_articles": result.source_articles,
                "clusters": result.clusters,
                "multi_article_clusters": result.multi_article_clusters,
                "signals": result.signals,
                "candidates": result.candidates,
                "locked_clusters_preserved": result.locked_clusters_preserved,
                "stale_clusters_removed": result.stale_clusters_removed,
                "generator": result.generator,
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    f"WeRSS pilot: {result.source_articles} articles; {result.clusters} clusters; "
                    f"{result.candidates} topic candidates."
                )
                print("Generator: deterministic rules only; no LLM call.")
            return 0

        if args.we_mprss_command == "serve":
            if not 1 <= args.port <= 65535:
                print("WeRSS pilot server port must be between 1 and 65535", file=sys.stderr)
                return 1
            try:
                run_demo_server(
                    settings.we_mprss_pilot_database_path,
                    port=args.port,
                    open_browser=args.open,
                )
            except (FileNotFoundError, OSError) as exc:
                print(f"WeRSS pilot server failed: {exc}", file=sys.stderr)
                return 1
            return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
