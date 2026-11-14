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
from .cycle_service import MonitoringCycleError, get_monitoring_cycle
from .database import connect_database
from .demo_pipeline import build_demo_topics
from .doctor import build_doctor_report, render_doctor_report
from .ingest_service import import_article_batch
from .knowledge_base import export_knowledge_base, knowledge_base_stats
from .llm_gateway import LlmGateway, LlmGatewayError
from .news_compose import (
    NewsComposeError,
    build_news_article,
    diff_news_builds,
    load_news_article_build,
    mark_news_article_pushed,
    news_publish_blockers,
    preview_news_build,
    preview_news_readiness,
)
from .news_fetch_service import NewsFetchError, fetch_enabled_news_sources
from .news_registry import (
    NewsRegistryError,
    configure_news_source,
    list_enabled_news_sources,
    list_news_sources,
    news_fetch_freshness,
    news_source_seed_count,
    news_source_yield,
    seed_news_sources,
    set_news_source_enabled,
)
from .news_selection import (
    evaluate_pending_news_articles,
    list_news_items,
    revalidate_selected_news_items,
)
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
from .research_workflow import ResearchWorkflowError, run_online_research
from .schema import initialize_schema, inspect_schema
from .signal_generation import SignalAnalysisError, analyze_cycle_signals
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
from .wechat_gateway import WechatGatewayError, push_news_draft
from .weekly_service import (
    WeeklyArticleError,
    build_weekly_article_prompt,
    confirm_weekly_article,
    export_weekly_signal_brief,
    generate_weekly_article,
    list_weekly_articles,
    list_weekly_signal_briefs,
    read_weekly_article,
    reopen_weekly_article,
)
from .wechat_article_import import import_wechat_article
from .web_app import run_demo_server, run_news_reader_server, run_radar_server
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
    source_import_article.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement before fetching the public article page.",
    )
    source_import_article.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    news = commands.add_parser(
        "news",
        help="Manage the Phase 4 news-line whitelist of directly fetched primary sources.",
    )
    news_commands = news.add_subparsers(dest="news_command", required=True)
    news_seed = news_commands.add_parser(
        "seed-sources",
        help="Seed the documented news-whitelist proposals as disabled sources.",
    )
    news_seed.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    news_list = news_commands.add_parser(
        "list-sources",
        help="List news-line whitelist entries without external calls.",
    )
    news_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    news_configure = news_commands.add_parser(
        "configure-source",
        help="Create or edit one news-whitelist source; it stays disabled unless --enabled is explicit.",
    )
    news_configure.add_argument("--name", required=True, help="Whitelist source display name.")
    news_configure.add_argument("--domain", required=True, help="Bare domain or HTTP(S) URL.")
    news_configure.add_argument(
        "--category",
        required=True,
        choices=("policy", "disclosure", "brand", "expo", "research"),
        help="Whitelist category per ADR-0014.",
    )
    news_configure.add_argument(
        "--entry-url", required=True, help="HTTP(S) fetch entry (list page, API endpoint, or RSS feed)."
    )
    news_configure.add_argument(
        "--fetch-kind",
        choices=("html_list", "api", "rss"),
        default="html_list",
        help="How the entry URL is fetched (default: html_list).",
    )
    news_configure.add_argument(
        "--priority",
        default="P2",
        choices=("P0", "P1", "P2", "P3"),
        help="Default news priority for items from this source (default: P2).",
    )
    news_configure.add_argument(
        "--parser-version", default="v1", help="Parser-config version tag (default: v1)."
    )
    news_configure.add_argument(
        "--parser-config",
        help=(
            "Optional parser options as a JSON object, e.g. "
            '\'{"searchkey":"保健"}\' for the cninfo announcement query.'
        ),
    )
    news_configure.add_argument("--notes", help="Optional operator notes.")
    news_configure.add_argument(
        "--enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Explicitly enable/disable; omit to keep the current state on update.",
    )
    news_configure.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    news_enable = news_commands.add_parser(
        "set-source-enabled",
        help="Explicitly enable or pause one news-whitelist source with an audit event.",
    )
    news_enable.add_argument("--name", required=True, help="Configured whitelist source name.")
    news_enable_group = news_enable.add_mutually_exclusive_group(required=True)
    news_enable_group.add_argument("--enable", action="store_true", help="Enable the confirmed whitelist entry.")
    news_enable_group.add_argument("--disable", action="store_true", help="Pause the whitelist entry.")
    news_enable.add_argument("--reason", help="Optional operator audit note.")
    news_enable.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    news_fetch = news_commands.add_parser(
        "fetch",
        help="Fetch every enabled news-whitelist source; external requests require --live.",
    )
    news_fetch.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement before any external site request is made.",
    )
    news_fetch.add_argument(
        "--lookback-hours",
        type=int,
        default=24,
        help="Keep items published within this many hours (default: 24).",
    )
    news_fetch.add_argument(
        "--max-items",
        type=int,
        default=30,
        help="Maximum in-window items kept per source (default: 30).",
    )
    news_fetch.add_argument(
        "--source",
        action="append",
        dest="sources",
        help="Restrict the run to these configured source names; repeat as needed.",
    )
    news_fetch.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    news_select = news_commands.add_parser(
        "select",
        help="Apply deterministic admission rules to fetched news articles (no network, no LLM).",
    )
    news_select.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    news_revalidate = news_commands.add_parser(
        "revalidate",
        help=(
            "Re-apply current selection rules to previously selected items "
            "(retroactive cleanup after rule tightening; no network, no LLM)."
        ),
    )
    news_revalidate.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    news_items = news_commands.add_parser(
        "list-items",
        help="List recorded news-item decisions (the daily source-table view).",
    )
    news_items.add_argument(
        "--selected-only", action="store_true", help="Only show items admitted for article building."
    )
    news_items.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    news_build = news_commands.add_parser(
        "build",
        help="Summarize selected items and assemble the versioned local article (LLM calls require --live).",
    )
    news_build.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement before any LLM API request is made.",
    )
    news_build.add_argument(
        "--lookback-hours",
        type=int,
        default=24,
        help="Daily selection window in hours (default: 24; falls back to a 7-day weekly view).",
    )
    news_build.add_argument(
        "--max-items",
        type=int,
        default=5,
        choices=range(1, 6),
        help="Maximum items included in one article (default: 5; requirement caps at 5).",
    )
    news_build.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the deterministic skeleton with placeholder summaries; no LLM call, no artifacts.",
    )
    news_build.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    news_readiness = news_commands.add_parser(
        "readiness",
        help="Show whether today's pool can produce a daily/weekly article (no network, no LLM).",
    )
    news_readiness.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    news_qc = news_commands.add_parser(
        "qc",
        help="Re-read one build's QC log and publish gate (no network, no LLM).",
    )
    news_qc.add_argument("--slug", required=True, help="Built article slug, e.g. news-2026-09-01.")
    news_qc.add_argument("--version", type=int, required=True, help="Built article version number.")
    news_qc.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    news_diff = news_commands.add_parser(
        "diff",
        help="Compare two versions of one build by manifest items (read-only).",
    )
    news_diff.add_argument("--slug", required=True, help="Built article slug.")
    news_diff.add_argument(
        "--from", dest="from_version", type=int, required=True,
        help="Older version number.",
    )
    news_diff.add_argument(
        "--to", dest="to_version", type=int, required=True,
        help="Newer version number.",
    )
    news_diff.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    news_publish = news_commands.add_parser(
        "publish",
        help="Push one built news article into the WeChat draft box (requires --live and credentials).",
    )
    news_publish.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement before any WeChat API request is made.",
    )
    news_publish.add_argument(
        "--confirm",
        action="store_true",
        help="Required human confirmation after reviewing the local artifacts; publish never runs automatically.",
    )
    news_publish.add_argument("--slug", required=True, help="Built article slug, e.g. news-2026-09-01.")
    news_publish.add_argument("--version", type=int, required=True, help="Built article version number.")
    news_publish.add_argument("--author", default="EarlyData", help="Draft author display name (default: EarlyData).")
    news_publish.add_argument("--digest", help="Optional draft digest; defaults to the article lead paragraph.")
    news_publish.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    news_serve = news_commands.add_parser(
        "serve",
        help="Serve built news articles as a local read-only reading site (127.0.0.1 only).",
    )
    news_serve.add_argument("--port", type=int, default=8780, help="Local port (default: 8780).")
    news_serve.add_argument(
        "--open", action="store_true", help="Open the reading site in the default browser."
    )

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

    signals = commands.add_parser(
        "signals",
        help="Use the configured analysis backend to interpret rule-generated industry signals.",
    )
    signals_commands = signals.add_subparsers(dest="signals_command", required=True)
    signals_analyze = signals_commands.add_parser(
        "analyze",
        help=(
            "Fill interpretive fields (underlying event, consensus, disagreements, "
            "business implications, data gaps, affected actors) of the open cycle's "
            "active signals. Rules keep doing recall and ranking; scores and states "
            "never change."
        ),
    )
    signals_analyze.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement before any LLM API request is made.",
    )
    signals_analyze.add_argument(
        "--max-signals",
        type=int,
        default=30,
        help="Maximum number of active signals to analyze (default: 30).",
    )
    signals_analyze.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    weekly = commands.add_parser(
        "weekly",
        help="Create and review weekly signal briefs and publishable weekly articles.",
    )
    weekly_commands = weekly.add_subparsers(dest="weekly_command", required=True)
    weekly_brief = weekly_commands.add_parser(
        "brief",
        help="Create a versioned local weekly signal-brief draft from existing signals.",
    )
    weekly_brief.add_argument(
        "--cycle", help="Monitoring-cycle slug (defaults to the current/latest cycle)."
    )
    weekly_brief.add_argument(
        "--max-signals",
        type=int,
        default=8,
        help="Number of top signals to include (default: 8).",
    )
    weekly_brief.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    weekly_list = weekly_commands.add_parser(
        "list",
        help="List weekly signal briefs and weekly articles for one cycle.",
    )
    weekly_list.add_argument(
        "--cycle", help="Monitoring-cycle slug (defaults to the current/latest cycle)."
    )
    weekly_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    weekly_article = weekly_commands.add_parser(
        "article",
        help="Generate and review publishable weekly industry articles.",
    )
    weekly_article_commands = weekly_article.add_subparsers(
        dest="weekly_article_command", required=True
    )
    weekly_article_generate = weekly_article_commands.add_parser(
        "generate",
        help=(
            "Generate one publishable weekly article with the pi public-web agent. "
            "Requires --live."
        ),
    )
    weekly_article_generate.add_argument(
        "--cycle", help="Monitoring-cycle slug (defaults to the current/latest cycle)."
    )
    weekly_article_generate.add_argument(
        "--max-signals",
        type=int,
        default=8,
        help="Number of top signals to include (default: 8).",
    )
    weekly_article_generate.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement before the pi model and web tools are invoked.",
    )
    weekly_article_generate.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    weekly_article_confirm = weekly_article_commands.add_parser(
        "confirm",
        help="Mark one generated weekly article as human-confirmed for publication.",
    )
    weekly_article_confirm.add_argument(
        "--cycle", help="Monitoring-cycle slug (defaults to the current/latest cycle)."
    )
    weekly_article_confirm.add_argument(
        "--version", type=int, required=True, help="Article version number (>= 1)."
    )
    weekly_article_confirm.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    weekly_article_reopen = weekly_article_commands.add_parser(
        "reopen",
        help="Return a confirmed weekly article to the draft queue.",
    )
    weekly_article_reopen.add_argument(
        "--cycle", help="Monitoring-cycle slug (defaults to the current/latest cycle)."
    )
    weekly_article_reopen.add_argument(
        "--version", type=int, required=True, help="Article version number (>= 1)."
    )
    weekly_article_reopen.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )

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
    research_run = research_commands.add_parser(
        "run",
        help="Start a human-confirmed research task with the pi public-web agent.",
    )
    research_run.add_argument(
        "--task-id",
        type=int,
        required=True,
        help="Research task id that has been confirmed and is waiting to start.",
    )
    research_run.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement before the pi model and web tools are invoked.",
    )
    research_run.add_argument("--json", action="store_true")

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
                if not args.live:
                    payload = {
                        "status": "protected",
                        "message": (
                            "Article import fetches the public WeChat page; "
                            "rerun with --live to acknowledge the external request."
                        ),
                    }
                    if args.json:
                        print(json.dumps(payload, ensure_ascii=False, indent=2))
                    else:
                        print(payload["message"], file=sys.stderr)
                    return 2
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

    if args.command == "news":
        try:
            if args.news_command == "seed-sources":
                result = seed_news_sources(settings.database_path)
                if args.json:
                    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
                else:
                    print(
                        f"News-whitelist seed completed: {result.inserted} inserted; "
                        f"{result.already_present} already present "
                        f"(documented proposal size: {news_source_seed_count()}). "
                        "All seeded sources remain disabled until an operator confirms and enables them."
                    )
                return 0

            if args.news_command == "list-sources":
                sources = [source.as_dict() for source in list_news_sources(settings.database_path)]
                enabled_count = len(list_enabled_news_sources(settings.database_path))
                yields = news_source_yield(settings.database_path)
                for source in sources:
                    stats = yields.get(int(source["id"]), {})
                    source["yield_30d"] = {
                        "evaluated": stats.get("evaluated", 0),
                        "selected": stats.get("selected", 0),
                    }
                payload = {
                    "sources": sources,
                    "source_count": len(sources),
                    "enabled_count": enabled_count,
                    "fetch_freshness": news_fetch_freshness(settings.database_path),
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    if not sources:
                        print("No news-whitelist sources are configured yet.")
                    for source in sources:
                        stats = source["yield_30d"]
                        print(
                            f"- {source['name']} ({source['category']}): "
                            f"{'enabled' if source['enabled'] else 'disabled'}; "
                            f"{source['default_priority']}; {source['fetch_kind']}; "
                            f"30d 入选 {stats['selected']}/{stats['evaluated']}; "
                            f"{source['entry_url']}"
                        )
                    freshness = payload["fetch_freshness"]
                    if freshness["stale"]:
                        age = freshness["age_hours"]
                        age_text = f"{age}h" if age is not None else "从未抓取"
                        print(
                            f"WARNING: 资讯抓取已停摆（最近 {age_text}，"
                            f"阈值 {freshness['stale_hours']}h）；请检查任务计划程序。"
                        )
                    print(f"News whitelist: {len(sources)} total; {enabled_count} enabled.")
                return 0

            if args.news_command == "configure-source":
                source = configure_news_source(
                    settings.database_path,
                    name=args.name,
                    domain=args.domain,
                    category=args.category,
                    entry_url=args.entry_url,
                    fetch_kind=args.fetch_kind,
                    default_priority=args.priority,
                    parser_version=args.parser_version,
                    parser_config=args.parser_config or "",
                    notes=args.notes,
                    enabled=args.enabled,
                )
                if args.json:
                    print(json.dumps(source.as_dict(), ensure_ascii=False, indent=2))
                else:
                    print(
                        f"Configured news source {source.name}: "
                        f"{'enabled' if source.enabled else 'disabled'}; "
                        f"{source.category}; {source.default_priority}; {source.entry_url}."
                    )
                return 0

            if args.news_command == "set-source-enabled":
                source = set_news_source_enabled(
                    settings.database_path,
                    name=args.name,
                    enabled=bool(args.enable),
                    reason=args.reason,
                )
                if args.json:
                    print(json.dumps(source.as_dict(), ensure_ascii=False, indent=2))
                else:
                    print(f"{source.name} is now {'enabled' if source.enabled else 'disabled'}.")
                return 0

            if args.news_command == "fetch":
                if not args.live:
                    payload = {
                        "status": "protected",
                        "message": (
                            "News fetch performs external site requests; "
                            "rerun with --live to acknowledge them."
                        ),
                    }
                    if args.json:
                        print(json.dumps(payload, ensure_ascii=False, indent=2))
                    else:
                        print(payload["message"], file=sys.stderr)
                    return 2
                try:
                    report = fetch_enabled_news_sources(
                        settings.database_path,
                        lookback_hours=args.lookback_hours,
                        max_items_per_source=args.max_items,
                        raw_dir=settings.news_raw_directory,
                        source_names=tuple(args.sources or ()),
                    )
                except (NewsFetchError, NewsRegistryError, OSError, ValueError) as exc:
                    print(f"News fetch failed: {exc}", file=sys.stderr)
                    return 1
                if args.json:
                    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
                else:
                    print(
                        f"News fetch completed: {len(report.outcomes)} sources; "
                        f"{report.succeeded} succeeded; {report.failed} failed; "
                        f"{report.articles_inserted} inserted; {report.articles_updated} updated."
                    )
                    for outcome in report.outcomes:
                        detail = (
                            f"error: {outcome.error}" if outcome.error else f"{outcome.items_found} items"
                        )
                        print(
                            f"- {outcome.source_name} ({outcome.fetch_kind}): "
                            f"{outcome.status}; {detail}"
                        )
                return 0

            if args.news_command == "select":
                report = evaluate_pending_news_articles(settings.database_path)
                if args.json:
                    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
                else:
                    print(
                        f"News selection completed: {report.evaluated} evaluated; "
                        f"{report.selected} selected; {report.rejected} rejected; "
                        f"daily article {'eligible' if report.daily_eligible else 'NOT eligible (<3 items)'}"
                    )
                    for reason_code, count in sorted(
                        report.rejection_counts.items(), key=lambda item: -item[1]
                    ):
                        print(f"  rejected[{reason_code}]: {count}")
                    for section, count in sorted(report.selected_by_section.items()):
                        print(f"  selected[{section}]: {count}")
                return 0

            if args.news_command == "revalidate":
                stats = revalidate_selected_news_items(settings.database_path)
                if args.json:
                    print(json.dumps(stats, ensure_ascii=False, indent=2))
                else:
                    print(
                        f"News revalidation completed: {stats['evaluated']} selected items re-checked; "
                        f"{stats['unselected']} unselected; "
                        f"{stats['priority_changed']} priority changes; "
                        f"{stats['section_changed']} section changes"
                    )
                    for reason_code, count in sorted(
                        stats["unselected_by_reason"].items(), key=lambda item: -item[1]
                    ):
                        print(f"  unselected[{reason_code}]: {count}")
                return 0

            if args.news_command == "list-items":
                items = [
                    item.as_dict()
                    for item in list_news_items(settings.database_path, selected_only=args.selected_only)
                ]
                if args.json:
                    print(json.dumps({"items": items, "count": len(items)}, ensure_ascii=False, indent=2))
                else:
                    if not items:
                        print("No news items recorded yet; run 'news fetch' then 'news select'.")
                    for item in items:
                        state = "selected" if item["selected"] else "rejected"
                        codes = ",".join(
                            reason["code"] for reason in item["rejection_reasons"]
                        )
                        suffix = f"; {codes}" if codes else ""
                        print(
                            f"- [{item['priority']}|{item['section_label']}] {item['title']} "
                            f"({item['source_name']} · {item['published_at']}) → {state}{suffix}"
                        )
                return 0

            if args.news_command == "readiness":
                payload = preview_news_readiness(settings.database_path)
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    state = "可成文" if payload["ready"] else "跳过"
                    mode = {
                        "daily": "日报",
                        "weekly": "周报形态",
                        "skip": "不成文",
                    }.get(str(payload["mode"]), str(payload["mode"]))
                    print(f"今日成文就绪度：{state}（{mode}）")
                    print(str(payload["detail"]))
                    print(
                        f"计划条目 {payload['item_count']} 条"
                        f"（目标 {payload['target_min']}–{payload['target_max']}）"
                    )
                    for item in payload["items"]:
                        print(
                            f"- [{item['priority']}|{item['section_label']}] "
                            f"{item['title']}（{item['source_name']}）"
                        )
                return 0

            if args.news_command == "build":
                if args.dry_run:
                    payload = preview_news_build(
                        settings.database_path,
                        lookback_hours=args.lookback_hours,
                        max_items=args.max_items,
                    )
                    if args.json:
                        print(json.dumps(payload, ensure_ascii=False, indent=2))
                    elif payload["status"] == "skipped":
                        print(f"Dry run skipped: {payload['detail']}")
                    else:
                        print(
                            f"Dry run ({payload['mode']}) {payload['item_count']} items "
                            f"→ {payload['slug']}: {payload['title']}"
                        )
                        print(payload["markdown"])
                    return 0
                if not args.live:
                    payload = {
                        "status": "protected",
                        "message": (
                            "News build calls the configured LLM for summaries; "
                            "rerun with --live to acknowledge the requests."
                        ),
                    }
                    if args.json:
                        print(json.dumps(payload, ensure_ascii=False, indent=2))
                    else:
                        print(payload["message"], file=sys.stderr)
                    return 2
                if not settings.llm_ready:
                    print(
                        "News build requires the analysis backend: set RADAR_LLM_PROVIDER, "
                        "RADAR_LLM_API_KEY, RADAR_LLM_BASE_URL and RADAR_LLM_MODEL in .env.",
                        file=sys.stderr,
                    )
                    return 1
                gateway = LlmGateway(
                    api_key=settings.llm_api_key,
                    base_url=settings.llm_base_url,
                    model=settings.llm_model,
                )
                try:
                    logo_path = settings.data_dir / "assets" / "earlydata-logo.png"
                    result = build_news_article(
                        settings.database_path,
                        gateway,
                        exports_dir=settings.exports_directory / "news-articles",
                        llm_model=settings.llm_model,
                        lookback_hours=args.lookback_hours,
                        max_items=args.max_items,
                        logo_path=logo_path if logo_path.is_file() else None,
                        # 原文配图默认关闭（2026-09-04）：展会列表页的 og:image
                        # 是页面级横幅，与单条内容不匹配，配了不如不配。提取链路
                        # 与测试保留，待按「正文内嵌图+内容相关性」规则实调后再开。
                    )
                except (NewsComposeError, LlmGatewayError, NewsRegistryError, OSError, ValueError) as exc:
                    print(f"News build failed: {exc}", file=sys.stderr)
                    return 1
                if args.json:
                    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
                else:
                    if result.status == "skipped":
                        print(f"News build skipped ({result.mode}): {result.detail}")
                    else:
                        print(
                            f"Built {result.mode} article v{result.version:03d}: {result.title} "
                            f"({result.item_count} items) at {result.detail}"
                        )
                        for file_name in result.files:
                            print(f"  - {file_name}")
                return 0

            if args.news_command == "qc":
                try:
                    build_info = load_news_article_build(
                        settings.database_path,
                        slug=args.slug,
                        version=args.version,
                    )
                    build_dir = Path(str(build_info["build_dir"]))
                    qc_path = build_dir / "qc-log.json"
                    if not qc_path.is_file():
                        print(f"QC log not found: {qc_path}", file=sys.stderr)
                        return 1
                    qc = json.loads(qc_path.read_text(encoding="utf-8"))
                    blockers = news_publish_blockers(build_dir)
                except (NewsComposeError, OSError, ValueError) as exc:
                    print(f"News QC failed: {exc}", file=sys.stderr)
                    return 1
                checks = qc.get("article_checks") if isinstance(qc, dict) else []
                checks = checks if isinstance(checks, list) else []
                item_checks = qc.get("item_checks") if isinstance(qc, dict) else []
                item_checks = item_checks if isinstance(item_checks, list) else []
                bad_items = [
                    check
                    for check in item_checks
                    if isinstance(check, dict)
                    and (
                        not check.get("summary_length_ok", True)
                        or not check.get("fields_complete", True)
                    )
                ]
                failed_checks = [
                    str(check.get("check"))
                    for check in checks
                    if isinstance(check, dict) and check.get("passed") is False
                ]
                # 以检查明细为准，而不是只看历史 all_passed 字段：
                # 手工修正 qc-log 后也能得到当前真实结论。
                all_passed = (
                    not failed_checks
                    if checks
                    else bool(qc.get("all_passed"))
                )
                payload = {
                    "slug": args.slug,
                    "version": args.version,
                    "all_passed": all_passed,
                    "article_checks": checks,
                    "failed_checks": failed_checks,
                    "item_checks_failed": bad_items,
                    "publish_blockers": blockers,
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    status = "通过" if payload["all_passed"] else "未通过"
                    print(f"QC {args.slug} v{args.version:03d}: {status}")
                    for check in checks:
                        if not isinstance(check, dict):
                            continue
                        mark = "OK  " if check.get("passed") else "FAIL"
                        print(
                            f"  [{mark}] {check.get('check')}: "
                            f"{check.get('detail') or ''}"
                        )
                    if bad_items:
                        print(f"  条目问题：{len(bad_items)} 条（摘要字数或字段缺失）")
                    if blockers:
                        print("发布门禁：拒绝 — " + "；".join(blockers))
                    else:
                        print("发布门禁：通过（total_length 仅为软提示）")
                return 0 if payload["all_passed"] else 1

            if args.news_command == "diff":
                try:
                    payload = diff_news_builds(
                        settings.database_path,
                        slug=args.slug,
                        from_version=args.from_version,
                        to_version=args.to_version,
                    )
                except (NewsComposeError, OSError, ValueError) as exc:
                    print(f"News diff failed: {exc}", file=sys.stderr)
                    return 1
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    counts = payload["counts"]
                    print(
                        f"Diff {payload['slug']} "
                        f"v{args.from_version:03d} → v{args.to_version:03d}: "
                        f"+{counts['added']} -{counts['removed']} ~{counts['changed']}"
                    )
                    if payload["title_changed"]:
                        print(
                            f"  标题：{payload['before_title']} → {payload['after_title']}"
                        )
                    for item in payload["added"]:
                        print(
                            f"  + [{item['priority']}] {item['title']}"
                            f"（{item['source_name']}）"
                        )
                    for item in payload["removed"]:
                        print(
                            f"  - [{item['priority']}] {item['title']}"
                            f"（{item['source_name']}）"
                        )
                    for item in payload["changed"]:
                        print(
                            f"  ~ [{item['priority']}] {item['title']}: "
                            + "、".join(item["fields"])
                        )
                return 0

            if args.news_command == "serve":
                run_news_reader_server(
                    settings.database_path, port=args.port, open_browser=args.open
                )
                return 0

            if args.news_command == "publish":
                if not args.live or not args.confirm:
                    payload = {
                        "status": "protected",
                        "message": (
                            "News publish performs WeChat API requests and always requires a "
                            "human decision: review the local article first, then rerun with "
                            "--live --confirm. It never runs inside the scheduled daily chain."
                        ),
                    }
                    if args.json:
                        print(json.dumps(payload, ensure_ascii=False, indent=2))
                    else:
                        print(payload["message"], file=sys.stderr)
                    return 2
                if not settings.wechat_ready:
                    print(
                        "News publish requires WeChat credentials: "
                        "set WECHAT_APP_ID and WECHAT_APP_SECRET in .env.",
                        file=sys.stderr,
                    )
                    return 1
                try:
                    build_info = load_news_article_build(
                        settings.database_path, slug=args.slug, version=args.version
                    )
                    if build_info["wechat_media_id"]:
                        print(
                            f"Already pushed as media_id {build_info['wechat_media_id']}; "
                            "no duplicate draft was created.",
                            file=sys.stderr,
                        )
                        return 1
                    blockers = news_publish_blockers(
                        Path(str(build_info["build_dir"]))
                    )
                    if blockers:
                        print(
                            "News publish blocked by QC: " + "；".join(blockers),
                            file=sys.stderr,
                        )
                        return 1
                    result = push_news_draft(
                        app_id=settings.wechat_app_id,
                        secret=settings.wechat_app_secret,
                        title=str(build_info["title"]),
                        html_content=Path(str(build_info["html_path"])).read_text(encoding="utf-8"),
                        cover_path=Path(str(build_info["cover_path"])),
                        digest=args.digest or "",
                        author=args.author,
                    )
                    mark_news_article_pushed(
                        settings.database_path,
                        slug=args.slug,
                        version=args.version,
                        media_id=result.media_id,
                    )
                except (NewsComposeError, WechatGatewayError, OSError, ValueError) as exc:
                    print(f"News publish failed: {exc}", file=sys.stderr)
                    return 1
                if args.json:
                    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
                else:
                    print(
                        f"Draft created for {args.slug} v{args.version:03d}: "
                        f"media_id {result.media_id}. Publishing to followers stays a manual step."
                    )
                return 0
        except (NewsRegistryError, OSError, ValueError) as exc:
            print(f"News whitelist configuration failed: {exc}", file=sys.stderr)
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

    if args.command == "signals" and args.signals_command == "analyze":
        if not args.live:
            payload = {
                "status": "protected",
                "message": (
                    "Signal analysis makes billable LLM API requests; "
                    "rerun with --live to acknowledge."
                ),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(payload["message"], file=sys.stderr)
            return 2
        if not settings.llm_ready:
            payload = {
                "status": "not_configured",
                "message": (
                    "The analysis backend is not configured for this project root; "
                    "set RADAR_LLM_API_KEY and RADAR_LLM_MODEL in .env."
                ),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(payload["message"], file=sys.stderr)
            return 1
        try:
            gateway = LlmGateway(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
            )
            result = analyze_cycle_signals(
                settings.database_path,
                gateway,
                max_signals=args.max_signals,
            )
        except (LlmGatewayError, SignalAnalysisError, MonitoringCycleError, ValueError, OSError) as exc:
            print(f"Signal analysis failed: {exc}", file=sys.stderr)
            return 1
        payload = result.as_dict()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"Signal analysis: {result.updated}/{len(result.outcomes)} signals updated; "
                f"{result.failed} failed; generator {result.generated_by}."
            )
            for outcome in result.outcomes:
                if outcome.status == "failed":
                    print(
                        f"- signal {outcome.signal_id}: failed after {outcome.attempts} attempts; "
                        f"{outcome.message}"
                    )
            print(
                "Scores, candidate states and workflow states were not changed; "
                "run `weekly brief` to render the updated interpretations."
            )
        return 1 if result.failed and not result.updated else 0

    if args.command == "weekly":
        if args.weekly_command == "list":
            cycle_connection = connect_database(settings.database_path, readonly=True)
            try:
                cycle = get_monitoring_cycle(cycle_connection, slug=args.cycle)
            finally:
                cycle_connection.close()
            if cycle is None:
                payload = {"briefs": [], "articles": [], "cycle_slug": None}
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    print("No monitoring cycle found.")
                return 0
            briefs = list_weekly_signal_briefs(
                settings.database_path,
                cycle_id=int(cycle["id"]),
            )
            articles = list_weekly_articles(
                settings.database_path,
                cycle_id=int(cycle["id"]),
            )
            payload = {
                "briefs": briefs,
                "articles": articles,
                "cycle_slug": str(cycle["slug"]),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"Weekly signal briefs: {len(briefs)}")
                for brief in briefs:
                    print(
                        f"- v{brief['version']:03d} · {brief['created_at']} · "
                        f"{brief['file_count']} files"
                    )
                print(f"Weekly articles: {len(articles)}")
                for article in articles:
                    status = "已定稿" if article["status"] == "final" else "待人工审阅"
                    print(
                        f"- v{article['version']:03d} · {article['title']} · {status} · "
                        f"{article['created_at']}"
                    )
            return 0
        if args.weekly_command == "article" and args.weekly_article_command == "generate":
            if not args.live:
                payload = {
                    "status": "protected",
                    "message": (
                        "Weekly article generation invokes the pi model and "
                        "public-web tools; rerun with --live to acknowledge."
                    ),
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    print(payload["message"], file=sys.stderr)
                return 2
            runner = PiAgentRunner(
                pi_command=[settings.pi_command],
                provider=settings.pi_provider,
                model=settings.pi_model or None,
                timeout_seconds=settings.pi_timeout_seconds,
                json_output=True,
                prompt_builder=build_weekly_article_prompt,
            )
            try:
                result = generate_weekly_article(
                    settings.database_path,
                    runner,
                    cycle_slug=args.cycle,
                    max_signals=args.max_signals,
                )
            except (
                WeeklyArticleError,
                AgentGatewayError,
                MonitoringCycleError,
                ValueError,
                OSError,
            ) as exc:
                print(f"Weekly article generation failed: {exc}", file=sys.stderr)
                return 1
            payload = result.as_dict()
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    f"Weekly article v{result.version:03d}: {result.title}; "
                    f"{result.signal_count} signals; draft at {result.markdown_path}"
                )
                print(
                    "The article is publish-ready text but remains draft_for_human_review "
                    "until you run: weekly article confirm --version "
                    f"{result.version}"
                )
                print("This workbench does not write to or publish on WeChat.")
            return 0
        if args.weekly_command == "article":
            cycle_connection = connect_database(settings.database_path, readonly=True)
            try:
                cycle = get_monitoring_cycle(
                    cycle_connection,
                    slug=args.cycle,
                )
            finally:
                cycle_connection.close()
            if cycle is None:
                print("Weekly article mutation failed: monitoring cycle not found", file=sys.stderr)
                return 1
            try:
                if args.weekly_article_command == "confirm":
                    mutation = confirm_weekly_article(
                        settings.database_path,
                        cycle_id=int(cycle["id"]),
                        version=args.version,
                    )
                else:
                    mutation = reopen_weekly_article(
                        settings.database_path,
                        cycle_id=int(cycle["id"]),
                        version=args.version,
                    )
            except (WeeklyArticleError, ValueError, OSError) as exc:
                print(f"Weekly article mutation failed: {exc}", file=sys.stderr)
                return 1
            payload = mutation.as_dict()
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                label = "已定稿" if payload["status"] == "final" else "回到待编辑"
                print(
                    f"Weekly article v{payload['version']:03d}: {label}; "
                    f"status={payload['status']}"
                )
            return 0
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

    if args.command == "research" and args.research_command == "run":
        if not args.live:
            payload = {
                "status": "protected",
                "message": (
                    "Online research invokes the pi model and public-web tools; "
                    "rerun with --live to acknowledge."
                ),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(payload["message"], file=sys.stderr)
            return 2
        runner = PiAgentRunner(
            pi_command=[settings.pi_command],
            provider=settings.pi_provider,
            model=settings.pi_model or None,
            timeout_seconds=settings.pi_timeout_seconds,
            json_output=True,
        )
        try:
            mutation = run_online_research(
                settings.database_path,
                args.task_id,
                runner,
            )
        except (ResearchWorkflowError, AgentGatewayError, ValueError, OSError) as exc:
            print(f"Online research failed: {exc}", file=sys.stderr)
            return 1
        payload = {
            "task_id": mutation.task_id,
            "draft_id": mutation.draft_id,
            "version": mutation.version,
            "task_status": mutation.task_status,
            "draft_status": mutation.draft_status,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            if mutation.draft_id:
                print(
                    f"Online research: task {mutation.task_id} -> draft V{mutation.version} "
                    f"(id {mutation.draft_id}); status {mutation.task_status}."
                )
                print("Sources stay 待补证 until a human reviews the specific pages.")
            else:
                print(
                    f"Online research failed and task {mutation.task_id} returned to "
                    "pending_start; check the research events."
                )
        return 0 if mutation.draft_id else 1

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
                pi_settings=settings,
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
