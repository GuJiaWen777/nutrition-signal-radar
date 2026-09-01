"""Discover the best news-list entry URL for browser-rendered news sources.

For every source given on the command line this script renders the current
entry URL with the headless browser, collects same-domain links whose text or
href looks like a news/media section, renders up to two candidates, and picks
the page with the most dated items according to the shared DOM parser.

Read-only: it never writes the registry.  Output is printed and saved to
``var/exports/entry-discovery-<date>.json``.

Usage:
    python scripts/discover_news_entries.py --source ADM --source 仙乐健康
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nutrition_signal_radar.config import load_settings  # noqa: E402
from nutrition_signal_radar.news_registry import list_news_sources  # noqa: E402
from nutrition_signal_radar.site_fetch_gateway import (  # noqa: E402
    _AnchorTextParser,
    _items_from_dom,
    _render_dom_with_browser,
)

_CST = timezone(timedelta(hours=8))
_NEWSISH = re.compile(r"news|press|media|资讯|新闻|动态|公告|insight|update|announcement|investor|投资者|关系", re.I)


def main() -> int:
    names = {name.strip() for name in sys.argv[1:] if not name.startswith("--")}
    settings = load_settings()
    sources = list_news_sources(settings.database_path)
    if names:
        sources = [source for source in sources if source.name in names]

    now = datetime.now(_CST)
    best: dict[str, dict[str, object]] = {}
    for source in sources:
        try:
            home_html = _render_dom_with_browser(source)
        except Exception as exc:
            print(f"[render-fail] {source.name}: {str(exc)[:90]}", flush=True)
            continue

        candidates: list[str] = []
        seen: set[str] = set()
        try:
            parser = _AnchorTextParser()
            parser.feed(home_html)
        except Exception:
            parser = None
        if parser is not None:
            for anchor in parser.anchors:
                href = str(anchor["href"]).strip()
                if not href.startswith(("http", "/")):
                    continue
                start, end = int(anchor["start"]), int(anchor["end"])
                text = parser.buffer[start:end].strip()
                if not (_NEWSISH.search(href) or _NEWSISH.search(text)):
                    continue
                absolute = requests.compat.urljoin(source.entry_url, href.split("#")[0])
                if absolute.rstrip("/") == source.entry_url.rstrip("/") or absolute in seen:
                    continue
                seen.add(absolute)
                candidates.append(absolute)

        def dated_count(html: str, entry: str) -> int:
            return sum(
                1 for item in _items_from_dom(html, replace(source, entry_url=entry), 50)
                if item.published_at
            )

        best_count = dated_count(home_html, source.entry_url)
        best_url = source.entry_url
        for candidate in candidates[:2]:
            try:
                candidate_html = _render_dom_with_browser(replace(source, entry_url=candidate))
                count = dated_count(candidate_html, candidate)
            except Exception:
                continue
            if count > best_count:
                best_count, best_url = count, candidate

        best[source.name] = {"entry_url": best_url, "dated_items": best_count}
        print(f"[{source.name}] best={best_count} dated items @ {best_url}", flush=True)

    report_dir = settings.exports_directory / "entry-discovery" / now.strftime("%Y-%m-%d")
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "entry-discovery.json").write_text(
        json.dumps(
            {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": best},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nreport: {report_dir / 'entry-discovery.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
