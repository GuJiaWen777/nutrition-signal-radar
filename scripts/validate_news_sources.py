"""Live validation sweep for the news-line whitelist (ADR-0014 operator tool).

For every configured news source (disabled ones included) this script performs
one bounded direct fetch via the site gateway and records whether the parser
works on the real page.  It is strictly read-only: no ingest, no health
writes, no enablement changes — conclusions never feed back into the registry
automatically (same discipline as the RedFox Phase-0 validation).

Usage:
    python scripts/validate_news_sources.py                 # all sources, 365-day window
    python scripts/validate_news_sources.py --source 巨潮资讯网
    python scripts/validate_news_sources.py --lookback-days 30

Report: var/exports/news-source-validation/<date>/validation.{json,md}
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nutrition_signal_radar.config import load_settings  # noqa: E402
from nutrition_signal_radar.news_registry import list_news_sources  # noqa: E402
from nutrition_signal_radar.site_fetch_gateway import (  # noqa: E402
    fetch_site_source,
)

_CST = timezone(timedelta(hours=8))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source", action="append", dest="sources", help="Restrict to named sources.")
    parser.add_argument("--lookback-days", type=int, default=365, help="Wide validation window (default: 365).")
    parser.add_argument("--max-items", type=int, default=10, help="Items kept per source (default: 10).")
    args = parser.parse_args()

    settings = load_settings(args.project_root)
    sources = list_news_sources(settings.database_path)
    wanted = {name.strip() for name in (args.sources or [])}
    if wanted:
        sources = [source for source in sources if source.name in wanted]
    now = datetime.now(_CST)
    raw_dir = settings.news_raw_directory / "validation"
    http_session = requests.Session()

    rows = []
    for source in sources:
        record = {
            "name": source.name,
            "domain": source.domain,
            "category": source.category,
            "fetch_kind": source.fetch_kind,
            "enabled": source.enabled,
            "entry_url": source.entry_url,
            "verdict": "",
            "items_found": 0,
            "skipped_no_date": 0,
            "skipped_out_of_window": 0,
            "error": "",
        }
        try:
            batch = fetch_site_source(
                session=http_session,
                source=source,
                lookback_hours=max(1, args.lookback_days * 24),
                max_items=args.max_items,
                raw_dir=raw_dir,
                now=now,
            )
            record["items_found"] = len(batch.items)
            record["skipped_no_date"] = batch.skipped_no_date
            record["skipped_out_of_window"] = batch.skipped_out_of_window
            if batch.items:
                record["verdict"] = "parser_ok"
            elif batch.skipped_no_date and not batch.skipped_out_of_window:
                record["verdict"] = "parser_no_dates"
            else:
                record["verdict"] = "no_recent_items"
        except Exception as exc:
            record["verdict"] = "fetch_failed"
            record["error"] = str(exc)[:300]
        rows.append(record)
        print(
            f"[{record['verdict']}] {source.name} ({source.fetch_kind}): "
            f"items={record['items_found']} no_date={record['skipped_no_date']} "
            f"out_of_window={record['skipped_out_of_window']}"
            + (f" error={record['error']}" if record["error"] else ""),
            flush=True,
        )

    report_dir = settings.exports_directory / "news-source-validation" / now.strftime("%Y-%m-%d")
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "lookback_days": args.lookback_days,
        "results": rows,
    }
    (report_dir / "validation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 资讯白名单活体校验报告",
        "",
        f"生成时间：{now:%Y-%m-%d %H:%M}｜窗口：近 {args.lookback_days} 天｜结论不写入来源配置。",
        "",
        "| 信源 | 类别 | 方式 | 启用 | 结论 | 条目 | 无日期 | 窗口外 | 错误 |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['name']} | {row['category']} | {row['fetch_kind']} | "
            f"{'是' if row['enabled'] else '否'} | {row['verdict']} | {row['items_found']} | "
            f"{row['skipped_no_date']} | {row['skipped_out_of_window']} | {row['error'][:80]} |"
        )
    (report_dir / "validation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nreport: {report_dir / 'validation.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
