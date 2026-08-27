from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


@dataclass(frozen=True)
class NormalizedArticle:
    """Source-neutral article record accepted by the radar ingestion boundary."""

    external_id: str
    account_name: str
    title: str
    published_at: str | None
    url: str | None
    content_snapshot: str
    content_hash: str | None
    read_count: int = 0
    like_count: int = 0
    share_count: int = 0
    recommend_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    source_reference: str | None = None


@dataclass(frozen=True)
class RowIssue:
    row_number: int
    reason: str


@dataclass(frozen=True)
class ArticleBatch:
    """A normalized batch that may come from Excel today or RedFox later."""

    source_kind: str
    source_path: str
    source_fingerprint: str
    source_rows: int
    articles: tuple[NormalizedArticle, ...]
    skipped_rows: int = 0
    collapsed_rows: int = 0
    issues: tuple[RowIssue, ...] = ()
    account_segment: str = "industry_media"


class ArticleSource(Protocol):
    def load(self) -> ArticleBatch: ...


def canonicalize_article_url(raw_url: str | None) -> str | None:
    """Return a stable URL, stripping WeChat tracking parameters when possible."""
    if raw_url is None or not str(raw_url).strip():
        return None

    value = str(raw_url).strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value

    host = (parsed.hostname or "").lower()
    if host != "mp.weixin.qq.com":
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))

    path = parsed.path.rstrip("/") or "/"
    if path.startswith("/s/"):
        # The current WeRSS fallback parser accepts Sogou-resolved articles by
        # inserting a stable local ``sg_`` path segment.  For those URLs the
        # signed query, rather than the synthetic path, is what lets WeChat
        # reopen the original article.  Keep it for source traceability while
        # continuing to strip tracking parameters from normal path-style links.
        if path.startswith("/s/sg_") and parsed.query:
            return urlunsplit(("https", "mp.weixin.qq.com", path, parsed.query, ""))
        return urlunsplit(("https", "mp.weixin.qq.com", path, "", ""))

    query = parse_qs(parsed.query, keep_blank_values=True)
    identity_keys = ("__biz", "mid", "idx", "sn")
    if path == "/s" and all(query.get(key, [""])[0] for key in identity_keys):
        identity_query = [(key, query[key][0]) for key in identity_keys]
        return urlunsplit(("https", "mp.weixin.qq.com", "/s", urlencode(identity_query), ""))

    return urlunsplit(("https", "mp.weixin.qq.com", parsed.path, parsed.query, ""))


def external_id_for_article(
    *,
    url: str | None,
    account_name: str,
    title: str,
    published_at: str | None,
    content_hash: str | None,
) -> str:
    """Build a deterministic fallback identity shared by all source adapters."""
    canonical_url = canonicalize_article_url(url)
    if canonical_url:
        material = canonical_url
        namespace = "url"
    else:
        material = "\x1f".join(
            (account_name.strip(), title.strip(), published_at or "", content_hash or "")
        )
        namespace = "content"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{namespace}:{digest}"


def normalize_source_type(value: object) -> str:
    """Map an agent-reported source label onto the research-source vocabulary.

    Labels arrive free-form from the pi agent (“权威媒体+行业媒体转载”, “监管原文”,
    “company_announcement”).  Strip parenthetical qualifiers and separators first
    so compound labels resolve to their leading canonical type instead of “other”.
    """

    cleaned = re.sub(r"[（(].*?[）)]", "", str(value or "").strip())
    normalized = re.split(r"[+、/|，,]", cleaned)[0].strip().lower()
    mapping = {
        "regulator": {
            "regulator",
            "regulatory",
            "监管",
            "监管原文",
            "官方公告",
            "政府",
            "government",
        },
        "research_paper": {"research_paper", "paper", "journal", "论文", "学术", "期刊"},
        "company_announcement": {"company_announcement", "announcement", "企业公告", "公司公告", "信披"},
        "market_data": {"market_data", "report", "市场数据", "数据报告", "研报"},
        "media": {"media", "news", "媒体", "新闻", "权威媒体"},
        "wechat_discovery": {"wechat_discovery", "wechat", "公众号"},
    }
    for canonical, aliases in mapping.items():
        if normalized in aliases:
            return canonical
    return "other"
