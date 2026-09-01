"""Article composition for the news line (Phase 4 P4-04, ADR-0014).

Selected news items are summarized by the configured analysis backend under a
strict JSON contract — the model only sees already-fetched original content
and must not add facts — then assembled by a deterministic template into the
requirement document's article structure.  Every build produces versioned
local artifacts: publishable inline-CSS HTML (WeChat editor ready), a Markdown
master, the daily source table (CSV), a QC log, and a manifest.  Publishing to
WeChat is a separate later slice; local HTML is the deliverable first.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .database import connect_database, transaction
from .llm_gateway import LlmAnalysisResult
from .news_cover import render_news_cover
from .news_selection import section_label
from .schema import initialize_schema


_CST = timezone(timedelta(hours=8), name="Asia/Shanghai")
SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
_MIN_ITEMS_FOR_DAILY = 2
_TARGET_ITEMS = 5
_SUMMARY_MIN_CHARS = 100
_SUMMARY_MAX_CHARS = 220
_IMPLICATION_MAX_CHARS = 120
_COMPOSE_MAX_RETRIES = 2
# 需求文档内容红线：不得出现的夸张词与疾病治疗类表述。
_FORBIDDEN_TERMS: tuple[str, ...] = (
    "爆款", "颠覆", "行业巨震", "史诗级", "王炸", "引爆",
    "治疗", "治愈", "疗效", "根治", "药用价值", "降三高",
)
_INTERNAL_MARKERS = ("待补证", "内部状态", "人工审阅", "未核实", "公众号发现线索")
_SYSTEM_PROMPT = (
    "你是营养健康行业资讯编辑。你只能根据用户提供的已抓取原始内容生成摘要，"
    "不得补充未出现的事实、数据或金额；不得把预测写成事实；不得把征求意见稿写成正式政策；"
    "不得对保健食品作疾病治疗类表述；不得使用“爆款”“颠覆”“行业巨震”等夸张词。"
    "必须输出 JSON 对象：{\"summary\": \"120-180字中文摘要\", "
    "\"implication\": \"对行业意味着什么（仅限基于原文的客观说明，不超过80字）\", "
    "\"keywords\": [\"2-3个行业关键词\"]}。"
)
_SECTION_ORDER = ("policy", "brand_finance", "expo", "research")
_BRAND_COLORS = {
    "title": "#173B66",
    "info": "#2F6CAE",
    "green": "#72BF44",
    "body": "#2B3545",
    "muted": "#718096",
    "panel": "#F1F6FC",
}


class NewsComposeError(ValueError):
    """Raised when an article build is misconfigured or violates its contract."""


class NewsComposeGateway(Protocol):
    """Structural type describing what compose needs from the LLM gateway."""

    def analyze(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_object: bool = True,
    ) -> LlmAnalysisResult: ...


@dataclass(frozen=True)
class ComposedItem:
    article_id: int
    source_name: str
    section: str
    priority: str
    title: str
    url: str | None
    published_at: str | None
    summary: str
    implication: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class NewsBuildResult:
    status: str  # built | skipped
    slug: str
    version: int
    mode: str
    title: str
    item_count: int
    detail: str
    files: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "slug": self.slug,
            "version": self.version,
            "mode": self.mode,
            "title": self.title,
            "item_count": self.item_count,
            "detail": self.detail,
            "files": list(self.files),
        }


def build_news_article(
    database_path: Path,
    gateway: NewsComposeGateway,
    *,
    exports_dir: Path,
    llm_model: str = "",
    lookback_hours: int = 24,
    max_items: int = _TARGET_ITEMS,
    logo_path: Path | None = None,
    actor: str = "news-build",
    now: datetime | None = None,
) -> NewsBuildResult:
    """Summarize selected items and assemble a versioned local article."""

    if lookback_hours < 1:
        raise NewsComposeError("lookback_hours must be at least 1")
    if not 1 <= max_items <= 10:
        raise NewsComposeError("max_items must be between 1 and 10")
    current_time = _ensure_cst(now) if now is not None else datetime.now(_CST)

    daily_items = _load_selected_items(database_path, current_time - timedelta(hours=lookback_hours))
    mode = "daily"
    slug = f"news-{current_time:%Y-%m-%d}"
    if len(daily_items) < _MIN_ITEMS_FOR_DAILY:
        mode = "weekly"
        slug = f"news-weekly-{(current_time - timedelta(days=6)):%Y%m%d}"
        weekly_items = _load_selected_items(
            database_path, current_time - timedelta(days=7)
        )
        items = weekly_items
    else:
        items = daily_items
    if len(items) < _MIN_ITEMS_FOR_DAILY:
        return NewsBuildResult(
            status="skipped",
            slug=slug,
            version=0,
            mode=mode,
            title="",
            item_count=len(items),
            detail=f"入选条目不足 {_MIN_ITEMS_FOR_DAILY} 条，按需求不强行生成低价值文章",
            files=(),
        )
    items = items[:max_items]

    composed, excluded = _compose_items(gateway, items)
    if len(composed) < _MIN_ITEMS_FOR_DAILY:
        return NewsBuildResult(
            status="skipped",
            slug=slug,
            version=0,
            mode=mode,
            title="",
            item_count=len(composed),
            detail="通过摘要契约的条目不足 2 条，不生成文章（详见 QC 日志）",
            files=(),
        )

    title = _build_title(mode, composed)
    article = _ArticleDocument(
        mode=mode, title=title, items=composed, generated_at=current_time
    )
    markdown = _render_markdown(article)
    html_text = _render_html(article)
    qc = _quality_checks(mode, composed, excluded, markdown)

    return _write_build(
        database_path,
        exports_dir,
        slug=slug,
        mode=mode,
        title=title,
        composed=composed,
        excluded=excluded,
        markdown=markdown,
        html_text=html_text,
        qc=qc,
        llm_model=llm_model,
        logo_path=logo_path,
        generated_at=current_time,
        actor=actor,
    )


# ---------------------------------------------------------------------------
# Candidate loading


def _load_selected_items(
    database_path: Path, since: datetime
) -> list[dict[str, object]]:
    initialize_schema(database_path)
    connection = connect_database(database_path, readonly=True)
    try:
        rows = connection.execute(
            """
            SELECT ni.article_id, ni.title, ni.url, ni.published_at,
                   ni.source_name, ni.section, ni.priority,
                   sa.content_snapshot, sa.fetched_at
            FROM news_items ni
            JOIN source_articles sa ON sa.id = ni.article_id
            WHERE ni.selected = 1 AND ni.published_at IS NOT NULL
              AND ni.published_at >= ?
            ORDER BY CASE ni.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
                     WHEN 'P2' THEN 2 ELSE 3 END,
                 ni.published_at DESC, ni.id
            """,
            (since.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _compose_items(
    gateway: NewsComposeGateway, items: list[dict[str, object]]
) -> tuple[list[ComposedItem], list[dict[str, str]]]:
    composed: list[ComposedItem] = []
    excluded: list[dict[str, str]] = []
    for item in items:
        summary = _summarize(gateway, item)
        if summary is None:
            excluded.append(
                {
                    "article_id": str(item["article_id"]),
                    "title": str(item["title"]),
                    "reason": "摘要契约未通过（格式、字数或违禁表述），已从文章剔除",
                }
            )
            continue
        composed.append(summary)
    return composed, excluded


def _summarize(
    gateway: NewsComposeGateway, item: dict[str, object]
) -> ComposedItem | None:
    user_prompt = (
        f"原始标题：{item['title']}\n"
        f"发布机构：{item['source_name']}\n"
        f"发布日期：{item['published_at']}\n"
        f"正文或摘要：{str(item['content_snapshot'] or '')[:2000]}\n"
        "请按系统约定输出 JSON。"
    )
    for _ in range(_COMPOSE_MAX_RETRIES + 1):
        try:
            result = gateway.analyze(_SYSTEM_PROMPT, user_prompt, json_object=True)
        except Exception:
            return None
        parsed = _parse_contract(result.content)
        if parsed is None:
            continue
        summary, implication, keywords = parsed
        if _contains_forbidden(summary) or _contains_forbidden(implication):
            continue
        return ComposedItem(
            article_id=int(item["article_id"]),
            source_name=str(item["source_name"]),
            section=str(item["section"]),
            priority=str(item["priority"]),
            title=str(item["title"]),
            url=str(item["url"] or "").strip() or None,
            published_at=str(item["published_at"] or "").strip() or None,
            summary=summary,
            implication=implication,
            keywords=keywords,
        )
    return None


def _parse_contract(content: str) -> tuple[str, str, tuple[str, ...]] | None:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    summary = str(payload.get("summary") or "").strip()
    implication = str(payload.get("implication") or "").strip()
    raw_keywords = payload.get("keywords")
    keywords = (
        tuple(str(word).strip() for word in raw_keywords if str(word).strip())
        if isinstance(raw_keywords, list)
        else ()
    )
    if not _SUMMARY_MIN_CHARS <= len(summary) <= _SUMMARY_MAX_CHARS:
        return None
    if not implication or len(implication) > _IMPLICATION_MAX_CHARS:
        return None
    if any(marker in summary or marker in implication for marker in _INTERNAL_MARKERS):
        return None
    return summary, implication, keywords[:3]


def _contains_forbidden(text: str) -> bool:
    return any(term in text for term in _FORBIDDEN_TERMS)


# ---------------------------------------------------------------------------
# Assembly


@dataclass(frozen=True)
class _ArticleDocument:
    mode: str
    title: str
    items: list[ComposedItem]
    generated_at: datetime


def _build_title(mode: str, composed: list[ComposedItem]) -> str:
    if mode == "weekly":
        return "本周营养健康行业资讯｜政策、财报与展会动态一览"
    focus = _focus_from_title(composed[0].title)
    return f"营养健康行业资讯｜{focus}"


def _focus_from_title(title: str, limit: int = 26) -> str:
    """Derive the article-title focus from the top item's headline.

    Strips editorial markers (【2026-08-31】-style repost prefixes), prefers
    the last "|"-separated segment (site section prefixes like "分会 | "), and
    truncates at a closing punctuation boundary instead of mid-phrase.
    """

    cleaned = str(title or "").strip()
    cleaned = re.sub(r"^(【[^】]*】|\[[^\]]*\])\s*", "", cleaned)
    parts = re.split(r"\s*[｜|]\s*", cleaned)
    if len(parts) > 1:
        tail = parts[-1].strip()
        if len(tail) >= max(8, limit // 2):
            cleaned = tail
    cleaned = cleaned.strip()
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    for i in range(len(cut), int(limit * 0.5), -1):
        if cut[i - 1] in "”》）」，、；：":
            return cut[:i]
    return cut.rstrip("（“「")


def _grouped_sections(items: list[ComposedItem]) -> list[tuple[str, list[ComposedItem]]]:
    groups: list[tuple[str, list[ComposedItem]]] = []
    for section in _SECTION_ORDER:
        members = [item for item in items if item.section == section]
        if members:
            groups.append((section, members))
    return groups


def _article_keywords(items: list[ComposedItem]) -> list[str]:
    from .news_selection import INDUSTRY_TERMS

    counts: dict[str, int] = {}
    for item in items:
        text = f"{item.title}{item.summary}"
        for term in INDUSTRY_TERMS:
            if term in text:
                counts[term] = counts.get(term, 0) + 1
    ranked = [term for term, _ in sorted(counts.items(), key=lambda pair: -pair[1])]
    keywords = [item for item in ranked if 2 <= len(item) <= 8][:4]
    for label in (section_label(section) for section in _SECTION_ORDER):
        if len(keywords) >= 4:
            break
        if label not in keywords:
            keywords.append(label)
    return keywords[:4]


def _render_markdown(article: _ArticleDocument) -> str:
    lines: list[str] = [
        f"# {article.title}",
        "",
        f"营养健康行业资讯 · {article.generated_at:%Y.%m.%d}",
        "",
        "## 导语",
        "",
        _lead_paragraph(article),
        "",
    ]
    for index, (section, members) in enumerate(_grouped_sections(article.items), start=1):
        lines.append(f"## {index:02d}｜{section_label(section)}")
        lines.append("")
        for item in members:
            lines.append(f"### {item.title}")
            lines.append("")
            lines.append(item.summary)
            lines.append("")
            lines.append(f"对行业意味着什么：{item.implication}")
            lines.append("")
            source_date = item.published_at[:10] if item.published_at else "日期见原文"
            lines.append(f"来源：{item.source_name}｜{source_date}")
            if item.url:
                lines.append(f"[查看原文 →]({item.url})")
            lines.append("")
    keywords = _article_keywords(article.items)
    lines.append("## 本期关键词")
    lines.append("")
    lines.append("｜".join(keywords))
    lines.append("")
    lines.append("## 信息说明")
    lines.append("")
    lines.append("本文根据公开信息整理，具体内容以原始发布方为准。")
    return "\n".join(lines) + "\n"


def _lead_paragraph(article: _ArticleDocument) -> str:
    involved = "、".join(section_label(section) for section, _ in _grouped_sections(article.items))
    return (
        f"本期收录 {len(article.items)} 条营养健康产业公开资讯，"
        f"涉及{involved}。"
    )


def _render_html(article: _ArticleDocument) -> str:
    """Render the requirement document's inline-CSS WeChat-ready layout."""

    def esc(value: str) -> str:
        return html.escape(str(value), quote=False)

    sections_html: list[str] = []
    for index, (section, members) in enumerate(_grouped_sections(article.items), start=1):
        blocks: list[str] = []
        for item in members:
            source_date = item.published_at[:10] if item.published_at else "日期见原文"
            link_html = (
                f'<a href="{html.escape(item.url, quote=True)}" style="color:{_BRAND_COLORS["info"]};'
                f'font-size:14px;text-decoration:none;">查看原文 →</a>'
                if item.url
                else ""
            )
            blocks.append(
                f'<section style="margin:0 0 26px;">'
                f'<p style="margin:0 0 8px;font-size:17px;font-weight:700;color:{_BRAND_COLORS["title"]};'
                f'line-height:1.6;">{esc(item.title)}</p>'
                f'<p style="margin:0 0 10px;font-size:16px;color:{_BRAND_COLORS["body"]};line-height:1.9;">'
                f"{esc(item.summary)}</p>"
                f'<section style="margin:0 0 10px;padding:10px 14px;background:{_BRAND_COLORS["panel"]};'
                f'border-left:3px solid {_BRAND_COLORS["green"]};">'
                f'<p style="margin:0;font-size:14px;color:{_BRAND_COLORS["body"]};line-height:1.8;">'
                f"<strong>对行业意味着什么：</strong>{esc(item.implication)}</p></section>"
                f'<p style="margin:0 0 4px;font-size:13px;color:{_BRAND_COLORS["muted"]};">'
                f"来源：{esc(item.source_name)}｜{esc(source_date)}</p>"
                f"{link_html}</section>"
            )
        sections_html.append(
            f'<section style="margin:0 0 8px;"><p style="margin:0 0 14px;font-size:19px;font-weight:700;'
            f'color:{_BRAND_COLORS["title"]};">{index:02d}｜{esc(section_label(section))}</p></section>'
            + "".join(blocks)
        )

    keywords = " ｜ ".join(
        f'<span style="display:inline-block;margin:0 6px 6px 0;padding:3px 10px;'
        f'background:{_BRAND_COLORS["panel"]};color:{_BRAND_COLORS["info"]};font-size:13px;'
        f'border-radius:4px;">{esc(word)}</span>'
        for word in _article_keywords(article.items)
    )
    return (
        f'<section style="max-width:660px;margin:0 auto;background:#FFFFFF;'
        f"font-family:'Noto Sans SC','PingFang SC','Microsoft YaHei',Arial,Helvetica,sans-serif;\">"
        f'<section style="padding:28px 18px 18px;">'
        f'<p style="margin:0 0 6px;font-size:13px;color:{_BRAND_COLORS["info"]};letter-spacing:0.1em;">'
        f"EARLYDATA · 营养健康行业资讯</p>"
        f'<h1 style="margin:0 0 8px;font-size:25px;font-weight:700;color:{_BRAND_COLORS["title"]};'
        f'line-height:1.4;text-align:left;">{esc(article.title)}</h1>'
        f'<p style="margin:0;font-size:14px;color:{_BRAND_COLORS["muted"]};">'
        f"{article.generated_at:%Y.%m.%d}｜政策 · 品牌 · 展会</p></section>"
        f'<section style="margin:0 18px 22px;padding:14px 16px;background:{_BRAND_COLORS["panel"]};'
        f'border-radius:6px;"><p style="margin:0;font-size:16px;color:{_BRAND_COLORS["body"]};'
        f'line-height:1.8;">{esc(_lead_paragraph(article))}</p></section>'
        f'<section style="padding:0 18px;">{"".join(sections_html)}</section>'
        f'<section style="padding:4px 18px 8px;">'
        f'<p style="margin:0 0 8px;font-size:15px;font-weight:700;color:{_BRAND_COLORS["title"]};">'
        f"本期关键词</p><p style=\"margin:0;line-height:2;\">{keywords}</p></section>"
        f'<section style="padding:6px 18px 30px;">'
        f'<p style="margin:0;font-size:12px;color:{_BRAND_COLORS["muted"]};line-height:1.7;">'
        f"信息说明：本文根据公开信息整理，具体内容以原始发布方为准。</p></section>"
        f"</section>"
    )


# ---------------------------------------------------------------------------
# QC + versioned export


def _quality_checks(
    mode: str,
    composed: list[ComposedItem],
    excluded: list[dict[str, str]],
    markdown: str,
) -> dict[str, object]:
    text_length = len(re.sub(r"[#>*\[\]()|！？\s-]", "", markdown))
    item_checks = [
        {
            "article_id": item.article_id,
            "title": item.title,
            "summary_length": len(item.summary),
            "summary_length_ok": _SUMMARY_MIN_CHARS <= len(item.summary) <= _SUMMARY_MAX_CHARS,
            "fields_complete": bool(item.source_name and item.published_at and item.url),
            "implication_length": len(item.implication),
        }
        for item in composed
    ]
    body_text = re.sub(r"[#>*\[\]()！！?？\s]", "", markdown)
    found_forbidden = sorted({term for term in _FORBIDDEN_TERMS if term in body_text})
    article_checks = [
        {"check": "item_count", "passed": 3 <= len(composed) <= 5,
         "detail": f"{len(composed)} 条（需求建议 3–5 条）"},
        {"check": "total_length", "passed": 1000 <= text_length <= 1800 or mode == "weekly",
         "detail": f"正文约 {text_length} 字（需求建议 1000–1800 字）"},
        {"check": "no_forbidden_terms", "passed": not found_forbidden,
         "detail": "、".join(found_forbidden) if found_forbidden else "未发现夸张词或疾病治疗表述"},
        {"check": "sources_complete", "passed": all(item["fields_complete"] for item in item_checks),
         "detail": "每条均有机构、日期与链接"},
    ]
    return {
        "item_checks": item_checks,
        "article_checks": article_checks,
        "excluded_items": excluded,
        "all_passed": all(check["passed"] for check in article_checks),
    }


def _write_build(
    database_path: Path,
    exports_dir: Path,
    *,
    slug: str,
    mode: str,
    title: str,
    composed: list[ComposedItem],
    excluded: list[dict[str, str]],
    markdown: str,
    html_text: str,
    qc: dict[str, object],
    llm_model: str,
    logo_path: Path | None,
    generated_at: datetime,
    actor: str,
) -> NewsBuildResult:
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM news_articles WHERE slug = ?", (slug,)
            ).fetchone()
            version = int(row["v"]) + 1
            build_dir = Path(exports_dir) / slug / f"v{version:03d}"
            build_dir.mkdir(parents=True, exist_ok=True)

            html_path = build_dir / "news-article.html"
            md_path = build_dir / "news-article.md"
            csv_path = build_dir / "source-table.csv"
            qc_path = build_dir / "qc-log.json"
            html_path.write_text(html_text, encoding="utf-8")
            md_path.write_text(markdown, encoding="utf-8")
            _write_source_table(database_path, csv_path, composed)

            cover_status = _render_cover(
                build_dir,
                title=title,
                date_text=f"{generated_at:%Y.%m.%d}",
                logo_path=logo_path,
            )

            manifest = {
                "slug": slug,
                "version": version,
                "mode": mode,
                "title": title,
                "llm_model": llm_model,
                "items": [item.__dict__ for item in composed],
                "excluded_items": excluded,
                "qc": qc,
                "cover": cover_status,
            }
            manifest_path = build_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            qc_path.write_text(
                json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            file_count = len(list(build_dir.iterdir()))
            files = ["news-article.html", "news-article.md", "source-table.csv"]
            if cover_status.get("status") == "included":
                files.append("cover.png")
                if cover_status.get("engine") == "html_browser":
                    files.append("cover.html")
            files += ["qc-log.json", "manifest.json"]
            connection.execute(
                """
                INSERT INTO news_articles(
                    slug, version, mode, title, item_count, llm_model,
                    relative_path, manifest_sha256, file_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slug,
                    version,
                    mode,
                    title,
                    len(composed),
                    llm_model,
                    str(build_dir),
                    manifest_sha256,
                    file_count,
                ),
            )
    finally:
        connection.close()
    return NewsBuildResult(
        status="built",
        slug=slug,
        version=version,
        mode=mode,
        title=title,
        item_count=len(composed),
        detail=str(build_dir),
        files=tuple(files),
    )


def load_news_article_build(database_path: Path, *, slug: str, version: int) -> dict[str, object]:
    """Load one versioned build row plus its artifact paths for publishing."""

    initialize_schema(database_path)
    connection = connect_database(database_path, readonly=True)
    try:
        row = connection.execute(
            """
            SELECT id, slug, version, mode, title, item_count, relative_path,
                   wechat_media_id, wechat_pushed_at
            FROM news_articles WHERE slug = ? AND version = ?
            """,
            (slug, version),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise NewsComposeError(f"成稿不存在：{slug} v{version:03d}")
    build_dir = Path(str(row["relative_path"]))
    return {
        "id": int(row["id"]),
        "slug": str(row["slug"]),
        "version": int(row["version"]),
        "mode": str(row["mode"]),
        "title": str(row["title"]),
        "item_count": int(row["item_count"]),
        "build_dir": build_dir,
        "html_path": build_dir / "news-article.html",
        "cover_path": build_dir / "cover.png",
        "wechat_media_id": str(row["wechat_media_id"] or "").strip() or None,
        "wechat_pushed_at": str(row["wechat_pushed_at"] or "").strip() or None,
    }


def mark_news_article_pushed(
    database_path: Path, *, slug: str, version: int, media_id: str
) -> None:
    """Record a successful draft-box push on the build row."""

    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            cursor = connection.execute(
                f"""
                UPDATE news_articles
                SET wechat_media_id = ?, wechat_pushed_at = ({SQL_NOW})
                WHERE slug = ? AND version = ?
                """,
                (media_id, slug, version),
            )
            if cursor.rowcount != 1:
                raise NewsComposeError(f"成稿不存在：{slug} v{version:03d}")
    finally:
        connection.close()


def _render_cover(
    build_dir: Path, *, title: str, date_text: str, logo_path: Path | None
) -> dict[str, object]:
    """Render the 1800×766 brand cover; a failure never aborts the build."""

    cover_path = build_dir / "cover.png"
    logo_note = "logo included" if logo_path and Path(logo_path).is_file() else "logo not configured"
    try:
        result = render_news_cover(
            cover_path, article_title=title, date_text=date_text, logo_path=logo_path
        )
    except Exception as exc:
        return {"status": "failed", "logo": logo_note, "error": str(exc)[:300]}
    status: dict[str, object] = {
        "status": "included",
        "engine": result.engine,
        "logo": logo_note,
        "file": "cover.png",
    }
    if (build_dir / "cover.html").is_file():
        status["html_preview"] = "cover.html"
    return status


def _write_source_table(
    database_path: Path, csv_path: Path, composed: list[ComposedItem]
) -> None:
    """Export the requirement document's daily source table for this build."""

    summaries = {item.article_id: item.summary for item in composed}
    initialize_schema(database_path)
    connection = connect_database(database_path, readonly=True)
    try:
        rows = connection.execute(
            """
            SELECT ni.id, ni.title, ni.source_name, ni.published_at, ni.url,
                   ni.section, ni.selected, sa.fetched_at
            FROM news_items ni
            JOIN source_articles sa ON sa.id = ni.article_id
            ORDER BY ni.selected DESC, ni.priority, ni.published_at DESC
            """
        ).fetchall()
    finally:
        connection.close()
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["标题", "来源", "日期", "URL", "抓取时间", "分类", "摘要", "是否入选"])
        for row in rows:
            writer.writerow(
                [
                    row["title"],
                    row["source_name"],
                    row["published_at"] or "",
                    row["url"] or "",
                    row["fetched_at"] or "",
                    section_label(str(row["section"])),
                    summaries.get(int(row["id"]), ""),
                    "是" if row["selected"] else "否",
                ]
            )


def _ensure_cst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_CST)
    return value.astimezone(_CST)


__all__ = [
    "ComposedItem",
    "NewsBuildResult",
    "NewsComposeError",
    "build_news_article",
]
