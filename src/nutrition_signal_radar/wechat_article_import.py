"""Operator-assisted WeChat article import for accounts that provider search
indexes cannot serve by exact account name.

This is the deliberate "human pastes a link" path: the operator confirms the
article belongs to a configured account, and the workbench fetches the public
page exactly once to store its body as a discovery-lead snapshot.  Nothing
here enables an account, approves a topic, or publishes anywhere.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .article_contract import (
    ArticleBatch,
    NormalizedArticle,
    canonicalize_article_url,
)
from .database import connect_database
from .ingest_service import import_article_batch
from .schema import initialize_schema
from .source_registry import SourceRegistryError


_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_CST = timezone(timedelta(hours=8), name="Asia/Shanghai")


class WechatHttpSession(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> object: ...


@dataclass(frozen=True)
class WechatArticleFetchResult:
    nickname: str | None
    title: str | None
    published_at: str | None
    body: str


def fetch_wechat_article(
    url: str,
    *,
    session: WechatHttpSession | None = None,
    timeout: float = 30.0,
) -> WechatArticleFetchResult:
    """Read one public WeChat article page and normalize its text fields."""

    from requests import Session

    cleaned_url = str(url).strip()
    if not cleaned_url.startswith(("https://", "http://")):
        raise SourceRegistryError("文章链接必须是完整的 HTTP(S) URL")
    active_session = session or Session()
    response = active_session.get(
        cleaned_url,
        headers={"User-Agent": _DEFAULT_USER_AGENT},
        timeout=timeout,
    )
    text = getattr(response, "text", "")
    if not text:
        raise SourceRegistryError("未能读取文章页面内容")
    return _parse_wechat_article_html(str(text))


def import_wechat_article(
    database_path: Path,
    url: str,
    *,
    account_name: str,
    actor: str = "operator",
    session: WechatHttpSession | None = None,
    published_at: str | None = None,
    title: str | None = None,
    body: str | None = None,
) -> dict[str, object]:
    """Store one operator-confirmed article under a configured source."""

    cleaned_account = str(account_name).strip()
    if not cleaned_account:
        raise SourceRegistryError("来源名称不能为空")
    initialize_schema(database_path)
    connection = connect_database(Path(database_path).resolve(), readonly=True)
    try:
        account_row = connection.execute(
            "SELECT id, name, redfox_name FROM source_accounts WHERE name = ?",
            (cleaned_account,),
        ).fetchone()
    finally:
        connection.close()
    if account_row is None:
        raise SourceRegistryError(f"来源不存在：{cleaned_account}，请先配置来源")

    if body is None:
        fetched = fetch_wechat_article(url, session=session)
        body = fetched.body
        title = title or fetched.title
        published_at = published_at or fetched.published_at
    title = str(title or "").strip()
    if not title:
        raise SourceRegistryError("文章标题为空，无法导入")
    if not str(body or "").strip():
        raise SourceRegistryError("文章正文为空，无法导入")

    normalized_url = canonicalize_article_url(url)
    if normalized_url is None:
        raise SourceRegistryError("文章链接无法规范化")
    content_hash = hashlib.sha256(str(body).encode("utf-8")).hexdigest()
    article = NormalizedArticle(
        external_id=None,
        account_name=cleaned_account,
        title=title,
        published_at=_normalize_published_at(published_at),
        url=normalized_url,
        content_snapshot=str(body),
        content_hash=content_hash,
    )
    result = import_article_batch(
        Path(database_path).resolve(),
        ArticleBatch(
            source_kind="redfox",
            source_path=url,
            source_fingerprint=f"manual:{content_hash[:16]}",
            source_rows=1,
            articles=(article,),
        ),
    )
    write_connection = connect_database(Path(database_path).resolve())
    try:
        write_connection.execute(
            """
            INSERT INTO source_registry_events(
                entity_kind, entity_id, event_type, actor, payload_json
            ) VALUES ('account', ?, 'manual_article_imported', ?, ?)
            """,
            (
                int(account_row["id"]),
                (actor or "operator").strip() or "operator",
                '{"url": %s, "title": %s}' % (
                    _json_string(normalized_url),
                    _json_string(title),
                ),
            ),
        )
        write_connection.commit()
    finally:
        write_connection.close()
    return {
        "database_path": str(Path(database_path).resolve()),
        "account": cleaned_account,
        "url": normalized_url,
        "title": title,
        "articles_inserted": result.articles_inserted,
        "articles_updated": result.articles_updated,
        "cycle_slug": result.cycle_slug,
        "in_period": result.in_period_articles,
    }


def _parse_wechat_article_html(text: str) -> WechatArticleFetchResult:
    nickname = _first_match(
        text,
        (
            r'nickname\s*=\s*(?:htmlDecode\s*\()?["\']([^"\']+)["\']',
            r'user_name\s*=\s*["\']([^"\']+)["\']',
        ),
    )
    title = _first_match(
        text,
        (
            r'<h1[^>]*id="activity-name"[^>]*>(.*?)</h1>',
            r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
        ),
    )
    if title:
        title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
    published_at = _first_match(
        text,
        (
            r'createTime\s*=\s*["\']([^"\']+)["\']',
            r'var\s+ct\s*=\s*["\']?(\d{10})["\']?',
            r'<meta[^>]+property="article:published_time"[^>]+content="([^"]+)"',
        ),
    )
    content_match = re.search(r'id="js_content"[^>]*>(.*?)</div>\s*<script', text, re.S)
    if content_match is None:
        content_match = re.search(r'id="js_content"[^>]*>(.*)', text, re.S)
    body = ""
    if content_match:
        body = html.unescape(re.sub(r"<[^>]+>", "\n", content_match.group(1)))
        body = re.sub(r"[ \t]+", " ", body)
        body = re.sub(r"\n{2,}", "\n", body).strip()
    return WechatArticleFetchResult(
        nickname=html.unescape(nickname).strip() if nickname else None,
        title=title or None,
        published_at=_normalize_published_at(published_at),
        body=body,
    )


def _first_match(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _normalize_published_at(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    if cleaned.isdigit() and len(cleaned) == 10:
        try:
            timestamp = int(cleaned)
            if timestamp > 10_000_000_000:
                timestamp //= 1000
            return datetime.fromtimestamp(timestamp, tz=_CST).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    normalized = cleaned.replace("/", "-").replace("T", " ")
    for format_string in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(normalized, format_string)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_CST)
            return parsed.isoformat(timespec="seconds")
        except ValueError:
            continue
    return cleaned


def _json_string(value: str) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
