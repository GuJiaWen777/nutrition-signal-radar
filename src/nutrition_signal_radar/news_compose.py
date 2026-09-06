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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin

import requests

from .database import connect_database, transaction
from .llm_gateway import LlmAnalysisResult
from .news_cover import render_news_cover
from .news_selection import (
    is_same_news_event,
    normalize_news_title,
    section_label,
    url_identity_keys,
)
from .schema import initialize_schema
from .site_fetch_gateway import USER_AGENT


_CST = timezone(timedelta(hours=8), name="Asia/Shanghai")
SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
_MIN_ITEMS_TO_BUILD = 2
# 单篇内同一信源最多入选条数：避免单一来源霸版，保证来源多样性。
_MAX_ITEMS_PER_SOURCE = 2
_MIN_ITEMS_FOR_DAILY = 3
# 焦点标题软上限（系列前缀之外）：超限时优先请模型压缩改写，机械截断兜底。
_TITLE_SOFT_LIMIT = 40
_TITLE_REWRITE_SYSTEM_PROMPT = (
    "你是微信公众号资讯标题编辑。只对给定原标题做压缩改写："
    "不得新增任何事实、数字、机构或产品名，不得改变原意，"
    "不得使用夸张、疾病治疗类表述或省略号。"
    '输出 JSON 对象：{"title": "改写后的标题"}。'
)
_MIN_ITEMS_FOR_DAILY = 3
# 成文侧正文硬闸：快照去标签去空白后不足 60 字（与筛选层「已有真实正文」
# 的 60 字标准一致）或正文与标题同文的条目，无法支撑 100-180 字摘要契约，
# 在进入模型前剔除——「35 字进、150 字出」只会逼出复述式注水摘要。
_SNAPSHOT_MIN_CHARS = 60
_TARGET_ITEMS = 5
_WEEKLY_LOOKBACK_DAYS = 7
_SUMMARY_MIN_CHARS = 100
_SUMMARY_MAX_CHARS = 180
_IMPLICATION_MAX_CHARS = 45
_COMPOSE_MAX_RETRIES = 2
# 填充话术标记：条目只有标题时模型的典型敷衍输出。出现即视为该条
# 没有可发布的实质内容（与筛选层 missing_body 同源的最后一道闸）。
_PADDING_MARKERS: tuple[str, ...] = (
    "仅有标题", "无实质", "无法判断", "暂无法", "信息有限",
    "原文与标题", "未包含更多", "等待正式文本", "未提供更多",
    "标题显示", "现有信息仅", "未从现有信息", "需查阅", "未提供具体",
    "待查阅", "未公布", "无从得知", "与标题相同", "与标题一致", "正文仅",
)
# 需求文档内容红线：不得出现的夸张词与疾病治疗类表述。
_FORBIDDEN_TERMS: tuple[str, ...] = (
    "爆款", "颠覆", "行业巨震", "史诗级", "王炸", "引爆",
    "治疗", "治愈", "疗效", "根治", "药用价值", "降三高",
)
_INTERNAL_MARKERS = ("待补证", "内部状态", "人工审阅", "未核实", "公众号发现线索")
_SYSTEM_PROMPT = (
    "你是营养健康行业资讯编辑。你只能根据用户提供的已抓取原始内容生成摘要，"
    "不得补充未出现的事实、数据或金额；不得把预测写成事实；不得把征求意见稿写成正式政策；"
    "不得对保健食品作疾病治疗类表述；不得使用“爆款”“颠覆”“行业巨震”等夸张词；"
    "摘要第一句必须写清主体和动作，禁止“反映出”“有助于激发”等空泛套话。"
    "必须输出 JSON 对象：{\"summary\": \"100-150字中文摘要：第一句写清主体与动作，"
    "只保留关键数字与期限，删掉一切套话\", "
    "\"implication\": \"对行业意味着什么（仅限基于原文的客观说明，不超过45字）\", "
    "\"keywords\": [\"2-3个行业关键词\"], "
    "\"key_facts\": [{\"kind\": \"number|deadline|entity\", "
    "\"value\": \"原文中出现的原文片段（数字、日期或主体名，必须逐字来自原文）\", "
    "\"note\": \"一句话说明（不超过30字）\"}]。key_facts 可为空数组；"
    "value 逐字复制原文片段，禁止改写。"
    "\"headline\": \"用于正文展示的新闻标题（不超过30字）：若原标题含【日期】前缀、"
    "竖线分段等编辑痕迹，改写为干净的新闻标题；若原标题本身干净则逐字返回；不得新增事实。\""
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
    display_title: str = ""
    key_facts: tuple[dict[str, str], ...] = ()
    # news_items.id：跨期排除的关联键（article_id 是 source_articles.id，两者不同）。
    news_item_id: int = 0
    # 合并条目带全部成员的 news_items.id（含单条情形），跨期排除逐成员记账。
    news_item_ids: tuple[int, ...] = ()
    # 原文配图（og:image 或正文首图，取自原文 URL，微信编辑器粘贴时自动转存）。
    image_url: str = ""


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
    fetch_lead_images: bool = False,
) -> NewsBuildResult:
    """Summarize selected items and assemble a versioned local article.

    ``fetch_lead_images`` 打开时，对入选条目逐条请求原文详情页提取配图
    （最多 max_items 条、单条 12 秒超时、失败静默降级为纯文字）。
    """

    if lookback_hours < 1:
        raise NewsComposeError("lookback_hours must be at least 1")
    if not 1 <= max_items <= 10:
        raise NewsComposeError("max_items must be between 1 and 10")
    current_time = _ensure_cst(now) if now is not None else datetime.now(_CST)

    # Slug 先于候选池确定：加载层要排除「已被其他期文章消耗」的条目，
    # 同 slug 重build（当天修复后重出 v2+）豁免，保证重试看到同一候选池。
    daily_slug = _daily_slug_for(current_time)
    weekly_slug = _weekly_slug_for(current_time)
    daily_items, daily_dupes = _dedupe_items(
        _load_selected_items(
            database_path,
            current_time - timedelta(hours=lookback_hours),
            as_of=current_time,
        )
    )
    weekly_items, weekly_dupes = _dedupe_items(
        _load_selected_items(
            database_path,
            current_time - timedelta(days=_WEEKLY_LOOKBACK_DAYS),
            as_of=current_time,
        )
    )
    # 先合并同模板公告、再过正文硬闸：两条「仅有标题」的同款备案公示
    # 合并后即有可写素材，单条薄料则在此出局。
    daily_items, daily_merges = _merge_routine_filings(daily_items)
    weekly_items, weekly_merges = _merge_routine_filings(weekly_items)
    daily_items, daily_thin = _filter_thin_inputs(daily_items)
    weekly_items, weekly_thin = _filter_thin_inputs(weekly_items)
    merged_records: list[dict[str, object]] = []
    seen_merges: set[str] = set()
    for record in (*daily_merges, *weekly_merges):
        if str(record["title"]) in seen_merges:
            continue
        merged_records.append(record)
        seen_merges.add(str(record["title"]))
    mode, items = _choose_item_pool(daily_items, weekly_items, max_items)
    slug = daily_slug if mode == "daily" else weekly_slug
    if mode == "skip" or len(items) < _MIN_ITEMS_TO_BUILD:
        return NewsBuildResult(
            status="skipped",
            slug=slug if mode != "skip" else f"news-{current_time:%Y-%m-%d}",
            version=0,
            mode="weekly" if mode == "skip" else mode,
            title="",
            item_count=len(items),
            detail=(
                f"去重、合并与正文硬闸后入选条目不足 {_MIN_ITEMS_TO_BUILD} 条，"
                "按需求不强行生成低价值文章"
            ),
            files=(),
        )

    composed, excluded = _compose_items(gateway, items)
    if fetch_lead_images and composed:
        session = requests.Session()
        session.trust_env = False  # 与抓取网关一致的直连策略
        composed = [
            replace(item, image_url=_extract_lead_image(session, str(item.url or "")))
            if item.url
            else item
            for item in composed
        ]
    seen_dupe_ids = {row["article_id"] for row in excluded}
    for record in (*daily_dupes, *weekly_dupes, *daily_thin, *weekly_thin):
        if record["article_id"] in seen_dupe_ids:
            continue
        excluded.append(record)
        seen_dupe_ids.add(record["article_id"])
    if len(composed) < _MIN_ITEMS_TO_BUILD:
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
    if mode == "daily" and len(composed) < _MIN_ITEMS_FOR_DAILY:
        mode = "weekly"
        slug = weekly_slug

    insights = _compose_insights(gateway, composed)
    viewpoint = _compose_viewpoint(gateway, composed)
    title = _build_title(mode, composed)
    title_provenance = "mechanical"
    if mode == "daily" and len(composed) and len(_focus_from_title(composed[0].display_title or composed[0].title)) > _TITLE_SOFT_LIMIT:
        # Over-limit headline: prefer a model compression of the original
        # (no new facts allowed), mechanical truncation stays the fallback.
        rewritten = _rewrite_title_with_model(gateway, composed[0].title, _TITLE_SOFT_LIMIT)
        if rewritten:
            title = _build_title(mode, composed, focus=rewritten)
            title_provenance = "model_rewrite"
        else:
            title = _build_title(
                mode, composed,
                focus=_mechanical_truncate(_focus_from_title(composed[0].title), _TITLE_SOFT_LIMIT),
            )
            title_provenance = "mechanical_truncated"
    article = _ArticleDocument(
        mode=mode, title=title, items=composed, insights=insights,
        generated_at=current_time, viewpoint=viewpoint,
    )
    markdown = _render_markdown(article)
    html_text = _render_html(article)
    qc = _quality_checks(
        mode, composed, excluded, markdown,
        insights=insights, merged=merged_records,
    )

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
        insights=insights,
        viewpoint=viewpoint,
        qc=qc,
        llm_model=llm_model,
        title_provenance=title_provenance,
        logo_path=logo_path,
        generated_at=current_time,
        actor=actor,
    )


# ---------------------------------------------------------------------------
# Candidate loading


_TAG_RE = re.compile(r"<[^>]+>")
_PAREN_TITLE_RE = re.compile(r"^(.+?)[（(](.+)[）)]$")
_ENTITY_MARKERS = ("公司", "集团", "厂", "中心", "协会", "研究院", "事务所")


def _stripped_text(raw: str) -> str:
    """Snapshot text reduced to comparable characters: no tags, entities, whitespace."""

    return re.sub(r"\s+", "", html.unescape(_TAG_RE.sub(" ", str(raw or ""))))


def _filing_base(title: str) -> tuple[str, str] | None:
    """拆出「同一模板、括号内主体不同」的公告标题，返回 (模板, 主体)。

    非贪婪模板 + 贪婪主体：公司名内部的括号（普康（杭州）健康科技）归主体。
    """

    cleaned = re.sub(r"^(【[^】]*】|\[[^\]]*\])\s*", "", str(title or "").strip())
    match = _PAREN_TITLE_RE.match(cleaned)
    if not match:
        return None
    base = match.group(1).strip()
    inner = match.group(2).strip()
    if len(base) < 8 or len(inner) < 2:
        return None
    return base, inner


def _build_merged_item(members: list[dict[str, object]]) -> dict[str, object]:
    """把同模板成员组装成一条简讯候选；成员快照拼接为新的正文快照。"""

    first = members[0]
    base, _ = _filing_base(str(first.get("title") or "")) or ("", "")
    inners = [
        (_filing_base(str(member.get("title") or "")) or ("", ""))[1]
        for member in members
    ]
    entity_like = all(
        any(marker in inner for marker in _ENTITY_MARKERS) for inner in inners
    )
    count_label = f"{len(members)}家企业" if entity_like else f"{len(members)}条同类信息"
    snapshot_parts: list[str] = []
    for member in members:
        member_title = str(member.get("title") or "")
        snapshot = _stripped_text(str(member.get("content_snapshot") or ""))
        title_norm = re.sub(r"\s+", "", member_title)
        if snapshot and snapshot != title_norm:
            snapshot_parts.append(
                f"标题：{member_title}\n正文或摘要：{member.get('content_snapshot')}"
            )
        else:
            snapshot_parts.append(f"标题：{member_title}")
    rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    priority = min(
        (str(member.get("priority") or "P3") for member in members),
        key=lambda value: rank.get(value, 9),
    )
    return {
        "article_id": first["article_id"],
        "news_item_id": first.get("news_item_id", 0),
        "member_news_item_ids": tuple(
            int(member.get("news_item_id") or 0) for member in members
        ),
        "title": f"{base}（{count_label}）",
        "url": str(first.get("url") or "").strip() or None,
        "published_at": str(first.get("published_at") or "").strip() or None,
        "source_name": str(first.get("source_name") or ""),
        "section": str(first.get("section") or ""),
        "priority": priority,
        "content_snapshot": "\n".join(snapshot_parts),
    }


def _merge_routine_filings(
    items: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """同源同日、去括号后同模板的公告合并为一条简讯，返回 (新列表, 合并记录)。

    典型场景：同一天对 N 家企业的同款备案公示各占一条，读者只需一条
    「…备案信息公示（N 家企业）」。合并是确定性文本操作，公司名逐字
    来自原标题，不引入新事实。
    """

    groups: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    order: list[tuple[str, str, str]] = []
    merged: list[dict[str, object]] = []
    for item in items:
        parsed = _filing_base(str(item.get("title") or ""))
        if parsed is None:
            merged.append(item)
            continue
        base, _inner = parsed
        key = (
            str(item.get("source_name") or ""),
            str(item.get("published_at") or "")[:10],
            re.sub(r"\s+", "", base),
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    records: list[dict[str, object]] = []
    for key in order:
        members = sorted(groups[key], key=lambda member: str(member.get("title") or ""))
        if len(members) < 2:
            merged.extend(members)
            continue
        merged_item = _build_merged_item(members)
        merged.append(merged_item)
        records.append(
            {
                "title": str(merged_item["title"]),
                "members": [str(member.get("title") or "") for member in members],
            }
        )
    return merged, records


def _filter_thin_inputs(
    items: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """剔除正文快照撑不起摘要契约的条目，返回 (保留列表, 排除记录)。"""

    kept: list[dict[str, object]] = []
    excluded: list[dict[str, str]] = []
    for item in items:
        snapshot = _stripped_text(str(item.get("content_snapshot") or ""))
        title_norm = re.sub(r"\s+", "", str(item.get("title") or ""))
        if len(snapshot) < _SNAPSHOT_MIN_CHARS or snapshot == title_norm:
            excluded.append(
                {
                    "article_id": str(item["article_id"]),
                    "title": str(item["title"]),
                    "reason": (
                        f"thin_input：正文快照去标签后不足 {_SNAPSHOT_MIN_CHARS} 字"
                        "或仅有标题，无法支撑摘要契约"
                    ),
                }
            )
            continue
        kept.append(item)
    return kept, excluded


_LEAD_IMAGE_TIMEOUT_SECONDS = 12.0
# 通用装饰图通常带这些痕迹；带它们的一律不当作正文配图。
_LEAD_IMAGE_BLOCKLIST = ("logo", "icon", "sprite", ".svg", ".gif", "qrcode", "captcha")
nl = "\n"


def _normalize_image_candidate(src: str, base_url: str, *, require_extension: bool) -> str:
    value = html.unescape(str(src or "")).strip().split()[0] if str(src or "").strip() else ""
    if not value or value.lower().startswith(("data:", "javascript:")):
        return ""
    absolute = urljoin(base_url, value)
    if not absolute.startswith(("http://", "https://")):
        return ""
    lowered = absolute.lower()
    if any(token in lowered for token in _LEAD_IMAGE_BLOCKLIST):
        return ""
    if require_extension and not lowered.split("?", 1)[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
        return ""
    return absolute


def _extract_lead_image(session: requests.Session, url: str) -> str:
    """Fetch one original article page and extract its lead image URL.

    只取原文页面自身携带的图（og:image 优先，其次正文首图），不做任何
    生成或替换；失败一律返回空串，条目保持纯文字。单条有界超时。
    """

    if not url or not url.startswith(("http://", "https://")):
        return ""
    try:
        response = session.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=_LEAD_IMAGE_TIMEOUT_SECONDS
        )
        if response.status_code != 200:
            return ""
        page = response.text
    except Exception:
        return ""
    for pattern in (
        r"<meta[^>]+property=[\"']og:image[\"'][^>]*content=[\"']([^\"']+)[\"']",
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*property=[\"']og:image[\"']",
    ):
        match = re.search(pattern, page, re.IGNORECASE)
        if match:
            candidate = _normalize_image_candidate(match.group(1), url, require_extension=False)
            if candidate:
                return candidate
    for match in re.finditer(r"<img[^>]+src=[\"']([^\"']+)[\"']", page, re.IGNORECASE):
        candidate = _normalize_image_candidate(match.group(1), url, require_extension=True)
        if candidate:
            return candidate
    return ""


def _daily_slug_for(moment: datetime) -> str:
    return f"news-{moment:%Y-%m-%d}"


def _weekly_slug_for(moment: datetime) -> str:
    return f"news-weekly-{(moment - timedelta(days=6)):%Y%m%d}"


def _load_selected_items(
    database_path: Path,
    since: datetime,
    *,
    as_of: datetime,
) -> list[dict[str, object]]:
    """Load selected items in the window, minus items consumed by past issues.

    跨期排除：已被「as_of 当天零点之前」成文的文章消耗过的条目不再入选，
    因此每一期只写新料、多期链式不重复。当天创建的文章不排除——同日重建
    （修复后重出 v2+）必须看到与上一版相同的候选池，无论上一版用了哪个池
    （daily 直选或 weekly 补位）。
    """

    day_start_utc = as_of.replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    cutoff = day_start_utc.strftime("%Y-%m-%dT%H:%M:%S")
    initialize_schema(database_path)
    connection = connect_database(database_path, readonly=True)
    try:
        rows = connection.execute(
            """
            SELECT ni.id AS news_item_id, ni.article_id, ni.title, ni.url,
                   ni.published_at, ni.source_name, ni.section, ni.priority,
                   sa.content_snapshot, sa.fetched_at
            FROM news_items ni
            JOIN source_articles sa ON sa.id = ni.article_id
            WHERE ni.selected = 1 AND ni.published_at IS NOT NULL
              AND ni.published_at >= ?
              AND NOT EXISTS (
                  SELECT 1 FROM news_article_items nai
                  WHERE nai.news_item_id = ni.id AND nai.created_at < ?
              )
            ORDER BY CASE ni.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
                     WHEN 'P2' THEN 2 ELSE 3 END,
                 ni.published_at DESC, ni.id
            """,
            (since.strftime("%Y-%m-%d %H:%M:%S"), cutoff),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _cap_per_source(
    items: list[dict[str, object]], max_items: int
) -> list[dict[str, object]]:
    """同一信源上限 _MAX_ITEMS_PER_SOURCE 条，保证来源多样性。

    例外：截断后不足 _MIN_ITEMS_FOR_DAILY 时，允许同源条目按原排序
    补足到 3——单一活跃源的场景不为多样性饿死期次，但也不同源灌满
    整期（补足上限即停）。
    """

    per_source: dict[str, int] = {}
    kept: list[dict[str, object]] = []
    for item in items:
        key = str(item.get("source_name") or "")
        if per_source.get(key, 0) >= _MAX_ITEMS_PER_SOURCE:
            continue
        per_source[key] = per_source.get(key, 0) + 1
        kept.append(item)
        if len(kept) >= max_items:
            break
    if len(kept) >= _MIN_ITEMS_FOR_DAILY:
        return kept
    kept_ids = {id(item) for item in kept}
    for item in items:
        if len(kept) >= _MIN_ITEMS_FOR_DAILY:
            break
        if id(item) in kept_ids:
            continue
        kept.append(item)
        kept_ids.add(id(item))
    return kept


def _choose_item_pool(
    daily_items: list[dict[str, object]],
    weekly_items: list[dict[str, object]],
    max_items: int,
) -> tuple[str, list[dict[str, object]]]:
    """Pick daily vs weekly pool.

    当天有已入选条目（1–4 条）时，一律从 7 日池按 P0→P3 补位到单篇上限：
    读者看到的是尽量接近 3–5 条的日报，而不是当天恰好 3 条就停。
    """

    if daily_items:
        filled = _backfill_daily(daily_items, weekly_items, max_items)
        if len(filled) >= _MIN_ITEMS_FOR_DAILY:
            return "daily", _cap_per_source(filled, max_items)
    if len(weekly_items) >= _MIN_ITEMS_TO_BUILD:
        return "weekly", _cap_per_source(_sort_news_items(weekly_items), max_items)
    return "skip", weekly_items or daily_items


def _backfill_daily(
    daily_items: list[dict[str, object]],
    weekly_items: list[dict[str, object]],
    max_items: int,
) -> list[dict[str, object]]:
    filled = list(daily_items)
    seen_ids = {item["article_id"] for item in filled}
    titles = [normalize_news_title(str(item.get("title") or "")) for item in filled]
    url_keys: set[str] = set()
    for item in filled:
        url = str(item.get("url") or "").strip()
        if url:
            url_keys |= set(url_identity_keys(url))
    for item in weekly_items:
        if len(filled) >= max_items:
            break
        if item["article_id"] in seen_ids:
            continue
        title = str(item.get("title") or "")
        url = str(item.get("url") or "").strip() or None
        if is_same_news_event(title, url, prior_titles=titles, prior_url_keys=url_keys):
            continue
        filled.append(item)
        seen_ids.add(item["article_id"])
        titles.append(normalize_news_title(title))
        if url:
            url_keys |= set(url_identity_keys(url))
    return _sort_news_items(filled)[:max_items]


def _sort_news_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    by_date = sorted(
        items, key=lambda item: str(item.get("published_at") or ""), reverse=True
    )
    return sorted(
        by_date, key=lambda item: rank.get(str(item.get("priority") or ""), 9)
    )


def _dedupe_items(
    items: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    kept: list[dict[str, object]] = []
    dropped: list[dict[str, str]] = []
    titles: list[str] = []
    url_keys: set[str] = set()
    for item in items:
        title = str(item.get("title") or "")
        url = str(item.get("url") or "").strip() or None
        code = is_same_news_event(
            title, url, prior_titles=titles, prior_url_keys=url_keys
        )
        if code:
            dropped.append(
                {
                    "article_id": str(item["article_id"]),
                    "title": title,
                    "reason": "同事件近重复，已从文章剔除",
                }
            )
            continue
        kept.append(item)
        titles.append(normalize_news_title(title))
        if url:
            url_keys |= set(url_identity_keys(url))
    return kept, dropped


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
        summary, implication, keywords, raw_facts, headline = parsed
        if _contains_forbidden(summary) or _contains_forbidden(implication):
            continue
        if _contains_padding(summary) or _contains_padding(implication):
            continue
        # 反虚构硬校验：value 必须逐字出现在已抓取原文中，否则丢弃。
        original_text = str(item.get("content_snapshot") or "") + str(item["title"])
        verified_facts = tuple(
            fact
            for fact in raw_facts
            if fact["value"]
            and fact["value"] in original_text
            and len(fact["value"].strip()) >= 4
            and any(ch not in "0123456789.,%年月日" for ch in fact["value"].strip())
        )
        member_ids = tuple(
            dict.fromkeys(
                value
                for value in (
                    int(item.get("news_item_id") or 0),
                    *(int(member) for member in item.get("member_news_item_ids") or ()),
                )
                if value > 0
            )
        )
        return ComposedItem(
            article_id=int(item["article_id"]),
            source_name=str(item["source_name"]),
            section=str(item["section"]),
            priority=str(item["priority"]),
            title=str(item["title"]),
            display_title=headline or str(item["title"]),
            url=str(item["url"] or "").strip() or None,
            published_at=str(item["published_at"] or "").strip() or None,
            summary=summary,
            implication=implication,
            keywords=keywords,
            key_facts=verified_facts,
            news_item_id=int(item.get("news_item_id") or 0),
            news_item_ids=member_ids,
        )
    return None


def _parse_contract(
    content: str,
) -> tuple[str, str, tuple[str, ...], list[dict[str, str]], str] | None:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    summary = str(payload.get("summary") or "").strip()
    implication = str(payload.get("implication") or "").strip()
    headline = str(payload.get("headline") or "").strip()
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
    raw_facts = payload.get("key_facts")
    key_facts: list[dict[str, str]] = []
    if isinstance(raw_facts, list):
        for fact in raw_facts[:3]:
            if not isinstance(fact, dict):
                continue
            value = str(fact.get("value") or "").strip()
            note = str(fact.get("note") or "").strip()
            kind = str(fact.get("kind") or "fact").strip()
            if not value or len(value) > 60 or len(note) > 24:
                continue
            key_facts.append({"kind": kind[:20], "value": value, "note": note})
    if headline and (6 <= len(headline) <= 40):
        if not any(marker in headline for marker in _INTERNAL_MARKERS) and "【" not in headline:
            return summary, implication, keywords[:3], key_facts, headline
    return summary, implication, keywords[:3], key_facts, ""


def _contains_forbidden(text: str) -> bool:
    return any(term in text for term in _FORBIDDEN_TERMS)


# ---------------------------------------------------------------------------
# Assembly


@dataclass(frozen=True)
class _ArticleDocument:
    mode: str
    title: str
    items: list[ComposedItem]
    insights: tuple[str, ...]
    generated_at: datetime
    viewpoint: str = ""


def _contains_padding(text: str) -> bool:
    return any(marker in text for marker in _PADDING_MARKERS)


def _viewpoint_corpus(composed: list[ComposedItem]) -> str:
    parts: list[str] = [composed[0].title if composed else ""]
    for item in composed:
        parts.extend(
            [item.title, item.summary, item.implication, item.display_title]
        )
        parts.extend(fact.get("value", "") for fact in item.key_facts)
    return nl.join(parts)


def _verify_viewpoint(text: str, composed: list[ComposedItem]) -> bool:
    """观点红线：字数、违禁词、数字必须逐字来自本期条目。"""

    if not _VIEWPOINT_MIN_CHARS <= len(text) <= _VIEWPOINT_MAX_CHARS:
        return False
    if _contains_forbidden(text):
        return False
    if _contains_padding(text):
        return False
    corpus = _viewpoint_corpus(composed)
    return all(run in corpus for run in re.findall(r"\d+(?:\.\d+)?", text))


def _compose_viewpoint(gateway, composed: list[ComposedItem]) -> str:
    """One stance-taking editorial view over this issue's own items only."""

    if not composed:
        return ""
    lines = nl.join(
        f"- {item.title}：{item.summary}（含义：{item.implication}）"
        for item in composed
    )
    user_prompt = (
        f"本期标题与条目：{nl}{lines}{nl}请按系统约定输出 JSON。"
    )
    for _ in range(_COMPOSE_MAX_RETRIES + 1):
        try:
            result = gateway.analyze(_VIEWPOINT_SYSTEM_PROMPT, user_prompt, json_object=True)
        except Exception:
            return ""
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError:
            continue
        candidate = str(payload.get("viewpoint") or "").strip() if isinstance(payload, dict) else ""
        if candidate and _verify_viewpoint(candidate, composed):
            return candidate
    return ""


def _mechanical_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for i in range(len(cut), int(limit * 0.5), -1):
        if cut[i - 1] in "”》）」，、；：":
            return cut[:i]
    # No closing punctuation to break on: never slice inside a number, and
    # mark the hard truncation with an ellipsis.
    trimmed = re.sub(r"(?:\d+[.])?\d+$", "", cut).rstrip("，、；：,; ")
    return (trimmed + "…") if trimmed else cut + "…"


def _rewrite_title_with_model(
    gateway, original_title: str, limit: int
) -> str | None:
    """Ask the analysis backend to compress an over-limit headline.

    The model only sees the original headline and must keep every fact;
    malformed, over-limit, or red-line-violating answers are rejected here so
    the mechanical truncation remains the last resort.
    """

    user_prompt = (
        f"原标题（{len(original_title)}字）：{original_title}\n"
        f"请改写为不超过 {limit} 字的标题，输出 JSON。"
    )
    for _ in range(2):
        try:
            result = gateway.analyze(_TITLE_REWRITE_SYSTEM_PROMPT, user_prompt, json_object=True)
        except Exception:
            return None
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError:
            continue
        title = str(payload.get("title") or "").strip() if isinstance(payload, dict) else ""
        if not 8 <= len(title) <= limit:
            continue
        if any(marker in title for marker in _INTERNAL_MARKERS) or _contains_forbidden(title):
            continue
        return title
    return None


_INSIGHTS_SYSTEM_PROMPT = (
    "你是营养健康行业资讯编辑。基于本期已入选的资讯条目（标题与摘要如下），"
    "提炼 1-3 条跨条目观察：只能归纳已给内容中的共性、联动或时间线，"
    "不得引入任何外部信息或预测；每条不超过 60 字；不得使用夸张表述。"
    '输出 JSON 对象：{"insights": ["观察一", "观察二"]}。'
)

# 「EarlyData观点」：全文级编辑部立场（对标行业媒体的「XX观点」栏目）。
# 只基于本期条目做判断，不得引入外部信息；数字必须逐字来自条目原文。
_VIEWPOINT_SYSTEM_PROMPT = (
    "你是「EarlyData观点」栏目的主笔，为营养健康行业资讯写全文级编辑部观点。"
    "基于本期条目给出一个鲜明的行业判断：先下结论，再给一条行业含义或趋势判断，"
    "立场清晰、不用中性复述，不出现『我认为』。只允许依据给定条目，"
    "不得引入任何外部事实、事件、企业或数据；正文数字必须逐字来自给定条目；"
    "60-140 字；不得使用夸张或疾病治疗表述。"
    '输出 JSON 对象：{"viewpoint": "观点正文"}。'
)
_VIEWPOINT_MIN_CHARS = 60
_VIEWPOINT_MAX_CHARS = 140


def _compose_insights(gateway, composed: list[ComposedItem]) -> tuple[str, ...]:
    """One cross-item analysis call over this issue's own items only."""

    if len(composed) < 2:
        return ()
    lines = chr(10).join(
        f"- {item.title}：{item.summary}" for item in composed
    )
    user_prompt = f"本期条目：\n{lines}\n请按系统约定输出 JSON。"
    for _ in range(2):
        try:
            result = gateway.analyze(_INSIGHTS_SYSTEM_PROMPT, user_prompt, json_object=True)
        except Exception:
            return ()
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError:
            continue
        raw = payload.get("insights") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            continue
        insights = tuple(
            text.strip()
            for text in (str(entry) for entry in raw)
            if 15 <= len(text.strip()) <= 50
            and not _contains_forbidden(text)
            and not any(marker in text for marker in _INTERNAL_MARKERS)
        )
        return insights[:3]
    return ()


def _build_title(
    mode: str, composed: list[ComposedItem], *, focus: str | None = None
) -> str:
    if mode == "weekly":
        return "本周营养健康行业资讯｜政策、财报与展会动态一览"
    base = focus if focus else _focus_from_title(composed[0].display_title or composed[0].title)
    return f"营养健康行业资讯｜{base}"


def _focus_from_title(title: str, limit: int = 40) -> str:
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
    return _mechanical_truncate(cleaned, limit)


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
        *(
            [f"> **EarlyData观点**：{article.viewpoint}", ""] if article.viewpoint else []
        ),
    ]
    if article.insights:
        lines.append("## 本期速览")
        lines.append("")
        lines.extend(f"- {insight}" for insight in article.insights)
        lines.append("")
    for index, (section, members) in enumerate(_grouped_sections(article.items), start=1):
        lines.append(f"## {index:02d}｜{section_label(section)}")
        lines.append("")
        for item in members:
            lines.append(f"### {item.display_title or item.title}")
            lines.append("")
            if item.image_url:
                lines.append(f"![原文配图]({item.image_url})")
                lines.append("")
            lines.append(item.summary)
            lines.append("")
            if item.key_facts:
                lines.append(
                    "关键信息："
                    + "、".join(f"{fact['value']}（{fact['note']}）" for fact in item.key_facts)
                )
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
    lines.append(_info_note(article))
    return "\n".join(lines) + "\n"


def _info_note(article: _ArticleDocument) -> str:
    """文末信息说明：收录统计 + 监测日期 + 整理声明，自包含。"""

    return (
        f"信息说明：监测日期：{article.generated_at:%Y.%m.%d}。"
        "本文根据公开信息整理，具体内容以原始发布方为准。"
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
            image_html = (
                f'<p style="margin:0 0 10px;"><img src="{html.escape(item.image_url, quote=True)}" '
                f'style="max-width:100%;border-radius:6px;display:block;" alt="原文配图"/></p>'
                if item.image_url
                else ""
            )
            blocks.append(
                f'<section style="margin:0 0 26px;">'
                f'<p style="margin:0 0 8px;font-size:17px;font-weight:700;color:{_BRAND_COLORS["title"]};'
                f'line-height:1.6;">{esc(item.display_title or item.title)}</p>'
                + image_html
                + f'<p style="margin:0 0 10px;font-size:16px;color:{_BRAND_COLORS["body"]};line-height:1.9;">'
                f"{esc(item.summary)}</p>"
                + (
                    f'<p style="margin:0 0 10px;font-size:14px;color:{_BRAND_COLORS["body"]};'
                    f'line-height:1.8;">关键信息：'
                    + "、".join(
                        f"<strong>{esc(fact['value'])}</strong>（{esc(fact['note'])}）"
                        for fact in item.key_facts[:2]
                    )
                    + "</p>"
                    if item.key_facts
                    else ""
                )
                + f'<section style="margin:0 0 10px;padding:10px 14px;background:{_BRAND_COLORS["panel"]};'
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
    viewpoint_html = (
        f'<section style="margin:0 18px 22px;padding:12px 16px;'
        f'background:{_BRAND_COLORS["title"]};border-radius:6px;">'
        f'<p style="margin:0 0 4px;font-size:13px;color:{_BRAND_COLORS["green"]};'
        f'letter-spacing:0.12em;font-weight:700;">EARLYDATA 观点</p>'
        f'<p style="margin:0;font-size:16px;color:#FFFFFF;line-height:1.9;">'
        f"{esc(article.viewpoint)}</p></section>"
        if article.viewpoint
        else ""
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
        + viewpoint_html
        + (
            f'<section style="margin:0 18px 22px;padding:0 16px;">'
            f'<p style="margin:0 0 6px;font-size:15px;font-weight:700;'
            f'color:{_BRAND_COLORS["title"]};">本期速览</p>'
            + "".join(
                f'<p style="margin:0 0 6px;font-size:14px;color:{_BRAND_COLORS["body"]};'
                f'line-height:1.8;">· {esc(insight)}</p>'
                for insight in article.insights
            )
            + "</section>"
            if article.insights else ""
        )
        + f'<section style="padding:0 18px;">{"".join(sections_html)}</section>'
        f'<section style="padding:4px 18px 8px;">'
        f'<p style="margin:0 0 8px;font-size:15px;font-weight:700;color:{_BRAND_COLORS["title"]};">'
        f"本期关键词</p><p style=\"margin:0;line-height:2;\">{keywords}</p></section>"
        f'<section style="padding:6px 18px 30px;">'
        f'<p style="margin:0;font-size:12px;color:{_BRAND_COLORS["muted"]};line-height:1.7;">'
        f"{esc(_info_note(article))}</p></section>"
        f"</section>"
    )


# ---------------------------------------------------------------------------
# QC + versioned export


def _quality_checks(
    mode: str,
    composed: list[ComposedItem],
    excluded: list[dict[str, str]],
    markdown: str,
    insights: tuple[str, ...] = (),
    merged: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    merged = merged or []
    merged_member_count = sum(len(record["members"]) for record in merged)  # type: ignore[arg-type]
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
        {"check": "item_count",
         "passed": (
             3 <= len(composed) <= 5 if mode == "daily" else 2 <= len(composed) <= 5
         ),
         "detail": f"{len(composed)} 条（日报 3–5 条，周报至少 2 条）"},
        {"check": "total_length", "passed": 1000 <= text_length <= 1800 or mode == "weekly",
         "detail": f"正文约 {text_length} 字（需求建议 1000–1800 字）"},
        {"check": "no_forbidden_terms", "passed": not found_forbidden,
         "detail": "、".join(found_forbidden) if found_forbidden else "未发现夸张词或疾病治疗表述"},
        {"check": "sources_complete", "passed": all(item["fields_complete"] for item in item_checks),
         "detail": "每条均有机构、日期与链接"},
        {"check": "key_facts_verified", "passed": True,
         "detail": f"关键信息 {sum(len(item.key_facts) for item in composed)} 条（已在抽取时经原文子串校验）"},
        {"check": "insights_count", "passed": True,
         "detail": f"本期速览 {len(insights)} 条（仅基于本期条目）"},
        {"check": "routine_merge",
         "passed": True,
         "detail": (
             f"同模板公告合并 {len(merged)} 组"
             f"（覆盖 {merged_member_count} 条原条目）"
         )},
    ]
    return {
        "item_checks": item_checks,
        "article_checks": article_checks,
        "excluded_items": excluded,
        "merged_items": merged,
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
    insights: tuple[str, ...],
    viewpoint: str,
    qc: dict[str, object],
    llm_model: str,
    title_provenance: str,
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
                "title_provenance": title_provenance,
                "insights": list(insights),
                "viewpoint": viewpoint,
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
            article_row_id = int(
                connection.execute(
                    "SELECT id FROM news_articles WHERE slug = ? AND version = ?", (slug, version)
                ).fetchone()["id"]
            )
            # created_at 显式取构建时刻（而非 DB 时钟）：跨期排除按它与
            # 构建日零点比较，测试的虚构时间线也必须成立。
            consumed_at = generated_at.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
            for item in composed:
                # 合并条目按全部成员记账：任何一个成员被本期消耗后，
                # 跨期排除都要认账，防止同款公告下期拆散重出。
                member_ids = item.news_item_ids or (
                    (item.news_item_id,) if item.news_item_id > 0 else ()
                )
                for member_id in member_ids:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO news_article_items(article_id, news_item_id, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (article_row_id, member_id, consumed_at),
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

    # 摘要按 news_items.id 对号（source-table 行主键是 ni.id，不是 article_id）：
    # 合并条目把同一条简讯摘要写到它的每个成员行上。
    summaries: dict[int, str] = {}
    for item in composed:
        member_ids = item.news_item_ids or (
            (item.news_item_id,) if item.news_item_id > 0 else ()
        )
        for member_id in member_ids:
            summaries[member_id] = item.summary
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
