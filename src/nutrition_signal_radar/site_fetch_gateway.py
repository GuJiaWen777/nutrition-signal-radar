"""Direct-fetch gateway for news-line whitelist sources (Phase 4, ADR-0014).

The gateway has no database writes and no scheduling behaviour.  For one
enabled whitelist source it fetches the configured entry URL (list page, JSON
API, or RSS feed), archives a desensitized raw snapshot under ``raw/sites/``,
and returns in-window items as plain data.  Callers inject an HTTP session in
tests; real calls are made only through the explicit ``news fetch --live``
command.  Parsers are deliberately conservative: items without a parseable
publish date are reported as skipped because the news line requires a
verifiable publish date for every selected item.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Mapping, Protocol
from urllib.parse import urljoin, urlsplit

from .news_registry import NewsSource


FETCH_TIMEOUT_SECONDS = 30.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 EarlyDataNewsRadar/0.1"
)
_CST = timezone(timedelta(hours=8), name="Asia/Shanghai")
_DATE_PATTERN = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{4}年\d{1,2}月\d{1,2}日"
    r"|\d{4}\.\d{1,2}\.\d{1,2}"
    r"|\d{4}/\d{1,2}/\d{1,2}"
    r"|\d{1,2}-\d{1,2}"
)
_MIN_TITLE_LENGTH = 6
_DATE_CONTEXT_CHARS = 120
_FORBIDDEN_HREF_PREFIXES = ("javascript:", "mailto:", "tel:", "#")


class SiteFetchError(RuntimeError):
    """A safe, normalized failure from the news-site HTTP boundary."""


class _HttpBody(Protocol):
    status_code: int

    @property
    def text(self) -> str: ...

    @property
    def content(self) -> bytes: ...

    def json(self) -> object: ...


class HttpSession(Protocol):
    def get(self, url: str, *, timeout: float, headers: Mapping[str, str]) -> _HttpBody: ...

    def post(
        self,
        url: str,
        *,
        data: Mapping[str, object],
        timeout: float,
        headers: Mapping[str, str],
    ) -> _HttpBody: ...


@dataclass(frozen=True)
class FetchedItem:
    """One candidate news entry parsed from a whitelist source entry page."""

    title: str
    url: str
    published_at: str | None
    summary: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SiteFetchBatch:
    """Parsed result of one fetch, plus its archived raw snapshot paths."""

    source_name: str
    domain: str
    fetch_kind: str
    items: tuple[FetchedItem, ...]
    skipped_no_date: int
    skipped_out_of_window: int
    raw_body_path: Path | None
    raw_meta_path: Path | None
    raw_body_sha256: str | None


def fetch_site_source(
    *,
    session: HttpSession,
    source: NewsSource,
    lookback_hours: int,
    max_items: int,
    raw_dir: Path,
    now: datetime | None = None,
) -> SiteFetchBatch:
    """Fetch one whitelist source and return in-window items with a raw snapshot."""

    if lookback_hours < 1:
        raise SiteFetchError("news lookback_hours must be at least 1")
    if max_items < 1:
        raise SiteFetchError("news max_items must be at least 1")
    current_time = _ensure_cst(now) if now is not None else datetime.now(_CST)
    cutoff = current_time - timedelta(hours=lookback_hours)

    if source.fetch_kind == "rss":
        raw_items, snapshot = _fetch_rss(session, source, raw_dir, current_time)
    elif source.fetch_kind == "html_list":
        raw_items, snapshot = _fetch_html_list(session, source, raw_dir, current_time, max_items)
    elif source.fetch_kind == "api":
        raw_items, snapshot = _fetch_api(session, source, raw_dir, current_time, cutoff, max_items)
    else:
        raise SiteFetchError(f"未知的抓取方式：{source.fetch_kind}")

    kept: list[FetchedItem] = []
    skipped_no_date = 0
    skipped_out_of_window = 0
    seen_urls: set[str] = set()
    for item in raw_items:
        if item.url in seen_urls:
            continue
        seen_urls.add(item.url)
        published = _to_cst_datetime(item.published_at)
        if published is None:
            skipped_no_date += 1
            continue
        if published < cutoff:
            skipped_out_of_window += 1
            continue
        kept.append(item)
        if len(kept) >= max_items:
            break

    return SiteFetchBatch(
        source_name=source.name,
        domain=source.domain,
        fetch_kind=source.fetch_kind,
        items=tuple(kept),
        skipped_no_date=skipped_no_date,
        skipped_out_of_window=skipped_out_of_window,
        raw_body_path=snapshot[0],
        raw_meta_path=snapshot[1],
        raw_body_sha256=snapshot[2],
    )


# ---------------------------------------------------------------------------
# Transport helpers


def _http_headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT}


def _checked_get(session: HttpSession, url: str) -> tuple[str, bytes, int]:
    try:
        response = session.get(url, timeout=FETCH_TIMEOUT_SECONDS, headers=_http_headers())
    except Exception as exc:
        raise SiteFetchError(f"抓取请求失败：{url}（{exc}）") from exc
    if response.status_code >= 400:
        raise SiteFetchError(f"抓取返回 HTTP {response.status_code}：{url}")
    return _decode_text(response), response.content, response.status_code


def _decode_text(response: object) -> str:
    """Decode with declared charset; many Chinese gov sites serve GBK without
    a charset header, in which case fall back to apparent encoding."""

    text = response.text  # type: ignore[attr-defined]
    headers = getattr(response, "headers", None)
    content_type = str(headers.get("content-type", "")) if headers is not None else ""
    if "charset" not in content_type.lower():
        apparent = getattr(response, "apparent_encoding", None)
        if apparent:
            try:
                text = response.content.decode(apparent, errors="replace")  # type: ignore[attr-defined]
            except (LookupError, ValueError):
                pass
    return text


def _slugify_domain(domain: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", domain.strip().lower()).strip("-") or "source"


def _write_snapshot(
    raw_dir: Path,
    source: NewsSource,
    now: datetime,
    body: bytes,
    *,
    request_url: str,
    method: str,
    status_code: int,
) -> tuple[Path, Path, str]:
    day_dir = raw_dir / now.strftime("%Y-%m-%d") / _slugify_domain(source.domain)
    day_dir.mkdir(parents=True, exist_ok=True)
    if body[:1] in (b"{", b"[") or request_url.endswith(".json"):
        suffix = ".json"
    elif body[:5] == b"<rss " or body[:5] == b"<feed" or body[:2] == b"<?":
        suffix = ".xml"
    else:
        suffix = ".html"
    body_path = day_dir / f"response{suffix}"
    body_path.write_bytes(body)
    meta = {
        "source_name": source.name,
        "domain": source.domain,
        "category": source.category,
        "fetch_kind": source.fetch_kind,
        "request_url": request_url,
        "method": method,
        "status_code": status_code,
        "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "byte_size": len(body),
    }
    meta_path = day_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    digest = hashlib.sha256(body).hexdigest()
    return body_path, meta_path, digest


# ---------------------------------------------------------------------------
# RSS / Atom


def _fetch_rss(
    session: HttpSession,
    source: NewsSource,
    raw_dir: Path,
    now: datetime,
) -> tuple[list[FetchedItem], tuple[Path | None, Path | None, str | None]]:
    body_text, body, status = _checked_get(session, source.entry_url)
    snapshot = _write_snapshot(
        raw_dir, source, now, body,
        request_url=source.entry_url, method="GET", status_code=status,
    )
    try:
        root = ET.fromstring(body_text)
    except ET.ParseError as exc:
        raise SiteFetchError(f"RSS/Atom 解析失败：{source.entry_url}（{exc}）") from exc

    items: list[FetchedItem] = []
    for element in root.iter():
        local = _local_name(element.tag)
        if local not in ("item", "entry"):
            continue
        children: dict[str, list[ET.Element]] = {}
        for child in element:
            children.setdefault(_local_name(child.tag), []).append(child)
        title = _clean_text(_element_text(children.get("title", [None])[0]))
        if not title:
            continue
        link = _atom_or_rss_link(children)
        published = _first_text(
            children.get("pubDate", [])
            + children.get("published", [])
            + children.get("updated", [])
        )
        summary = _clean_text(
            _first_text(children.get("description", []) + children.get("summary", []))
        )
        items.append(
            FetchedItem(
                title=title,
                url=urljoin(source.entry_url, link) if link else source.entry_url,
                published_at=_normalize_publish_time(published),
                summary=summary,
            )
        )
    return items, snapshot


def _local_name(tag: object) -> str:
    return str(tag).split("}")[-1] if isinstance(tag, str) else ""


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext())


def _first_text(elements: list[ET.Element]) -> str:
    for element in elements:
        text = _clean_text(_element_text(element))
        if text:
            return text
    return ""


def _atom_or_rss_link(children: dict[str, list[ET.Element]]) -> str:
    links = children.get("link", [])
    # Atom links carry the URL in href (prefer rel=alternate); RSS links carry
    # it as element text.
    for link in links:
        href = (link.get("href") or "").strip()
        if href and (link.get("rel") or "alternate") == "alternate":
            return href
    for link in links:
        href = (link.get("href") or "").strip()
        if href:
            return href
    for link in links:
        text = _clean_text(_element_text(link))
        if text:
            return text
    return _first_text(children.get("guid", []))


# ---------------------------------------------------------------------------
# Generic HTML list pages


class _AnchorTextParser(HTMLParser):
    """Flatten visible text while recording each anchor's span and href."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.buffer = ""
        self.anchors: list[dict[str, object]] = []
        self._open_anchors: list[dict[str, object]] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
            return
        if tag == "a" and self._skip_depth == 0:
            attr_map = dict(attrs)
            href = attr_map.get("href") or ""
            self._open_anchors.append(
                {"href": href, "title": attr_map.get("title") or "", "start": len(self.buffer)}
            )

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a" and self._open_anchors:
            anchor = self._open_anchors.pop()
            start = int(anchor["start"])  # type: ignore[arg-type]
            self.anchors.append(
                {
                    "href": anchor["href"],
                    "title": anchor["title"],
                    "start": start,
                    "end": len(self.buffer),
                }
            )

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.buffer += data


def _fetch_html_list(
    session: HttpSession,
    source: NewsSource,
    raw_dir: Path,
    now: datetime,
    max_items: int,
) -> tuple[list[FetchedItem], tuple[Path | None, Path | None, str | None]]:
    body_text, body, status = _checked_get(session, source.entry_url)
    snapshot = _write_snapshot(
        raw_dir, source, now, body,
        request_url=source.entry_url, method="GET", status_code=status,
    )

    parser = _AnchorTextParser()
    try:
        parser.feed(body_text)
    except Exception as exc:
        raise SiteFetchError(f"列表页解析失败：{source.entry_url}（{exc}）") from exc

    source_host = urlsplit(source.entry_url).hostname or ""
    items: list[FetchedItem] = []
    for index, anchor in enumerate(parser.anchors):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(_FORBIDDEN_HREF_PREFIXES):
            continue
        start, end = int(anchor["start"]), int(anchor["end"])
        # Gov/portal pages often truncate the visible anchor text and keep the
        # full headline in the ``title`` attribute — prefer the longer one.
        title_attr = _clean_text(str(anchor.get("title") or ""))
        title = _clean_text(parser.buffer[start:end])
        if len(title_attr) > len(title):
            title = title_attr
        if len(title) < _MIN_TITLE_LENGTH:
            continue
        absolute = urljoin(source.entry_url, href)
        parsed = urlsplit(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        host = (parsed.hostname or "").lower()
        if not (host == source_host or host.endswith(f".{source_host}")):
            continue
        previous_end = int(parser.anchors[index - 1]["end"]) if index > 0 else 0
        next_start = int(parser.anchors[index + 1]["start"]) if index + 1 < len(parser.anchors) else len(parser.buffer)
        # List pages usually print the date after the title; search right
        # context first so a neighbouring item's date is not adopted.
        published = (
            _normalize_publish_time(_find_date(parser.buffer[end:min(next_start, end + _DATE_CONTEXT_CHARS)]))
            or _normalize_publish_time(_find_date(parser.buffer[max(previous_end, start - _DATE_CONTEXT_CHARS):start]))
        )
        items.append(
            FetchedItem(
                title=title,
                url=absolute,
                published_at=published,
                summary="",
            )
        )
        if len(items) >= max_items:
            break
    return items, snapshot


def _find_date(text: str) -> str | None:
    match = _DATE_PATTERN.search(text)
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# JSON APIs (cninfo disclosure query implemented; more parsers follow live tuning)


_CNINFO_DOMAIN = "cninfo.com.cn"


def _fetch_api(
    session: HttpSession,
    source: NewsSource,
    raw_dir: Path,
    now: datetime,
    cutoff: datetime,
    max_items: int,
) -> tuple[list[FetchedItem], tuple[Path | None, Path | None, str | None]]:
    host = urlsplit(source.entry_url).hostname or ""
    if host.endswith(_CNINFO_DOMAIN):
        return _fetch_cninfo(session, source, raw_dir, now, cutoff, max_items)
    raise SiteFetchError(
        f"该信源的 API 解析器尚未实现，请先在启用前完成实调配置：{source.name}（{source.domain}）"
    )


def _fetch_cninfo(
    session: HttpSession,
    source: NewsSource,
    raw_dir: Path,
    now: datetime,
    cutoff: datetime,
    max_items: int,
) -> tuple[list[FetchedItem], tuple[Path | None, Path | None, str | None]]:
    try:
        config = json.loads(source.parser_config) if source.parser_config else {}
    except json.JSONDecodeError:
        config = {}
    config = config if isinstance(config, dict) else {}
    form = {
        "pageNum": 1,
        "pageSize": max(1, min(max_items, 30)),
        "column": str(config.get("column") or "szse"),
        "tabName": str(config.get("tabName") or "fulltext"),
        "plate": "",
        "stock": str(config.get("stock") or ""),
        "searchkey": str(config.get("searchkey") or ""),
        "secid": "",
        "category": str(config.get("category") or ""),
        "trade": str(config.get("trade") or ""),
        "seDate": f"{cutoff:%Y-%m-%d}~{now:%Y-%m-%d}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    try:
        response = session.post(
            source.entry_url, data=form, timeout=FETCH_TIMEOUT_SECONDS, headers=_http_headers()
        )
    except Exception as exc:
        raise SiteFetchError(f"巨潮接口请求失败：{source.entry_url}（{exc}）") from exc
    if response.status_code >= 400:
        raise SiteFetchError(f"巨潮接口返回 HTTP {response.status_code}：{source.entry_url}")
    try:
        payload = response.json()
    except Exception as exc:
        raise SiteFetchError(f"巨潮接口返回非 JSON 内容：{source.entry_url}") from exc
    body = response.content
    snapshot = _write_snapshot(
        raw_dir, source, now, body,
        request_url=source.entry_url, method="POST", status_code=response.status_code,
    )
    if not isinstance(payload, dict):
        raise SiteFetchError("巨潮接口返回结构异常")
    announcements = payload.get("announcements") or []
    items: list[FetchedItem] = []
    for announcement in announcements:
        if not isinstance(announcement, dict):
            continue
        raw_title = str(announcement.get("announcementTitle") or "")
        title = _clean_text(re.sub(r"<[^>]+>", "", raw_title))
        if not title:
            continue
        adjunct = str(announcement.get("adjunctUrl") or "").strip()
        url = f"https:{adjunct}" if adjunct.startswith("//") else adjunct
        sec_name = str(announcement.get("secName") or "").strip()
        sec_code = str(announcement.get("secCode") or "").strip()
        items.append(
            FetchedItem(
                title=title,
                url=url,
                published_at=_normalize_publish_time(announcement.get("announcementTime")),
                summary="",
                metadata={
                    "news_sec_name": sec_name,
                    "news_sec_code": sec_code,
                },
            )
        )
    return items, snapshot


# ---------------------------------------------------------------------------
# Shared parsing helpers


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _ensure_cst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_CST)
    return value.astimezone(_CST)


def _to_cst_datetime(value: object) -> datetime | None:
    """Parse assorted publish-time formats into an aware CST datetime.

    Date-only values are treated as end-of-day so a list page that only shows
    ``2026-08-30`` stays inside a 24-hour window started later that day.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed: datetime | None = None
    date_only = False
    if re.fullmatch(r"\d{13}", text):
        parsed = datetime.fromtimestamp(int(text) / 1000, tz=_CST)
    elif re.fullmatch(r"\d{10}", text):
        parsed = datetime.fromtimestamp(int(text), tz=_CST)
    elif re.fullmatch(r"\d{1,2}-\d{1,2}", text):
        # Month-day-only dates are common on government list pages; the year
        # is implied as the current year, or the previous year when that date
        # has not happened yet.
        now = datetime.now(_CST)
        month, day = (int(part) for part in text.split("-"))
        try:
            parsed = datetime(now.year, month, day, 23, 59, 59, tzinfo=_CST)
        except ValueError:
            return None
        if parsed > now + timedelta(days=1):
            try:
                parsed = parsed.replace(year=now.year - 1)
            except ValueError:
                return None
        date_only = True
    else:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            parsed = None
        if parsed is None:
            candidate = text.replace("Z", "+00:00")
            if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", candidate):
                date_only = True
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                parsed = None
        if parsed is None:
            for pattern in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d",
                "%Y年%m月%d日 %H:%M",
                "%Y年%m月%d日",
                "%Y.%m.%d",
                "%Y/%m/%d",
            ):
                try:
                    parsed = datetime.strptime(text, pattern)
                    date_only = pattern in (
                        "%Y-%m-%d",
                        "%Y年%m月%d日",
                        "%Y.%m.%d",
                        "%Y/%m/%d",
                    )
                    break
                except ValueError:
                    continue
    if parsed is None:
        return None
    if date_only:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return _ensure_cst(parsed)


def _normalize_publish_time(value: object) -> str | None:
    """Normalize assorted publish-time formats to ``YYYY-MM-DD HH:MM:SS`` CST."""

    parsed = _to_cst_datetime(value)
    if parsed is None:
        return None
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


__all__ = [
    "FETCH_TIMEOUT_SECONDS",
    "FetchedItem",
    "HttpSession",
    "SiteFetchBatch",
    "SiteFetchError",
    "fetch_site_source",
]
