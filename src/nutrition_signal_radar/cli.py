from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from .agent_gateway import (
    AgentGatewayError,
    PiAgentRequest,
    PiAgentRunner,
    build_pi_system_prompt,
    save_agent_exchange,
)
from .config import ConfigError, load_settings
from .cycle_service import MonitoringCycleError
from .demo_pipeline import build_demo_topics
from .doctor import build_doctor_report, render_doctor_report
from .ingest_service import import_article_batch
from .knowledge_base import export_knowledge_base, knowledge_base_stats
from .redfox_gateway import (
    DEFAULT_REDFOX_ACCOUNT_NAMES,
    RedFoxGateway,
    RedFoxGatewayError,
)
from .redfox_validation import validate_redfox_accounts, write_redfox_validation_report
from .redfox_wide_search import (
    search_redfox_wide_library,
    write_redfox_wide_search_report,
)
from .schema import initialize_schema, inspect_schema
from .source_registry import (
    SourceRegistryError,
    configure_source_account,
    configure_priority_site,
    list_source_accounts,
    list_priority_sites,
    seed_priority_sites,
    set_priority_site_enabled,
    set_source_account_enabled,
)
from .sync_service import backfill_missing_bodies, sync_enabled_redfox_sources
from .weekly_service import export_weekly_signal_brief
from .wechat_article_import import import_wechat_article
from .web_app import run_demo_server, run_radar_server
from .we_mprss_gateway import (
    DEFAULT_TRACKED_ACCOUNTS,
    WeMpRssImportError,
    load_we_mprss_batch,
    validate_we_mprss_source,
)
from .xlsx_gateway import XlsxImportError, load_xlsx_batch


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console_output()
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

    redfox = commands.add_parser(
        "redfox",
        help="Run explicitly authorized, read-only RedFox source validation.",
    )
    redfox_commands = redfox.add_subparsers(dest="redfox_command", required=True)
    redfox_validate = redfox_commands.add_parser(
        "validate",
        help="Probe account names and data coverage without importing or enabling any source.",
    )
    redfox_validate.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement before any external RedFox request is made.",
    )
    redfox_validate.add_argument(
        "--account",
        action="append",
        dest="accounts",
        help="Public account display name; repeat to replace the six default accounts.",
    )
    redfox_validate.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="Inclusive lookback window in days (default: 30).",
    )
    redfox_validate.add_argument(
        "--max-pages",
        type=int,
        default=3,
        help="Maximum 20-row list pages per confirmed account (default: 3).",
    )
    redfox_validate.add_argument(
        "--max-detail-requests",
        type=int,
        default=30,
        help="Maximum full-body requests across the complete validation run (default: 30).",
    )
    redfox_validate.add_argument(
        "--min-detail-success-rate",
        type=float,
        default=0.7,
        help="Minimum successful-detail-sample rate required for an enable recommendation (default: 0.70).",
    )
    redfox_validate.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    redfox_wide_search = redfox_commands.add_parser(
        "broad-search",
        help="Search the RedFox broad library without importing or enabling any source.",
    )
    redfox_wide_search.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement before any external RedFox request is made.",
    )
    redfox_wide_search.add_argument(
        "--keyword",
        required=True,
        help="Broad-library keyword (searches article titles, summaries, and authors).",
    )
    redfox_wide_search.add_argument(
        "--exact-author",
        help="Optional exact public-account display name to count; never enables a source.",
    )
    redfox_wide_search.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="Maximum 20-row broad-search pages (default: 1).",
    )
    redfox_wide_search.add_argument(
        "--sort-type",
        choices=("0", "2", "4"),
        default="2",
        help="0=default, 2=latest, 4=hottest (default: 2).",
    )
    redfox_wide_search.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    sources = commands.add_parser(
        "sources",
        help="Manage local source proposals and explicit enablement gates.",
    )
    source_commands = sources.add_subparsers(dest="source_command", required=True)
    source_seed = source_commands.add_parser(
        "seed-priority-sites",
        help="Seed the documented 31 priority websites as disabled research preferences.",
    )
    source_seed.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    source_list = source_commands.add_parser(
        "list",
        help="List configured public-account sources and priority sites without external calls.",
    )
    source_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    source_configure = source_commands.add_parser(
        "configure-account",
        help="Create or edit one RedFox source; it stays disabled unless --enabled is explicit.",
    )
    source_configure.add_argument("--name", required=True, help="Local monitored-source display name.")
    source_configure.add_argument(
        "--redfox-name", required=True, help="Human-confirmed exact RedFox display name."
    )
    source_configure.add_argument(
        "--redfox-account",
        help="Human-confirmed RedFox account identifier (required for quality library).",
    )
    source_configure.add_argument(
        "--library",
        choices=("quality", "wide"),
        default="quality",
        help="RedFox quality or broad library (default: quality).",
    )
    source_configure.add_argument(
        "--validation-status",
        choices=("pending", "matched", "not_found", "error", "paused"),
        default="pending",
        help="Operator-recorded validation state (default: pending).",
    )
    source_configure.add_argument(
        "--priority", type=int, default=50, help="0-100 source priority (default: 50)."
    )
    source_configure.add_argument(
        "--enabled",
        action="store_true",
        help="Explicitly enable after a human-confirmed matched validation result.",
    )
    source_configure.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    source_enable = source_commands.add_parser(
        "set-enabled",
        help="Explicitly enable or pause one configured source with an audit event.",
    )
    source_enable.add_argument("--name", required=True, help="Configured local source display name.")
    enable_group = source_enable.add_mutually_exclusive_group(required=True)
    enable_group.add_argument("--enable", action="store_true", help="Enable a matched, complete source.")
    enable_group.add_argument("--disable", action="store_true", help="Pause a source without deleting it.")
    source_enable.add_argument("--reason", help="Optional operator audit note.")
    source_enable.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    source_site_configure = source_commands.add_parser(
        "configure-site",
        help="Create or edit a public-research priority site; it is not an internet whitelist.",
    )
    source_site_configure.add_argument("--name", required=True, help="Site display name.")
    source_site_configure.add_argument("--domain", required=True, help="Bare domain or HTTP(S) URL.")
    source_site_configure.add_argument("--category", required=True, help="Site category, for example company or media.")
    source_site_configure.add_argument("--source-role", required=True, help="How this site may be used.")
    source_site_configure.add_argument("--evidence-policy", required=True, help="Concrete evidence-use rule.")
    source_site_configure.add_argument("--priority", type=int, default=50, help="0-100 priority (default: 50).")
    source_site_configure.add_argument(
        "--topic", action="append", dest="topics", help="Applicable topic; repeat as needed."
    )
    source_site_configure.add_argument("--derived-from", help="Human-readable source of this configuration.")
    source_site_configure.add_argument("--enabled", action="store_true", help="Explicitly enable this research preference.")
    source_site_configure.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    source_site_enable = source_commands.add_parser(
        "set-site-enabled",
        help="Explicitly enable or pause one priority site without making it a strict whitelist.",
    )
    source_site_enable.add_argument("--domain", required=True, help="Configured priority-site domain.")
    site_enable_group = source_site_enable.add_mutually_exclusive_group(required=True)
    site_enable_group.add_argument("--enable", action="store_true", help="Enable the site as a research preference.")
    site_enable_group.add_argument("--disable", action="store_true", help="Pause the site preference.")
    source_site_enable.add_argument("--reason", help="Optional operator audit note.")
    source_site_enable.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    source_import_article = source_commands.add_parser(
        "import-article",
        help="Store one operator-confirmed WeChat article under a configured source.",
    )
    source_import_article.add_argument("--url", required=True, help="Public WeChat article URL.")
    source_import_article.add_argument("--account", required=True, help="Configured source account name.")
    source_import_article.add_argument("--title", help="Optional title override when page parsing fails.")
    source_import_article.add_argument("--published-at", help="Optional publish time override, e.g. 2026-08-07 17:30.")
    source_import_article.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    sync = commands.add_parser(
        "sync",
        help="Synchronize explicitly enabled RedFox sources and rebuild rule-based signals.",
    )
    sync.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement before any billable RedFox requests are made.",
    )
    sync.add_argument("--lookback-days", type=int, default=3, help="Inclusive lookback days (default: 3).")
    sync.add_argument("--max-pages", type=int, default=3, help="Maximum 20-row pages per source (default: 3).")
    sync.add_argument(
        "--max-detail-requests",
        type=int,
        default=30,
        help="Maximum quality-library detail requests across the complete sync (default: 30).",
    )
    sync.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    weekly = commands.add_parser(
        "weekly",
        help="Create a versioned local weekly signal-brief draft from existing signals.",
    )
    weekly.add_argument("--cycle", help="Monitoring-cycle slug (defaults to the current/latest cycle).")
    weekly.add_argument(
        "--max-signals", type=int, default=8, help="Number of top signals to include (default: 8)."
    )
    weekly.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    knowledge_base = commands.add_parser(
        "knowledge-base",
        help="Accumulate and inspect the local article knowledge base.",
    )
    knowledge_base_commands = knowledge_base.add_subparsers(
        dest="knowledge_base_command", required=True
    )
    knowledge_base_stats_parser = knowledge_base_commands.add_parser(
        "stats",
        help="Report stored article and body coverage without external calls.",
    )
    knowledge_base_stats_parser.add_argument("--json", action="store_true")
    knowledge_base_export = knowledge_base_commands.add_parser(
        "export",
        help="Write an immutable local snapshot of stored articles and bodies.",
    )
    knowledge_base_export.add_argument(
        "--max-articles",
        type=int,
        help="Optional export cap for a partial inspection snapshot.",
    )
    knowledge_base_export.add_argument("--json", action="store_true")
    knowledge_base_backfill = knowledge_base_commands.add_parser(
        "backfill",
        help="Fetch missing article bodies from RedFox into the local knowledge base.",
    )
    knowledge_base_backfill.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement before billable RedFox detail requests are made.",
    )
    knowledge_base_backfill.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum missing-body requests across this backfill run (default: 200).",
    )
    knowledge_base_backfill.add_argument("--json", action="store_true")

    research = commands.add_parser(
        "research",
        help="Run explicitly authorized research-agent smoke tests.",
    )
    research_commands = research.add_subparsers(dest="research_command", required=True)
    research_smoke = research_commands.add_parser(
        "smoke-test-pi",
        help="Run the P2-02 public-web retrieval smoke test against the pi agent.",
    )
    research_smoke.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement before the pi model and web tools are invoked.",
    )
    research_smoke.add_argument(
        "--question",
        default=(
            "请检索并给出至少 3 条能相互印证的公开网页证据，主题：2025 年以来"
            "中国市场监管总局对保健食品原料目录或营养素补充剂注册备案相关的监管动态。"
            "要求每条给出具体页面 URL、发布机构、日期与来源类型；"
            "找不到可靠来源就明确写“不确定”，绝不编造 URL。"
        ),
        help="Non-sensitive smoke-test question (defaults to the P2-02 question).",
    )
    research_smoke.add_argument("--json", action="store_true")

    serve = commands.add_parser(
        "serve",
        help="Serve the formal local workbench on 127.0.0.1 after sources have been configured/synced.",
    )
    serve.add_argument("--port", type=int, default=8765, help="Local port (default: 8765).")
    serve.add_argument("--open", action="store_true", help="Open the board in the default browser.")

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

    if args.command == "sources":
        try:
            if args.source_command == "seed-priority-sites":
                result = seed_priority_sites(settings.database_path)
                payload = result.as_dict()
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    print(
                        "Priority-site seed completed: "
                        f"{result.inserted} inserted; {result.already_present} already present. "
                        "All seeded sites remain disabled until an operator enables them."
                    )
                return 0

            if args.source_command == "import-article":
                try:
                    result = import_wechat_article(
                        settings.database_path,
                        args.url,
                        account_name=args.account,
                        title=args.title,
                        published_at=args.published_at,
                    )
                except (SourceRegistryError, ValueError, OSError) as exc:
                    print(f"Article import failed: {exc}", file=sys.stderr)
                    return 1
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    print(
                        f"Imported under {result['account']}: {result['title']}; "
                        f"inserted {result['articles_inserted']}, updated {result['articles_updated']}."
                    )
                return 0

            if args.source_command == "list":
                accounts = [account.as_dict() for account in list_source_accounts(settings.database_path)]
                sites = [site.as_dict() for site in list_priority_sites(settings.database_path)]
                payload = {
                    "accounts": accounts,
                    "account_count": len(accounts),
                    "priority_sites": sites,
                    "priority_site_count": len(sites),
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    if not accounts:
                        print("No public-account sources are configured.")
                    for account in accounts:
                        print(
                            f"- {account['name']}: {'enabled' if account['enabled'] else 'disabled'}; "
                            f"{account['validation_status']}; {account['redfox_library']}; "
                            f"priority {account['priority']}"
                        )
                    print(f"Priority sites: {len(sites)} total; {sum(bool(site['enabled']) for site in sites)} enabled.")
                return 0

            if args.source_command == "configure-account":
                account = configure_source_account(
                    settings.database_path,
                    name=args.name,
                    redfox_name=args.redfox_name,
                    redfox_account=args.redfox_account,
                    redfox_library=args.library,
                    priority=args.priority,
                    enabled=args.enabled,
                    validation_status=args.validation_status,
                )
                payload = account.as_dict()
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    print(
                        f"Configured {account.name}: "
                        f"{'enabled' if account.enabled else 'disabled'}; "
                        f"validation {account.validation_status}; library {account.redfox_library}."
                    )
                return 0

            if args.source_command == "set-enabled":
                account = set_source_account_enabled(
                    settings.database_path,
                    name=args.name,
                    enabled=bool(args.enable),
                    reason=args.reason,
                )
                payload = account.as_dict()
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    print(f"{account.name} is now {'enabled' if account.enabled else 'disabled'}.")
                return 0

            if args.source_command == "configure-site":
                site = configure_priority_site(
                    settings.database_path,
                    name=args.name,
                    domain=args.domain,
                    category=args.category,
                    source_role=args.source_role,
                    evidence_policy=args.evidence_policy,
                    priority=args.priority,
                    enabled=args.enabled,
                    applicable_topics=tuple(args.topics or ()),
                    derived_from=args.derived_from,
                )
                payload = site.as_dict()
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    print(
                        f"Configured priority site {site.domain}: "
                        f"{'enabled' if site.enabled else 'disabled'}; priority {site.priority}."
                    )
                return 0

            if args.source_command == "set-site-enabled":
                site = set_priority_site_enabled(
                    settings.database_path,
                    domain=args.domain,
                    enabled=bool(args.enable),
                    reason=args.reason,
                )
                payload = site.as_dict()
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    print(f"Priority site {site.domain} is now {'enabled' if site.enabled else 'disabled'}.")
                return 0
        except (SourceRegistryError, OSError, ValueError) as exc:
            print(f"Source configuration failed: {exc}", file=sys.stderr)
            return 1

    if args.command == "sync":
        if not args.live:
            payload = {
                "status": "protected",
                "message": "RedFox sync can make billable external requests; rerun with --live.",
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(payload["message"], file=sys.stderr)
            return 2
        try:
            enabled_sources = [
                account
                for account in list_source_accounts(settings.database_path)
                if account.enabled and account.validation_status == "matched"
            ]
        except (SourceRegistryError, OSError, ValueError) as exc:
            print(f"RedFox sync preparation failed: {exc}", file=sys.stderr)
            return 1
        if not enabled_sources:
            payload = {
                "status": "no_enabled_sources",
                "message": "No human-confirmed, enabled RedFox sources are configured; no external request was made.",
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(payload["message"], file=sys.stderr)
            return 1
        if not settings.redfox_ready:
            payload = {
                "status": "not_configured",
                "message": "REDFOX_API_KEY is not configured for this project root.",
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(payload["message"], file=sys.stderr)
            return 1
        try:
            gateway = RedFoxGateway(
                settings.redfox_api_key,
                base_url=settings.redfox_base_url,
                timeout_seconds=settings.redfox_timeout_seconds,
                max_retries=settings.redfox_max_retries,
                raw_snapshot_directory=settings.redfox_raw_directory,
            )
            result = sync_enabled_redfox_sources(
                settings.database_path,
                gateway,
                lookback_days=args.lookback_days,
                max_pages=args.max_pages,
                max_detail_requests=args.max_detail_requests,
            )
        except (RedFoxGatewayError, SourceRegistryError, ValueError, OSError) as exc:
            print(f"RedFox sync failed: {exc}", file=sys.stderr)
            return 1
        payload = result.as_dict()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"RedFox sync: {result.successful_sources}/{result.attempted_sources} sources completed; "
                f"{result.imported_articles} normalized articles."
            )
            for outcome in result.outcomes:
                print(
                    f"- {outcome.source_name}: {outcome.status}; "
                    f"{outcome.normalized_articles} articles; {outcome.message}"
                )
            if result.signal_build:
                print(
                    f"Rule-based signals rebuilt for {result.signal_build.cycle_slug}: "
                    f"{result.signal_build.signals} signals, {result.signal_build.candidates} candidates."
                )
        return 1 if result.failed_sources else 0

    if args.command == "weekly":
        try:
            result = export_weekly_signal_brief(
                settings.database_path,
                cycle_slug=args.cycle,
                max_signals=args.max_signals,
            )
        except (MonitoringCycleError, ValueError, OSError) as exc:
            print(f"Weekly signal brief failed: {exc}", file=sys.stderr)
            return 1
        payload = result.as_dict()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"Weekly signal brief v{result.version:03d}: {result.signal_count} active signals; "
                f"draft at {result.markdown_path}"
            )
            print("It remains a human-review draft and does not create research tasks or publish.")
        return 0

    if args.command == "knowledge-base":
        if args.knowledge_base_command == "stats":
            stats = knowledge_base_stats(settings.database_path)
            payload = stats.as_dict()
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    f"Local knowledge base: {stats.articles} articles; "
                    f"{stats.articles_with_body} with body "
                    f"({stats.body_coverage:.1%}); {stats.accounts} accounts."
                )
            return 0
        if args.knowledge_base_command == "backfill":
            if not args.live:
                payload = {
                    "status": "protected",
                    "message": (
                        "Knowledge-base backfill makes billable RedFox detail requests; "
                        "rerun with --live to acknowledge."
                    ),
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    print(payload["message"], file=sys.stderr)
                return 2
            if not settings.redfox_ready:
                print(
                    "REDFOX_API_KEY is not configured for this project root.",
                    file=sys.stderr,
                )
                return 1
            try:
                gateway = RedFoxGateway(
                    settings.redfox_api_key,
                    base_url=settings.redfox_base_url,
                    timeout_seconds=settings.redfox_timeout_seconds,
                    max_retries=settings.redfox_max_retries,
                    raw_snapshot_directory=settings.redfox_raw_directory,
                )
                result = backfill_missing_bodies(
                    settings.database_path,
                    gateway,
                    limit=args.limit,
                )
            except (RedFoxGatewayError, ValueError, OSError) as exc:
                print(f"Knowledge-base backfill failed: {exc}", file=sys.stderr)
                return 1
            payload = result.as_dict()
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    f"Knowledge-base backfill: {result.attempted} missing; "
                    f"{result.filled} filled; {result.failed} failed."
                )
            return 0 if result.failed < result.attempted or result.attempted == 0 else 1
        try:
            result = export_knowledge_base(
                settings.database_path,
                max_articles=args.max_articles,
            )
        except (ValueError, OSError) as exc:
            print(f"Knowledge base export failed: {exc}", file=sys.stderr)
            return 1
        payload = result.as_dict()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"Knowledge base v{result.version:03d}: {result.exported_articles} articles "
                f"({result.with_body} with body); snapshot at {result.directory}"
            )
            print("The snapshot is read-only and does not publish or alter topic status.")
        return 0

    if args.command == "research" and args.research_command == "smoke-test-pi":
        if not args.live:
            payload = {
                "status": "protected",
                "message": (
                    "The pi smoke test invokes a model and public-web tools; "
                    "rerun with --live to acknowledge the bounded external call."
                ),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(payload["message"], file=sys.stderr)
            return 2
        request = PiAgentRequest(
            research_question=str(args.question).strip(),
            audience="营养健康行业品牌方、原料商及企业决策者",
            decision_use="验证研究后端能否公开检索并返回可核验页面 URL。",
            scope="仅公开互联网；不访问付费数据库或内部系统。",
            time_range="2025 年至今",
            output_requirements="返回来源清单，每条含具体 URL 与来源类型；未核实项标记待补证。",
        )
        runner = PiAgentRunner(
            pi_command=[settings.pi_command],
            provider=settings.pi_provider,
            model=settings.pi_model or None,
            timeout_seconds=settings.pi_timeout_seconds,
            system_prompt=build_pi_system_prompt(),
        )
        try:
            with tempfile.TemporaryDirectory(prefix="pi-smoke-") as smoke_cwd:
                runner.working_directory = Path(smoke_cwd)
                result = runner.run(request)
            exchange = save_agent_exchange(settings.agent_raw_directory, request, result)
        except AgentGatewayError as exc:
            print(f"pi smoke test failed: {exc}", file=sys.stderr)
            return 1
        passed = (
            result.status == "completed"
            and result.web_tool_calls >= 1
            and len(result.web_urls) >= 1
        )
        payload = {
            "status": "passed" if passed else "failed",
            "agent_status": result.status,
            "web_tool_calls": result.web_tool_calls,
            "url_count": len(result.web_urls),
            "urls": list(result.web_urls[:20]),
            "tool_calls": len(result.tool_calls),
            "final_text_length": len(result.final_text),
            "final_text": result.final_text[:4000],
            "exchange_files": {
                "input": str(exchange[0]),
                "output": str(exchange[1]),
                "events": str(exchange[2]),
            },
            "stderr": result.stderr[:1000],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"pi smoke test: {payload['status']}; agent={result.status}; "
                f"web_tools={result.web_tool_calls}; urls={len(result.web_urls)}; "
                f"text={len(result.final_text)} chars."
            )
            if result.stderr.strip():
                print(f"pi stderr: {result.stderr.strip()[:500]}")
            for url in result.web_urls[:10]:
                print(f"- {url}")
        return 0 if passed else 1

    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            print("Server port must be between 1 and 65535", file=sys.stderr)
            return 1
        try:
            run_radar_server(
                settings.database_path,
                port=args.port,
                open_browser=args.open,
                workspace_label="Nutrition Signal Radar",
            )
        except (FileNotFoundError, OSError) as exc:
            print(f"Radar server failed: {exc}", file=sys.stderr)
            return 1
        return 0

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

    if args.command == "redfox" and args.redfox_command == "broad-search":
        if not args.live:
            payload = {
                "status": "protected",
                "message": "RedFox broad search is read-only but can make billable external requests; rerun with --live.",
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    "RedFox broad search is protected: no external request was made. "
                    "Rerun with --live to acknowledge the bounded provider calls.",
                    file=sys.stderr,
                )
            return 2
        if not settings.redfox_ready:
            payload = {
                "status": "not_configured",
                "message": "REDFOX_API_KEY is not configured for this project root.",
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(payload["message"], file=sys.stderr)
            return 1
        try:
            gateway = RedFoxGateway(
                settings.redfox_api_key,
                base_url=settings.redfox_base_url,
                timeout_seconds=settings.redfox_timeout_seconds,
                max_retries=settings.redfox_max_retries,
                raw_snapshot_directory=settings.redfox_raw_directory,
            )
            report = search_redfox_wide_library(
                gateway,
                keyword=args.keyword,
                exact_author=args.exact_author,
                max_pages=args.max_pages,
                sort_type=args.sort_type,
            )
            artifacts = write_redfox_wide_search_report(report, settings.exports_directory)
        except (RedFoxGatewayError, ValueError, OSError) as exc:
            print(f"RedFox broad search failed: {exc}", file=sys.stderr)
            return 1

        payload = report.as_dict()
        payload["artifacts"] = artifacts.as_dict()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("RedFox broad search completed (read-only; no source registry or database changes).")
            print(
                f"Keyword: {report.keyword}; pages: {report.pages_fetched}; "
                f"results: {len(report.results)}; exact-author matches: {report.exact_author_match_count}"
            )
            for author in report.authors[:10]:
                print(
                    f"- {author.author or '未提供'}: {author.article_count} results; "
                    f"latest {author.latest_published_at or 'unknown'}"
                )
            print(f"JSON report: {artifacts.json_path}")
            print(f"Markdown report: {artifacts.markdown_path}")
        return 0

    if args.command == "redfox" and args.redfox_command == "validate":
        accounts = tuple(args.accounts or DEFAULT_REDFOX_ACCOUNT_NAMES)
        if not args.live:
            payload = {
                "status": "protected",
                "message": "RedFox validation is read-only but can make billable external requests; rerun with --live.",
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    "RedFox validation is protected: no external request was made. "
                    "Rerun with --live to acknowledge the bounded provider calls.",
                    file=sys.stderr,
                )
            return 2
        if not settings.redfox_ready:
            payload = {
                "status": "not_configured",
                "message": "REDFOX_API_KEY is not configured for this project root.",
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(payload["message"], file=sys.stderr)
            return 1
        try:
            gateway = RedFoxGateway(
                settings.redfox_api_key,
                base_url=settings.redfox_base_url,
                timeout_seconds=settings.redfox_timeout_seconds,
                max_retries=settings.redfox_max_retries,
                raw_snapshot_directory=settings.redfox_raw_directory,
            )
            report = validate_redfox_accounts(
                gateway,
                accounts=accounts,
                lookback_days=args.lookback_days,
                max_pages=args.max_pages,
                max_detail_requests=args.max_detail_requests,
                min_detail_success_rate=args.min_detail_success_rate,
            )
            artifacts = write_redfox_validation_report(report, settings.exports_directory)
        except (RedFoxGatewayError, ValueError, OSError) as exc:
            print(f"RedFox validation failed: {exc}", file=sys.stderr)
            return 1

        payload = report.as_dict()
        payload["artifacts"] = artifacts.as_dict()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("RedFox validation completed (read-only; no source registry or database changes).")
            print(
                f"Window: {report.publish_time_start} to {report.publish_time_end}; "
                f"enable recommendation: {report.enable_count}/{len(report.accounts)}"
            )
            for health in report.accounts:
                print(
                    f"- {health.requested_name}: {health.status}; "
                    f"recommend {health.recommended_action}; {health.article_count} articles; "
                    f"detail sample {health.detail_successes}/{health.detail_requests}; "
                    f"current body coverage {health.body_coverage:.0%}; {health.message}"
                )
            print(f"JSON report: {artifacts.json_path}")
            print(f"Markdown report: {artifacts.markdown_path}")
        # A pause/correct-name recommendation is a valid validation outcome,
        # not a process failure. Consumers can inspect ``ready_for_next_phase``
        # in the JSON report before deciding whether to begin formal sync.
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


def _configure_console_output() -> None:
    """Keep Windows console output from crashing on characters outside its code page."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if hasattr(stream, "isatty") and stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError, io.UnsupportedOperation):
            continue


if __name__ == "__main__":
    sys.exit(main())
