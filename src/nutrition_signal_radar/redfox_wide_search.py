"""Bounded, read-only reports for RedFox's broad public-account article library.

The broad endpoint searches article titles, summaries, and authors.  It is
useful for checking whether a public-account name appears outside RedFox's
quality library, but it must never silently turn a search result into an
enabled monitoring source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Protocol

from .redfox_gateway import (
    REDFOX_WIDE_SORT_TYPES,
    RedFoxListedWork,
    RedFoxWideSearchPage,
    normalize_account_name,
)


_CST = timezone(timedelta(hours=8), name="Asia/Shanghai")


class RedFoxWideSearchGateway(Protocol):
    def search_wide_articles(
        self,
        keyword: str,
        *,
        offset: int = 0,
        sort_type: str = "2",
    ) -> RedFoxWideSearchPage: ...


@dataclass(frozen=True)
class RedFoxWideSearchArticle:
    title: str
    author: str
    published_at: str | None
    work_uuid: str | None
    work_url: str | None
    source_url: str | None
    has_inline_content: bool
    read_count: int
    like_count: int
    watch_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "author": self.author,
            "published_at": self.published_at,
            "work_uuid": self.work_uuid,
            "work_url": self.work_url,
            "source_url": self.source_url,
            "has_inline_content": self.has_inline_content,
            "read_count": self.read_count,
            "like_count": self.like_count,
            "watch_count": self.watch_count,
        }


@dataclass(frozen=True)
class RedFoxWideAuthorSummary:
    author: str
    article_count: int
    latest_published_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "author": self.author,
            "article_count": self.article_count,
            "latest_published_at": self.latest_published_at,
        }


@dataclass(frozen=True)
class RedFoxWideSearchReport:
    generated_at: str
    keyword: str
    sort_type: str
    exact_author: str | None
    total_reported_by_provider: int | None
    pages_fetched: int
    pagination_truncated: bool
    results: tuple[RedFoxWideSearchArticle, ...]
    authors: tuple[RedFoxWideAuthorSummary, ...]
    exact_author_match_count: int
    raw_snapshot_paths: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "keyword": self.keyword,
            "sort_type": self.sort_type,
            "exact_author": self.exact_author,
            "total_reported_by_provider": self.total_reported_by_provider,
            "pages_fetched": self.pages_fetched,
            "pagination_truncated": self.pagination_truncated,
            "result_count": len(self.results),
            "exact_author_match_count": self.exact_author_match_count,
            "results": [result.as_dict() for result in self.results],
            "authors": [author.as_dict() for author in self.authors],
            "raw_snapshot_paths": list(self.raw_snapshot_paths),
        }


@dataclass(frozen=True)
class RedFoxWideSearchArtifacts:
    json_path: Path
    markdown_path: Path

    def as_dict(self) -> dict[str, str]:
        return {"json_path": str(self.json_path), "markdown_path": str(self.markdown_path)}


def search_redfox_wide_library(
    gateway: RedFoxWideSearchGateway,
    *,
    keyword: str,
    max_pages: int = 1,
    sort_type: str = "2",
    exact_author: str | None = None,
) -> RedFoxWideSearchReport:
    """Read a small number of broad-search pages and summarize non-secret fields."""

    cleaned_keyword = str(keyword).strip()
    cleaned_author = str(exact_author or "").strip() or None
    if not cleaned_keyword:
        raise ValueError("RedFox broad search requires a keyword")
    if max_pages < 1:
        raise ValueError("RedFox broad-search max_pages must be at least 1")
    if sort_type not in REDFOX_WIDE_SORT_TYPES:
        allowed = ", ".join(REDFOX_WIDE_SORT_TYPES)
        raise ValueError(f"RedFox broad-search sort_type must be one of: {allowed}")

    pages: list[RedFoxWideSearchPage] = []
    for page_number in range(max_pages):
        page = gateway.search_wide_articles(
            cleaned_keyword,
            offset=page_number * 20,
            sort_type=sort_type,
        )
        pages.append(page)
        if not page.has_more:
            break

    works = tuple(work for page in pages for work in page.works)
    results = tuple(_search_article(work) for work in works)
    authors = _author_summaries(results)
    exact_count = sum(
        normalize_account_name(result.author) == normalize_account_name(cleaned_author)
        for result in results
    ) if cleaned_author else 0
    raw_paths = tuple(
        dict.fromkeys(page.raw_snapshot_path for page in pages if page.raw_snapshot_path)
    )
    return RedFoxWideSearchReport(
        generated_at=datetime.now(_CST).isoformat(timespec="seconds"),
        keyword=cleaned_keyword,
        sort_type=sort_type,
        exact_author=cleaned_author,
        total_reported_by_provider=pages[0].total if pages else None,
        pages_fetched=len(pages),
        pagination_truncated=bool(pages and pages[-1].has_more and len(pages) >= max_pages),
        results=results,
        authors=authors,
        exact_author_match_count=exact_count,
        raw_snapshot_paths=raw_paths,
    )


def write_redfox_wide_search_report(
    report: RedFoxWideSearchReport,
    exports_directory: Path,
) -> RedFoxWideSearchArtifacts:
    """Replace the latest discovery report atomically under local runtime state."""

    directory = Path(exports_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "redfox_wide_search.json"
    markdown_path = directory / "redfox_wide_search.md"
    _write_text_atomically(json_path, json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n")
    _write_text_atomically(markdown_path, render_redfox_wide_search_markdown(report))
    return RedFoxWideSearchArtifacts(json_path=json_path, markdown_path=markdown_path)


def render_redfox_wide_search_markdown(report: RedFoxWideSearchReport) -> str:
    """Render an operator-facing discovery report without article bodies or credentials."""

    lines = [
        "# RedFox 广域库搜索（只读）",
        "",
        f"生成时间：{report.generated_at}",
        f"关键词：{_cell(report.keyword)}",
        f"排序：{_sort_label(report.sort_type)}",
        f"页面：{report.pages_fetched} 页"
        + ("（已触及设定页数上限）" if report.pagination_truncated else ""),
        f"提供方首个响应报告的结果总数：{report.total_reported_by_provider if report.total_reported_by_provider is not None else '未提供'}。",
        "",
        "本报告仅用于发现与人工核验：不会写入来源配置、SQLite、选题、研究或发布流程。",
    ]
    if report.exact_author:
        lines.extend(
            (
                "",
                f"精确作者筛选：{_cell(report.exact_author)}；当前页中精确作者结果：{report.exact_author_match_count} 篇。",
                "即使存在精确结果，也仍须由操作者人工确认后才可配置或启用监测来源。",
            )
        )

    lines.extend(("", "## 作者分布", "", "| 作者 | 本页结果 | 最近发布时间 |", "| --- | ---: | --- |"))
    if report.authors:
        for author in report.authors:
            lines.append(
                f"| {_cell(author.author or '未提供')} | {author.article_count} | "
                f"{_cell(author.latest_published_at or '—')} |"
            )
    else:
        lines.append("| 未返回结果 | 0 | — |")

    lines.extend(("", "## 返回文章（不含正文）", "", "| 作者 | 标题 | 发布时间 | 正文 | 原文 |", "| --- | --- | --- | --- | --- |"))
    if report.results:
        for result in report.results:
            destination = result.work_url or result.source_url or ""
            link = f"[打开]({destination})" if destination.startswith(("https://", "http://")) else "—"
            lines.append(
                f"| {_cell(result.author or '未提供')} | {_cell(result.title or '未提供')} | "
                f"{_cell(result.published_at or '—')} | {'有' if result.has_inline_content else '无'} | {link} |"
            )
    else:
        lines.append("| — | 未返回文章 | — | — | — |")
    lines.append("")
    return "\n".join(lines)


def _search_article(work: RedFoxListedWork) -> RedFoxWideSearchArticle:
    payload: Mapping[str, object] = work.payload
    return RedFoxWideSearchArticle(
        title=_clean_text(payload.get("title")),
        author=_clean_text(payload.get("author")),
        published_at=_clean_text(payload.get("publishTime")) or None,
        work_uuid=_clean_text(payload.get("workUuid")) or None,
        work_url=_clean_text(payload.get("workUrl")) or None,
        source_url=_clean_text(payload.get("sourceUrl")) or None,
        has_inline_content=bool(_clean_text(payload.get("content"))),
        read_count=_nonnegative_int(payload.get("readCount")),
        like_count=_nonnegative_int(payload.get("likeCount")),
        watch_count=_nonnegative_int(payload.get("watchCount")),
    )


def _author_summaries(
    results: tuple[RedFoxWideSearchArticle, ...],
) -> tuple[RedFoxWideAuthorSummary, ...]:
    grouped: dict[str, list[RedFoxWideSearchArticle]] = {}
    for result in results:
        grouped.setdefault(result.author or "未提供", []).append(result)
    summaries = [
        RedFoxWideAuthorSummary(
            author=author,
            article_count=len(items),
            latest_published_at=max((item.published_at for item in items if item.published_at), default=None),
        )
        for author, items in grouped.items()
    ]
    return tuple(sorted(summaries, key=lambda item: (-item.article_count, item.author)))


def _sort_label(value: str) -> str:
    return {"0": "默认", "2": "最新", "4": "最热"}.get(value, value)


def _write_text_atomically(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(float(str(value).strip().replace(",", ""))))
    except (TypeError, ValueError, OverflowError):
        return 0
