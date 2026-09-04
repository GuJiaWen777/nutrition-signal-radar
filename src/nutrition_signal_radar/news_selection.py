"""Deterministic selection rules for the news line (Phase 4 P4-03).

Every candidate article ingested from an enabled news-whitelist source is
evaluated against the requirement document's five admission conditions and
classified into an article section plus a P0–P3 priority.  The rules are pure
keyword/structure logic with no LLM and no network: every accept/reject
decision is recorded with machine-readable reasons so the daily source table
stays auditable (ADR-0014).  Keyword tables are intentionally simple and can
be tuned by operators over time.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .article_contract import canonicalize_article_url
from .database import connect_database, transaction
from .schema import initialize_schema


_SECTION_LABELS = {
    "policy": "政策监管",
    "brand_finance": "品牌与财报",
    "expo": "展会与产业活动",
    "research": "科研与原料",
}
_REJECTION_CODES = {
    "missing_title",
    "missing_date",
    "missing_url",
    "missing_body",
    "not_relevant",
    "duplicate_url",
    "duplicate_title",
    "no_action_info",
}
_TITLE_NEAR_DUP_THRESHOLD = 0.92
_MIN_TITLE_LENGTH = 6
# Last path segment like /notice/182630200.html is treated as the same CMS
# article even when the section prefix differs.
_NUMERIC_PATH_ID = re.compile(r"^(\d{6,})(?:\.html?)?$", re.IGNORECASE)

INDUSTRY_TERMS: tuple[str, ...] = (
    "保健食品", "特殊食品", "特医食品", "特殊医学用途", "特殊膳食", "特膳",
    "食品安全",
    "膳食补充剂", "功能食品", "健康食品", "营养品", "营养健康", "营养",
    "益生菌", "维生素", "矿物质", "蛋白粉", "鱼油", "胶原蛋白", "膳食纤维",
    "新食品原料", "食品原料", "婴幼儿配方", "乳粉", "奶粉", "DHA", "低GI",
    "甜味剂", "功能声称", "保健",
)
# 展会信源的放宽词：主办方发的参展/活动内容天然属于产业资讯，
# 不再要求标题同时命中营养词表（避免「XX企业亮相XX展」被误杀）。
_EXPO_TERMS: tuple[str, ...] = (
    "展会", "博览", "参展", "展商", "亮相", "峰会", "论坛", "巡回",
)
ACTION_TERMS: tuple[str, ...] = (
    "发布", "印发", "征求意见", "通报", "处罚", "罚", "吊销", "责令", "立案",
    "整治", "批准", "获批", "注册", "备案", "许可", "披露", "财报", "业绩",
    "融资", "并购", "收购", "投资", "合作", "签署", "战略", "举办", "召开",
    "开幕", "开展", "闭幕", "启动", "成立", "下架", "召回", "抽检", "通告",
    "公告", "出台", "实施", "施行", "修订", "换届", "更名", "重组", "上市",
)
_SECTION_TERMS: dict[str, tuple[str, ...]] = {
    "policy": (
        "政策", "监管", "公告", "通报", "处罚", "征求意见", "标准", "法规",
        "备案", "注册", "许可", "抽检", "立案", "整治", "目录", "通告", "执法",
        "合规", "督查", "约谈",
    ),
    "brand_finance": (
        "财报", "业绩", "半年报", "年报", "季报", "预增", "预减", "融资",
        "并购", "收购", "投资", "合作", "战略", "新品", "工厂", "产能", "渠道",
        "品牌", "签约", "重组", "上市",
    ),
    "expo": (
        "展会", "博览会", "论坛", "峰会", "大会", "会议", "参展", "开展",
        "开幕", "闭幕", "展位", "巡展", "报名",
    ),
    "research": (
        "研究", "科研", "论文", "期刊", "白皮书", "团体标准", "共识", "临床",
        "试验", "指南", "学术",
    ),
}
_FINANCE_TERMS = (
    "财报", "业绩", "半年报", "年报", "季报", "预增", "预减", "融资", "并购",
    "收购", "重组", "投资",
)
_EXPO_PREVIEW_TERMS = ("预告", "报名", "即将", "将办", "筹备")
_SOURCE_CATEGORY_SECTION = {
    "policy": "policy",
    "disclosure": "brand_finance",
    "brand": "brand_finance",
    "expo": "expo",
    "research": "research",
}


class NewsSelectionError(ValueError):
    """Raised when the selection run is misconfigured."""


@dataclass(frozen=True)
class NewsItemDecision:
    """One evaluated candidate with its section, priority and verdict."""

    article_id: int
    news_source_id: int
    source_name: str
    source_category: str
    title: str
    url: str | None
    published_at: str | None
    section: str
    priority: str
    selected: bool
    rejection_reasons: tuple[dict[str, str], ...]
    priority_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "article_id": self.article_id,
            "news_source_id": self.news_source_id,
            "source_name": self.source_name,
            "source_category": self.source_category,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "section": self.section,
            "section_label": _SECTION_LABELS.get(self.section, self.section),
            "priority": self.priority,
            "selected": self.selected,
            "rejection_reasons": list(self.rejection_reasons),
            "priority_reasons": list(self.priority_reasons),
        }


@dataclass(frozen=True)
class NewsSelectionReport:
    database_path: Path
    evaluated: int
    selected: int
    rejected: int
    rejection_counts: dict[str, int]
    selected_by_section: dict[str, int]
    selected_by_priority: dict[str, int]

    @property
    def daily_eligible(self) -> bool:
        """A daily article needs at least three selected items (docx: 3–5)."""

        return self.selected >= 3

    def as_dict(self) -> dict[str, object]:
        return {
            "database_path": str(self.database_path),
            "evaluated": self.evaluated,
            "selected": self.selected,
            "rejected": self.rejected,
            "rejection_counts": self.rejection_counts,
            "selected_by_section": self.selected_by_section,
            "selected_by_priority": self.selected_by_priority,
            "daily_eligible": self.daily_eligible,
        }


def section_label(section: str) -> str:
    return _SECTION_LABELS.get(section, section)


def evaluate_pending_news_articles(database_path: Path) -> NewsSelectionReport:
    """Score every not-yet-evaluated news-source article and record decisions."""

    initialize_schema(database_path)
    connection = connect_database(database_path)
    decisions: list[NewsItemDecision] = []
    try:
        with transaction(connection):
            # 正文补全后的重开：因 missing_body 被拒、但 source_articles 的
            # 快照如今已是真实正文的条目，删除旧评估记录重新参评。这是数据
            # 修正而非决策反转——当时判的是「无正文」，前提已变。
            connection.execute(
                """
                DELETE FROM news_items
                WHERE id IN (
                    SELECT ni.id FROM news_items ni
                    JOIN source_articles sa ON sa.id = ni.article_id
                    WHERE ni.selected = 0
                      AND ni.rejection_reasons LIKE '%missing_body%'
                      AND length(replace(sa.content_snapshot, ' ', '')) >= 60
                      AND replace(sa.content_snapshot, ' ', '')
                          != replace(ni.title, ' ', '')
                )
                """
            )
            prior = _load_recent_selected_index(connection)
            for row in _pending_rows(connection):
                decision = _decide(row, prior)
                connection.execute(
                    f"""
                    INSERT INTO news_items(
                        article_id, news_source_id, title, url, published_at,
                        source_name, source_category, section, priority,
                        selected, decision, rejection_reasons, priority_reasons
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision.article_id,
                        decision.news_source_id,
                        decision.title,
                        decision.url,
                        decision.published_at,
                        decision.source_name,
                        decision.source_category,
                        decision.section,
                        decision.priority,
                        int(decision.selected),
                        "selected" if decision.selected else "rejected",
                        json.dumps(
                            list(decision.rejection_reasons), ensure_ascii=False
                        ),
                        json.dumps(
                            list(decision.priority_reasons), ensure_ascii=False
                        ),
                    ),
                )
                if decision.selected:
                    prior["titles"].append(_normalize_title(decision.title))
                    if decision.url:
                        prior["url_keys"] |= set(url_identity_keys(decision.url))
                decisions.append(decision)
    finally:
        connection.close()

    rejection_counts: dict[str, int] = {}
    selected_by_section: dict[str, int] = {}
    selected_by_priority: dict[str, int] = {}
    for decision in decisions:
        if decision.selected:
            selected_by_section[decision.section] = selected_by_section.get(decision.section, 0) + 1
            selected_by_priority[decision.priority] = selected_by_priority.get(decision.priority, 0) + 1
        else:
            for reason in decision.rejection_reasons:
                code = reason["code"]
                rejection_counts[code] = rejection_counts.get(code, 0) + 1
    return NewsSelectionReport(
        database_path=Path(database_path).resolve(),
        evaluated=len(decisions),
        selected=sum(1 for decision in decisions if decision.selected),
        rejected=sum(1 for decision in decisions if not decision.selected),
        rejection_counts=rejection_counts,
        selected_by_section=selected_by_section,
        selected_by_priority=selected_by_priority,
    )


def list_news_items(database_path: Path, *, selected_only: bool = False) -> list[NewsItemDecision]:
    """Return recorded decisions, newest first; the daily source-table view."""

    initialize_schema(database_path)
    connection = connect_database(database_path, readonly=True)
    try:
        query = "SELECT * FROM news_items"
        if selected_only:
            query += " WHERE selected = 1"
        query += " ORDER BY id DESC"
        return [_news_item(row) for row in connection.execute(query)]
    finally:
        connection.close()


def _pending_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT sa.id AS article_id, sa.title, sa.url, sa.published_at,
               sa.content_snapshot,
               ns.id AS news_source_id, ns.name AS source_name,
               ns.category AS source_category
        FROM source_articles sa
        JOIN source_accounts acc ON acc.id = sa.account_id
        JOIN news_sources ns ON ns.name = acc.name
        LEFT JOIN news_items ni ON ni.article_id = sa.id
        WHERE ni.id IS NULL
        ORDER BY sa.id
        """
    ).fetchall()


def _decide(row: sqlite3.Row, prior: dict[str, object]) -> NewsItemDecision:
    title = str(row["title"] or "").strip()
    url = str(row["url"] or "").strip() or None
    published_at = str(row["published_at"] or "").strip() or None
    body = str(row["content_snapshot"] or "").strip()
    haystack = f"{title} {body}"

    reasons: list[dict[str, str]] = []
    if len(title) < _MIN_TITLE_LENGTH:
        reasons.append({"code": "missing_title", "detail": "标题缺失或过短，无法作为可追溯条目"})
    if not published_at:
        reasons.append({"code": "missing_date", "detail": "缺少发布日期，无法核验时效"})
    if not url:
        reasons.append({"code": "missing_url", "detail": "缺少原文链接"})
    body_norm = re.sub(r"\s+", "", body)
    title_norm = re.sub(r"\s+", "", title)
    # 标题快照（正文=标题，或比标题长不了几个字）等于没有正文：
    # 这类条目进成文只会产出复述标题的填充话术，直接拦下。
    if (
        len(body) < _MIN_TITLE_LENGTH
        or body_norm == title_norm
        or len(body_norm) < 15
    ):
        reasons.append({"code": "missing_body", "detail": "正文仅有标题或无实质内容"})
    source_category = str(row["source_category"])
    relevant = _contains_any(haystack, INDUSTRY_TERMS) or (
        source_category in ("policy", "disclosure") and "食品" in haystack
    ) or (
        source_category == "expo" and _contains_any(haystack, _EXPO_TERMS)
    )
    if not relevant:
        reasons.append({"code": "not_relevant", "detail": "未命中营养健康产业相关词，与产业不直接相关"})
    duplicate_code = is_same_news_event(
        title,
        url,
        prior_titles=prior["titles"],  # type: ignore[arg-type]
        prior_url_keys=prior["url_keys"],  # type: ignore[arg-type]
    )
    if duplicate_code == "duplicate_url":
        reasons.append({"code": "duplicate_url", "detail": "同一原文链接或同一站点文章编号已入选"})
    elif duplicate_code == "duplicate_title":
        reasons.append({"code": "duplicate_title", "detail": "标题与近期已入选条目近似重复"})
    # 展会源的参展/活动预告没有政策或公司「动作」，但本身就是产业动态：
    # 该类目跳过行动信息门（相关性已由 _EXPO_TERMS 把关）。
    if source_category != "expo" and not _contains_any(haystack, ACTION_TERMS):
        reasons.append({"code": "no_action_info", "detail": "没有明确的政策动作、公司动作或会议信息"})

    section, section_reason = _classify_section(str(row["source_category"]), haystack)
    priority, priority_reasons = _assess_priority(section, haystack)

    return NewsItemDecision(
        article_id=int(row["article_id"]),
        news_source_id=int(row["news_source_id"]),
        source_name=str(row["source_name"]),
        source_category=str(row["source_category"]),
        title=title,
        url=url,
        published_at=published_at,
        section=section,
        priority=priority,
        selected=not reasons,
        rejection_reasons=tuple(reasons),
        priority_reasons=(section_reason, *priority_reasons),
    )


def _classify_section(source_category: str, haystack: str) -> tuple[str, str]:
    """Prefer keyword evidence over the source-category default."""

    # 公告/披露 are generic disclosure words: on disclosure and brand sources
    # they alone must not pull company announcements into the policy section.
    generic = {"公告", "披露"} if source_category in ("disclosure", "brand") else set()
    best_section, best_hits = None, 0
    for section, terms in _SECTION_TERMS.items():
        effective = [term for term in terms if term not in generic]
        hits = sum(1 for term in effective if term in haystack)
        if hits > best_hits:
            best_section, best_hits = section, hits
    if best_section is not None:
        return best_section, f"关键词命中栏目规则（{_SECTION_LABELS[best_section]}）"
    default = _SOURCE_CATEGORY_SECTION.get(source_category, "research")
    return default, f"无栏目关键词命中，按信源类别默认归入（{_SECTION_LABELS[default]}）"


def _assess_priority(section: str, haystack: str) -> tuple[str, list[str]]:
    """Apply the requirement document's P0–P3 content priority table."""

    if section == "policy":
        return "P0", ["政策监管类内容按需求定为 P0"]
    if section == "brand_finance":
        if _contains_any(haystack, _FINANCE_TERMS):
            return "P1", ["财报、融资并购等披露类动作按需求定为 P1"]
        return "P2", ["常规品牌动作按需求定为 P2"]
    if section == "expo":
        if _contains_any(haystack, _EXPO_PREVIEW_TERMS):
            return "P3", ["一般会议/展会预告按需求定为 P3"]
        return "P2", ["重点展会与产业论坛按需求定为 P2"]
    return "P2", ["科研与原料动态按需求定为 P2"]


def _load_recent_selected_index(connection: sqlite3.Connection) -> dict[str, object]:
    rows = connection.execute(
        "SELECT title, url FROM news_items WHERE selected = 1 ORDER BY id DESC LIMIT 500"
    ).fetchall()
    url_keys: set[str] = set()
    for row in rows:
        if row["url"]:
            url_keys |= set(url_identity_keys(str(row["url"])))
    return {
        "titles": [_normalize_title(str(row["title"])) for row in rows],
        "url_keys": url_keys,
    }


def _contains_any(haystack: str, terms: tuple[str, ...]) -> bool:
    lowered = haystack.lower()
    return any(term.lower() in lowered for term in terms)


def url_identity_keys(url: str) -> frozenset[str]:
    """Stable identity keys for one news URL.

    Includes the canonical URL and, when the last path segment is a 6+ digit
    CMS id (``/notice/182630200.html``), a host+id key so the same article
    listed under a different section path is still treated as one event.
    """

    canonical = canonicalize_article_url(url) or url.strip()
    keys = {_canonical_url_key(url)}
    parsed = urlsplit(canonical)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    last = parsed.path.rstrip("/").rsplit("/", 1)[-1] if parsed.path else ""
    match = _NUMERIC_PATH_ID.match(last)
    if host and match:
        keys.add(f"{host}#id:{match.group(1)}")
    return frozenset(keys)


def is_same_news_event(
    title: str,
    url: str | None,
    *,
    prior_titles: list[str],
    prior_url_keys: set[str],
) -> str | None:
    """Return ``duplicate_url`` / ``duplicate_title`` when this item collides."""

    if url:
        if url_identity_keys(url) & prior_url_keys:
            return "duplicate_url"
    if title and _is_near_duplicate(title, prior_titles):
        return "duplicate_title"
    return None


def normalize_news_title(title: str) -> str:
    return _normalize_title(title)


def _canonical_url_key(url: str) -> str:
    canonical = canonicalize_article_url(url)
    return (canonical or url).rstrip("/")


def _normalize_title(title: str) -> str:
    cleaned = unicodedata.normalize("NFKC", str(title or "")).strip()
    # 【2026-08-31】-style editorial prefixes and leading dates are repost
    # markers, not part of the headline for similarity purposes.
    cleaned = re.sub(r"^(【[^】]*】|\[[^\]]*\])\s*", "", cleaned)
    cleaned = re.sub(r"^\d{4}[-./年]\d{1,2}([-./月]\d{1,2}日?)?\s*", "", cleaned)
    return re.sub(r"[\s\W_]+", "", cleaned).lower()


def _is_near_duplicate(title: str, prior_titles: list[str]) -> bool:
    candidate = _normalize_title(title)
    if not candidate:
        return False
    for prior in prior_titles:
        if not prior:
            continue
        if candidate == prior:
            return True
        if difflib.SequenceMatcher(None, candidate, prior).ratio() >= _TITLE_NEAR_DUP_THRESHOLD:
            return True
    return False


def _news_item(row: sqlite3.Row) -> NewsItemDecision:
    rejection_raw = json.loads(str(row["rejection_reasons"] or "[]"))
    priority_raw = json.loads(str(row["priority_reasons"] or "[]"))
    return NewsItemDecision(
        article_id=int(row["article_id"]),
        news_source_id=int(row["news_source_id"]),
        source_name=str(row["source_name"]),
        source_category=str(row["source_category"]),
        title=str(row["title"]),
        url=str(row["url"] or "").strip() or None,
        published_at=str(row["published_at"] or "").strip() or None,
        section=str(row["section"]),
        priority=str(row["priority"]),
        selected=bool(row["selected"]),
        rejection_reasons=tuple(dict(item) for item in rejection_raw if isinstance(item, dict)),
        priority_reasons=tuple(str(item) for item in priority_raw),
    )


__all__ = [
    "INDUSTRY_TERMS",
    "NewsItemDecision",
    "NewsSelectionError",
    "NewsSelectionReport",
    "evaluate_pending_news_articles",
    "is_same_news_event",
    "list_news_items",
    "normalize_news_title",
    "section_label",
    "url_identity_keys",
]
