"""A source-neutral RedFox adapter for public-account monitoring.

The adapter intentionally has no database writes and no scheduling behaviour.
It talks to the three documented quality-library endpoints used in Phase 0 and
to the broad-library keyword search endpoint used for bounded discovery.  The
broad-library path is deliberately a discovery/validated-source fallback, not
an automatic source-enablement mechanism.  Callers can inject an HTTP session
in tests; real calls are made only from an explicit CLI validation command.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import requests

from .article_contract import (
    ArticleBatch,
    NormalizedArticle,
    RowIssue,
    canonicalize_article_url,
    external_id_for_article,
)
from .config import (
    DEFAULT_REDFOX_BASE_URL,
    DEFAULT_REDFOX_MAX_RETRIES,
    DEFAULT_REDFOX_TIMEOUT_SECONDS,
)


DEFAULT_REDFOX_ACCOUNT_NAMES = (
    "庶正康讯",
    "营养健康评论",
    "Bonny营养领航",
    "健康产业观察",
    "营养品观察",
    "功能食品圈",
)
REDFOX_PAGE_SIZE = 20
_CST = timezone(timedelta(hours=8), name="Asia/Shanghai")

_SEARCH_USER_PATH = "/story/api/gzhData/searchUser"
_WORK_LIST_PATH = "/story/api/gzhData/queryWorkList"
_WORK_DETAIL_PATH = "/story/api/gzhData/queryWork"
_WIDE_ARTICLE_SEARCH_PATH = "/story/api/gzh/data/searchArticle"
REDFOX_WIDE_SORT_TYPES = ("0", "2", "4")


class RedFoxGatewayError(RuntimeError):
    """A safe, normalized failure from the RedFox HTTP boundary."""


class RedFoxHttpResponse(Protocol):
    status_code: int

    def json(self) -> object: ...


class RedFoxHttpSession(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
        timeout: float,
    ) -> RedFoxHttpResponse: ...


@dataclass(frozen=True)
class RedFoxAccountCandidate:
    account: str
    account_name: str
    verify_info: str
    redfox_index: float | None
    last_publish_time: str | None
    last_article_title: str
    account_type: str

    def as_dict(self) -> dict[str, object]:
        return {
            "account": self.account,
            "account_name": self.account_name,
            "verify_info": self.verify_info,
            "redfox_index": self.redfox_index,
            "last_publish_time": self.last_publish_time,
            "last_article_title": self.last_article_title,
            "account_type": self.account_type,
        }


@dataclass(frozen=True)
class RedFoxAccountSearch:
    candidates: tuple[RedFoxAccountCandidate, ...]
    raw_snapshot_path: str | None


@dataclass(frozen=True)
class RedFoxListedWork:
    payload: Mapping[str, object]
    raw_snapshot_path: str | None


@dataclass(frozen=True)
class RedFoxListedWorks:
    works: tuple[RedFoxListedWork, ...]
    total: int | None
    pages_fetched: int
    pagination_truncated: bool
    raw_snapshot_paths: tuple[str, ...]


@dataclass(frozen=True)
class RedFoxWideSearchPage:
    """One bounded page from RedFox's documented broad article library."""

    keyword: str
    sort_type: str
    works: tuple[RedFoxListedWork, ...]
    total: int | None
    offset: int
    has_more: bool
    raw_snapshot_path: str | None


@dataclass(frozen=True)
class RedFoxAccountBatch:
    """One validated account's source-neutral articles plus acquisition facts."""

    batch: ArticleBatch
    total: int | None
    pages_fetched: int
    pagination_truncated: bool
    detail_eligible_articles: int
    detail_requests: int
    detail_successes: int
    detail_budget_exhausted: bool
    detail_errors: tuple[str, ...]
    articles_with_full_body: int
    raw_snapshot_paths: tuple[str, ...]


class RedFoxGateway:
    """Use RedFox documented public-account endpoints without leaking credentials."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_REDFOX_BASE_URL,
        timeout_seconds: float = DEFAULT_REDFOX_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_REDFOX_MAX_RETRIES,
        session: RedFoxHttpSession | None = None,
        raw_snapshot_directory: Path | None = None,
    ) -> None:
        if not str(api_key).strip():
            raise RedFoxGatewayError("RedFox API key is not configured")
        if not str(base_url).strip().startswith(("https://", "http://")):
            raise RedFoxGatewayError("RedFox base URL must be an absolute HTTP(S) URL")
        if timeout_seconds <= 0:
            raise RedFoxGatewayError("RedFox timeout must be positive")
        if not isinstance(max_retries, int) or max_retries < 0:
            raise RedFoxGatewayError("RedFox max_retries must be a non-negative integer")

        self._api_key = str(api_key).strip()
        self._base_url = str(base_url).strip().rstrip("/")
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = max_retries
        self._session = session or requests.Session()
        self._raw_snapshot_directory = (
            Path(raw_snapshot_directory).resolve() if raw_snapshot_directory else None
        )
        self._snapshot_counter = 0

    def search_accounts(self, keyword: str, *, offset: int = 0) -> RedFoxAccountSearch:
        """Search public-account candidates without choosing one automatically."""
        value = _clean_text(keyword)
        if not value:
            raise RedFoxGatewayError("RedFox account search requires a keyword")
        if offset < 0 or offset % REDFOX_PAGE_SIZE != 0:
            raise RedFoxGatewayError("RedFox account-search offset must be a non-negative multiple of 20")

        payload, raw_path = self._post_json(
            "search_user",
            _SEARCH_USER_PATH,
            {"keyword": value, "offset": offset, "sortType": "_0"},
        )
        data = _response_data(payload, operation="search_user")
        rows = _response_list(data, operation="search_user")
        candidates = tuple(_account_candidate(row) for row in rows if isinstance(row, Mapping))
        return RedFoxAccountSearch(candidates=candidates, raw_snapshot_path=raw_path)

    def list_works(
        self,
        *,
        account: str,
        account_name: str,
        publish_time_start: str,
        publish_time_end: str,
        max_pages: int,
    ) -> RedFoxListedWorks:
        """Fetch the documented 20-row pages for one confirmed RedFox account."""
        account_value = _clean_text(account)
        if not account_value:
            raise RedFoxGatewayError("RedFox work-list request requires an account identifier")
        if max_pages < 1:
            raise RedFoxGatewayError("RedFox max_pages must be at least 1")

        works: list[RedFoxListedWork] = []
        raw_paths: list[str] = []
        total: int | None = None
        has_more = False
        pages_fetched = 0
        for page_number in range(max_pages):
            offset = page_number * REDFOX_PAGE_SIZE
            payload, raw_path = self._post_json(
                "query_work_list",
                _WORK_LIST_PATH,
                {
                    "account": account_value,
                    "accountName": _clean_text(account_name),
                    "offset": offset,
                    "sortType": "_2",
                    "publishTimeStart": _clean_text(publish_time_start),
                    "publishTimeEnd": _clean_text(publish_time_end),
                },
            )
            data = _response_data(payload, operation="query_work_list")
            rows = _response_list(data, operation="query_work_list")
            total = _optional_int(data.get("total")) if total is None else total
            has_more = _as_bool(data.get("hasMore"))
            pages_fetched += 1
            if raw_path:
                raw_paths.append(raw_path)
            works.extend(
                RedFoxListedWork(payload=dict(row), raw_snapshot_path=raw_path)
                for row in rows
                if isinstance(row, Mapping)
            )
            if not has_more:
                break

        return RedFoxListedWorks(
            works=tuple(works),
            total=total,
            pages_fetched=pages_fetched,
            pagination_truncated=has_more and pages_fetched >= max_pages,
            raw_snapshot_paths=tuple(raw_paths),
        )

    def get_work_detail(self, work_uuid: str) -> tuple[Mapping[str, object], str | None]:
        """Fetch a full article body when a list page omits ``content``."""
        value = _clean_text(work_uuid)
        if not value:
            raise RedFoxGatewayError("RedFox work-detail request requires workUuid")
        payload, raw_path = self._post_json(
            "query_work_detail",
            _WORK_DETAIL_PATH,
            {"workUuid": value},
        )
        return _response_data(payload, operation="query_work_detail"), raw_path

    def search_wide_articles(
        self,
        keyword: str,
        *,
        offset: int = 0,
        sort_type: str = "2",
    ) -> RedFoxWideSearchPage:
        """Search one page of the broad library without choosing an account.

        The provider searches titles, summaries, and authors.  A caller that
        wants to treat the result as one monitored account must still filter
        by an explicitly confirmed, exact author name before importing it.
        """

        value = _clean_text(keyword)
        if not value:
            raise RedFoxGatewayError("RedFox broad search requires a keyword")
        if offset < 0 or offset % REDFOX_PAGE_SIZE != 0:
            raise RedFoxGatewayError("RedFox broad-search offset must be a non-negative multiple of 20")
        if sort_type not in REDFOX_WIDE_SORT_TYPES:
            allowed = ", ".join(REDFOX_WIDE_SORT_TYPES)
            raise RedFoxGatewayError(f"RedFox broad-search sort_type must be one of: {allowed}")

        payload, raw_path = self._post_json(
            "search_wide_articles",
            _WIDE_ARTICLE_SEARCH_PATH,
            {"keyword": value, "offset": offset, "sortType": sort_type},
        )
        data = _response_data(payload, operation="search_wide_articles")
        rows = _response_list(data, operation="search_wide_articles")
        total = _optional_int(data.get("total"))
        has_more = (
            offset + len(rows) < total
            if total is not None
            else len(rows) >= REDFOX_PAGE_SIZE
        )
        return RedFoxWideSearchPage(
            keyword=value,
            sort_type=sort_type,
            works=tuple(
                RedFoxListedWork(payload=dict(row), raw_snapshot_path=raw_path)
                for row in rows
            ),
            total=total,
            offset=offset,
            has_more=has_more,
            raw_snapshot_path=raw_path,
        )

    def fetch_wide_account_batch(
        self,
        *,
        account_name: str,
        publish_time_start: str,
        publish_time_end: str,
        max_pages: int = 3,
    ) -> RedFoxAccountBatch:
        """Fetch an exact-author subset from broad search for a confirmed source.

        RedFox's broad endpoint is keyword based, not an account-list API.  We
        therefore use it only when the operator has configured the exact
        display name and retain only results whose ``author`` exactly matches
        that name.  Results outside the requested date window are discarded.
        """

        requested_name = _clean_text(account_name)
        if not requested_name:
            raise RedFoxGatewayError("RedFox broad account fetch requires an account name")
        if max_pages < 1:
            raise RedFoxGatewayError("RedFox max_pages must be at least 1")

        matched_works: list[RedFoxListedWork] = []
        raw_paths: list[str] = []
        total: int | None = None
        pages_fetched = 0
        has_more = False
        for page_number in range(max_pages):
            page = self.search_wide_articles(
                requested_name,
                offset=page_number * REDFOX_PAGE_SIZE,
                sort_type="2",
            )
            pages_fetched += 1
            total = page.total if total is None else total
            has_more = page.has_more
            if page.raw_snapshot_path:
                raw_paths.append(page.raw_snapshot_path)
            matched_works.extend(
                work
                for work in page.works
                if normalize_account_name(work.payload.get("author"))
                == normalize_account_name(requested_name)
                and _published_in_range(
                    work.payload.get("publishTime"),
                    start=publish_time_start,
                    end=publish_time_end,
                )
            )
            if not page.has_more:
                break

        articles: list[NormalizedArticle] = []
        issues: list[RowIssue] = []
        for row_number, listed_work in enumerate(matched_works, start=1):
            work = dict(listed_work.payload)
            content = _clean_text(work.get("content"))
            body_state = "inline" if content else "missing"
            raw_references = _unique_paths((listed_work.raw_snapshot_path,))
            title = _clean_text(work.get("title"))
            if not title:
                issues.append(RowIssue(row_number=row_number, reason="missing title"))
                continue
            url = canonicalize_article_url(
                _clean_text(work.get("workUrl")) or _clean_text(work.get("sourceUrl")) or None
            )
            snapshot = content or _clean_text(work.get("summary"))
            if not snapshot:
                body_state = "missing"
            elif body_state == "missing":
                body_state = "summary"
            content_hash = _hash_text(snapshot) if snapshot else None
            work_uuid = _clean_text(work.get("workUuid"))
            external_id = (
                f"redfox:{work_uuid}"
                if work_uuid
                else external_id_for_article(
                    url=url,
                    account_name=requested_name,
                    title=title,
                    published_at=_published_at(work.get("publishTime")),
                    content_hash=content_hash,
                )
            )
            metrics = _metrics_from_work(work)
            articles.append(
                NormalizedArticle(
                    external_id=external_id,
                    account_name=requested_name,
                    title=title,
                    published_at=_published_at(work.get("publishTime")),
                    url=url,
                    content_snapshot=snapshot,
                    content_hash=content_hash,
                    read_count=metrics["read_count"],
                    like_count=metrics["like_count"],
                    share_count=metrics["share_count"],
                    recommend_count=metrics["recommend_count"],
                    metadata={
                        "source_kind": "redfox_wide",
                        "redfox_library": "wide",
                        "redfox_account": None,
                        "redfox_account_name": requested_name,
                        "redfox_work_uuid": work_uuid or None,
                        "redfox_body_state": body_state,
                        "redfox_original_author": _clean_text(work.get("originalAuthor")),
                        "redfox_order_num": _optional_int(work.get("orderNum")),
                        "redfox_is_original": _optional_int(work.get("isOriginal")),
                        "redfox_account_type": _clean_text(work.get("accountType")),
                        "redfox_metrics": {
                            "watch_count": _nonnegative_int(work.get("watchCount")),
                            "comment_count": _nonnegative_int(work.get("commentCount")),
                            "collect_count": _nonnegative_int(work.get("collectCount")),
                            "reward_count": _nonnegative_int(work.get("rewardCount")),
                        },
                        "redfox_raw_snapshot_paths": list(raw_references),
                    },
                    source_reference=raw_references[-1] if raw_references else None,
                )
            )

        deduplicated, collapsed = _collapse_articles(articles)
        full_body_count = sum(
            article.metadata.get("redfox_body_state") == "inline" for article in deduplicated
        )
        fingerprint_material = {
            "library": "wide",
            "account_name": requested_name,
            "publish_time_start": _clean_text(publish_time_start),
            "publish_time_end": _clean_text(publish_time_end),
            "work_uuids": [article.external_id for article in deduplicated],
        }
        source_fingerprint = "sha256:" + _hash_text(
            json.dumps(fingerprint_material, ensure_ascii=False, sort_keys=True)
        )
        batch = ArticleBatch(
            source_kind="redfox_wide",
            source_path=f"{self._base_url}{_WIDE_ARTICLE_SEARCH_PATH}",
            source_fingerprint=source_fingerprint,
            source_rows=len(matched_works),
            articles=tuple(deduplicated),
            skipped_rows=len(issues),
            collapsed_rows=collapsed,
            issues=tuple(issues),
            account_segment="industry_media",
        )
        return RedFoxAccountBatch(
            batch=batch,
            total=total,
            pages_fetched=pages_fetched,
            pagination_truncated=has_more and pages_fetched >= max_pages,
            detail_eligible_articles=sum(
                not _clean_text(work.payload.get("content")) for work in matched_works
            ),
            detail_requests=0,
            detail_successes=0,
            detail_budget_exhausted=False,
            detail_errors=(),
            articles_with_full_body=full_body_count,
            raw_snapshot_paths=tuple(_unique_paths(raw_paths)),
        )

    def fetch_account_batch(
        self,
        *,
        account: str,
        account_name: str,
        publish_time_start: str,
        publish_time_end: str,
        max_pages: int = 10,
        max_detail_requests: int = 30,
    ) -> RedFoxAccountBatch:
        """List, fill missing bodies within a capped budget, and normalize articles."""
        if max_detail_requests < 0:
            raise RedFoxGatewayError("RedFox max_detail_requests cannot be negative")

        listing = self.list_works(
            account=account,
            account_name=account_name,
            publish_time_start=publish_time_start,
            publish_time_end=publish_time_end,
            max_pages=max_pages,
        )
        articles: list[NormalizedArticle] = []
        issues: list[RowIssue] = []
        detail_eligible_articles = 0
        detail_requests = 0
        detail_successes = 0
        detail_errors: list[str] = []
        raw_paths = list(listing.raw_snapshot_paths)

        for row_number, listed_work in enumerate(listing.works, start=1):
            work = dict(listed_work.payload)
            body_state, content = _body_from_work(work)
            raw_references = _unique_paths((listed_work.raw_snapshot_path,))
            work_uuid = _clean_text(work.get("workUuid"))

            if not content and work_uuid:
                detail_eligible_articles += 1
                if detail_requests < max_detail_requests:
                    detail_requests += 1
                    try:
                        detail, detail_raw_path = self.get_work_detail(work_uuid)
                        work = _merge_work_fields(work, detail)
                        body_state, content = _body_from_work(work)
                        if content:
                            body_state = "detail"
                            detail_successes += 1
                        raw_references = _unique_paths((*raw_references, detail_raw_path))
                        if detail_raw_path:
                            raw_paths.append(detail_raw_path)
                    except RedFoxGatewayError as exc:
                        body_state = "detail_error"
                        detail_errors.append(f"{work_uuid}: {exc}")

            title = _clean_text(work.get("title"))
            if not title:
                issues.append(RowIssue(row_number=row_number, reason="missing title"))
                continue
            url = canonicalize_article_url(
                _clean_text(work.get("workUrl")) or _clean_text(work.get("sourceUrl")) or None
            )
            snapshot = content or _clean_text(work.get("summary"))
            if not snapshot:
                body_state = "missing"
            elif body_state == "missing":
                body_state = "summary"
            content_hash = _hash_text(snapshot) if snapshot else None
            external_id = (
                f"redfox:{work_uuid}"
                if work_uuid
                else external_id_for_article(
                    url=url,
                    account_name=account_name,
                    title=title,
                    published_at=_published_at(work.get("publishTime")),
                    content_hash=content_hash,
                )
            )
            metrics = _metrics_from_work(work)
            articles.append(
                NormalizedArticle(
                    external_id=external_id,
                    account_name=_clean_text(account_name),
                    title=title,
                    published_at=_published_at(work.get("publishTime")),
                    url=url,
                    content_snapshot=snapshot,
                    content_hash=content_hash,
                    read_count=metrics["read_count"],
                    like_count=metrics["like_count"],
                    share_count=metrics["share_count"],
                    recommend_count=metrics["recommend_count"],
                    metadata={
                        "source_kind": "redfox",
                        "redfox_account": _clean_text(account),
                        "redfox_account_name": _clean_text(account_name),
                        "redfox_work_uuid": work_uuid or None,
                        "redfox_body_state": body_state,
                        "redfox_original_author": _clean_text(work.get("originalAuthor")),
                        "redfox_order_num": _optional_int(work.get("orderNum")),
                        "redfox_is_original": _optional_int(work.get("isOriginal")),
                        "redfox_account_type": _clean_text(work.get("accountType")),
                        "redfox_metrics": {
                            "watch_count": _nonnegative_int(work.get("watchCount")),
                            "comment_count": _nonnegative_int(work.get("commentCount")),
                            "collect_count": _nonnegative_int(work.get("collectCount")),
                            "reward_count": _nonnegative_int(work.get("rewardCount")),
                        },
                        "redfox_raw_snapshot_paths": list(raw_references),
                    },
                    source_reference=raw_references[-1] if raw_references else None,
                )
            )

        deduplicated, collapsed = _collapse_articles(articles)
        full_body_count = sum(
            article.metadata.get("redfox_body_state") in {"inline", "detail"}
            for article in deduplicated
        )
        fingerprint_material = {
            "account": _clean_text(account),
            "account_name": _clean_text(account_name),
            "publish_time_start": _clean_text(publish_time_start),
            "publish_time_end": _clean_text(publish_time_end),
            "work_uuids": [article.external_id for article in deduplicated],
        }
        source_fingerprint = "sha256:" + _hash_text(
            json.dumps(fingerprint_material, ensure_ascii=False, sort_keys=True)
        )
        batch = ArticleBatch(
            source_kind="redfox",
            source_path=f"{self._base_url}{_WORK_LIST_PATH}",
            source_fingerprint=source_fingerprint,
            source_rows=len(listing.works),
            articles=tuple(deduplicated),
            skipped_rows=len(issues),
            collapsed_rows=collapsed,
            issues=tuple(issues),
            account_segment="industry_media",
        )
        return RedFoxAccountBatch(
            batch=batch,
            total=listing.total,
            pages_fetched=listing.pages_fetched,
            pagination_truncated=listing.pagination_truncated,
            detail_eligible_articles=detail_eligible_articles,
            detail_requests=detail_requests,
            detail_successes=detail_successes,
            detail_budget_exhausted=detail_eligible_articles > detail_requests,
            detail_errors=tuple(detail_errors),
            articles_with_full_body=full_body_count,
            raw_snapshot_paths=tuple(_unique_paths(raw_paths)),
        )

    def _post_json(
        self,
        operation: str,
        path: str,
        payload: Mapping[str, object],
    ) -> tuple[Mapping[str, object], str | None]:
        url = f"{self._base_url}{path}"
        for attempt in range(self._max_retries + 1):
            try:
                response = self._session.post(
                    url,
                    headers={
                        "Content-Type": "application/json",
                        "REDFOX_API_KEY": self._api_key,
                    },
                    json=dict(payload),
                    timeout=self._timeout_seconds,
                )
            except Exception as exc:  # Requests and injected test sessions share no common base class.
                error = RedFoxGatewayError(
                    f"RedFox {operation} request failed: {type(exc).__name__}"
                )
                if attempt < self._max_retries:
                    continue
                raise error from exc

            status_code = _optional_int(getattr(response, "status_code", None))
            try:
                response_payload = response.json()
            except Exception as exc:
                response_preview = _clean_text(getattr(response, "text", ""))[:1000]
                snapshot_response: dict[str, object] = {"non_json_response": True}
                if response_preview:
                    snapshot_response["body_preview"] = self._redact_text(response_preview)
                self._write_raw_snapshot(
                    operation,
                    url=url,
                    request_payload=payload,
                    response_payload=snapshot_response,
                    http_status=status_code,
                )
                error = RedFoxGatewayError(f"RedFox {operation} returned a non-JSON response")
                if attempt < self._max_retries and _retryable_http_status(status_code):
                    continue
                raise error from exc

            raw_path = self._write_raw_snapshot(
                operation,
                url=url,
                request_payload=payload,
                response_payload=response_payload,
                http_status=status_code,
            )
            if not isinstance(response_payload, Mapping):
                error = RedFoxGatewayError(
                    f"RedFox {operation} returned an invalid JSON envelope"
                )
                if attempt < self._max_retries and _retryable_http_status(status_code):
                    continue
                raise error
            if status_code is None or not 200 <= status_code < 300:
                message = _clean_text(response_payload.get("msg")) or "HTTP request failed"
                error = RedFoxGatewayError(
                    f"RedFox {operation} failed: {self._redact_text(message)}"
                )
                if attempt < self._max_retries and _retryable_http_status(status_code):
                    continue
                raise error
            if _optional_int(response_payload.get("code")) != 2000:
                message = _clean_text(response_payload.get("msg")) or "provider returned an error code"
                raise RedFoxGatewayError(
                    f"RedFox {operation} failed: {self._redact_text(message)}"
                )
            return dict(response_payload), raw_path

        raise AssertionError("RedFox retry loop exited without a response")

    def _write_raw_snapshot(
        self,
        operation: str,
        *,
        url: str,
        request_payload: Mapping[str, object],
        response_payload: object,
        http_status: int | None,
    ) -> str | None:
        if self._raw_snapshot_directory is None:
            return None
        now = datetime.now(timezone.utc).astimezone(_CST)
        directory = self._raw_snapshot_directory / now.date().isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        self._snapshot_counter += 1
        target = directory / (
            f"{now.strftime('%H%M%S%f')}-{self._snapshot_counter:04d}-{operation}.json"
        )
        record = {
            "captured_at": now.isoformat(timespec="seconds"),
            "operation": operation,
            "request": {
                "method": "POST",
                "url": url,
                "json": _redact_secret(dict(request_payload), self._api_key),
            },
            "http_status": http_status,
            "response": _redact_secret(response_payload, self._api_key),
        }
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        temporary.replace(target)
        return str(target)

    def _redact_text(self, value: str) -> str:
        return value.replace(self._api_key, "[redacted]")


def normalize_account_name(value: object) -> str:
    """Compare public account names without turning fuzzy search into auto-selection."""
    normalized = unicodedata.normalize("NFKC", _clean_text(value)).casefold()
    return re.sub(r"\s+", "", normalized)


def _response_data(payload: Mapping[str, object], *, operation: str) -> Mapping[str, object]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise RedFoxGatewayError(f"RedFox {operation} response is missing an object data field")
    return dict(data)


def _response_list(data: Mapping[str, object], *, operation: str) -> tuple[Mapping[str, object], ...]:
    rows = data.get("list")
    if rows is None:
        return ()
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise RedFoxGatewayError(f"RedFox {operation} response data.list is not an array")
    return tuple(row for row in rows if isinstance(row, Mapping))


def _account_candidate(row: Mapping[str, object]) -> RedFoxAccountCandidate:
    return RedFoxAccountCandidate(
        account=_clean_text(row.get("account")),
        account_name=_clean_text(row.get("accountName")),
        verify_info=_clean_text(row.get("verifyInfo")),
        redfox_index=_optional_float(row.get("redfoxIndex")),
        last_publish_time=_published_at(row.get("lastPublishTime")),
        last_article_title=_clean_text(row.get("lastArticleTitle")),
        account_type=_clean_text(row.get("accountType")),
    )


def _body_from_work(work: Mapping[str, object]) -> tuple[str, str]:
    content = _clean_text(work.get("content"))
    if content:
        return "inline", content
    return "missing", ""


def _merge_work_fields(
    listed_work: Mapping[str, object], detail_work: Mapping[str, object]
) -> dict[str, object]:
    merged = dict(listed_work)
    for key, value in detail_work.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _metrics_from_work(work: Mapping[str, object]) -> dict[str, int]:
    return {
        "read_count": _nonnegative_int(work.get("readCount")),
        "like_count": _nonnegative_int(work.get("likeCount")),
        "share_count": _nonnegative_int(work.get("shareCount")),
        "recommend_count": _nonnegative_int(work.get("watchCount")),
    }


def _published_at(value: object) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    if text.replace(".", "", 1).isdigit():
        try:
            timestamp = float(text)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(_CST).isoformat(
                timespec="seconds"
            )
        except (OverflowError, OSError, ValueError):
            return None
    for format_string in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, format_string).replace(tzinfo=_CST).isoformat(
                timespec="seconds"
            )
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CST)
    return parsed.astimezone(_CST).isoformat(timespec="seconds")


def _published_in_range(value: object, *, start: str, end: str) -> bool:
    """Keep broad-search results within the caller's inclusive date window."""

    published_at = _published_at(value)
    if published_at is None:
        return False
    published_date = published_at[:10]
    return _clean_text(start) <= published_date <= _clean_text(end)


def _collapse_articles(
    articles: Sequence[NormalizedArticle],
) -> tuple[list[NormalizedArticle], int]:
    selected: dict[str, NormalizedArticle] = {}
    collapsed = 0
    for article in articles:
        key = article.url or article.external_id
        current = selected.get(key)
        if current is None:
            selected[key] = article
            continue
        collapsed += 1
        preferred = article if len(article.content_snapshot) > len(current.content_snapshot) else current
        selected[key] = replace(
            current,
            title=preferred.title,
            published_at=current.published_at or article.published_at,
            content_snapshot=preferred.content_snapshot,
            content_hash=preferred.content_hash,
            read_count=max(current.read_count, article.read_count),
            like_count=max(current.like_count, article.like_count),
            share_count=max(current.share_count, article.share_count),
            recommend_count=max(current.recommend_count, article.recommend_count),
        )
    return list(selected.values()), collapsed


def _unique_paths(paths: Sequence[str | None]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(path for path in paths if path))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(float(str(value).strip().replace(",", ""))))
    except (TypeError, ValueError, OverflowError):
        return 0


def _optional_int(value: object) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_float(value: object) -> float | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _clean_text(value).casefold() in {"1", "true", "yes", "y"}


def _retryable_http_status(status_code: int | None) -> bool:
    return status_code in {429, 502, 503, 504}


def _redact_secret(value: object, secret: str) -> object:
    """Keep a provider echo from copying the configured key into runtime files."""
    if isinstance(value, str):
        return value.replace(secret, "[redacted]")
    if isinstance(value, Mapping):
        return {
            _redact_secret(key, secret): _redact_secret(item, secret)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_secret(item, secret) for item in value)
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    return value
