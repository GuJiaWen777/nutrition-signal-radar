from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openpyxl import load_workbook
from openpyxl.utils.datetime import CALENDAR_WINDOWS_1900, from_excel

from .article_contract import (
    ArticleBatch,
    NormalizedArticle,
    RowIssue,
    canonicalize_article_url,
    external_id_for_article,
)


DEFAULT_SHEET_NAME = "数据列表"
REQUIRED_HEADERS = ("标题", "公众号", "日期", "链接", "文章内容")


class XlsxImportError(ValueError):
    """Raised when an input workbook cannot satisfy the demo import contract."""


def load_xlsx_batch(
    path: Path,
    *,
    sheet_name: str = DEFAULT_SHEET_NAME,
    timezone_name: str = "Asia/Shanghai",
) -> ArticleBatch:
    """Read a workbook without modifying it and return source-neutral articles."""
    source_path = path.expanduser().resolve()
    if not source_path.is_file():
        raise XlsxImportError(f"Excel input does not exist: {source_path}")
    if source_path.suffix.lower() != ".xlsx":
        raise XlsxImportError(f"Excel input must be an .xlsx file: {source_path}")

    fingerprint = _sha256_file(source_path)
    workbook = load_workbook(filename=source_path, read_only=True, data_only=True)
    try:
        if sheet_name not in workbook.sheetnames:
            available = ", ".join(workbook.sheetnames)
            raise XlsxImportError(
                f"Worksheet {sheet_name!r} was not found; available worksheets: {available}"
            )
        worksheet = workbook[sheet_name]
        row_iterator = worksheet.iter_rows(values_only=True)
        try:
            headers = next(row_iterator)
        except StopIteration as exc:
            raise XlsxImportError(f"Worksheet {sheet_name!r} is empty") from exc

        numbered_rows = enumerate(row_iterator, start=2)
        return normalize_xlsx_rows(
            headers,
            numbered_rows,
            source_path=source_path,
            source_fingerprint=fingerprint,
            sheet_name=sheet_name,
            timezone_name=timezone_name,
            workbook_epoch=workbook.epoch,
        )
    finally:
        workbook.close()


def normalize_xlsx_rows(
    headers: Sequence[Any],
    numbered_rows: Iterable[tuple[int, Sequence[Any]]],
    *,
    source_path: Path,
    source_fingerprint: str,
    sheet_name: str = DEFAULT_SHEET_NAME,
    timezone_name: str = "Asia/Shanghai",
    workbook_epoch: datetime = CALENDAR_WINDOWS_1900,
) -> ArticleBatch:
    """Normalize P&B-style rows; exposed separately for deterministic unit tests."""
    header_names = tuple("" if value is None else str(value).strip() for value in headers)
    missing_headers = [name for name in REQUIRED_HEADERS if name not in header_names]
    if missing_headers:
        raise XlsxImportError(f"Missing required columns: {', '.join(missing_headers)}")
    if len(set(name for name in header_names if name)) != len([name for name in header_names if name]):
        raise XlsxImportError("Workbook contains duplicate non-empty column names")

    positions = {name: index for index, name in enumerate(header_names) if name}
    source_timezone = _load_timezone(timezone_name)
    articles_by_id: dict[str, NormalizedArticle] = {}
    issues: list[RowIssue] = []
    source_rows = 0
    collapsed_rows = 0

    for row_number, values in numbered_rows:
        if not any(not _is_blank(value) for value in values):
            continue
        source_rows += 1
        row = {name: values[index] if index < len(values) else None for name, index in positions.items()}
        title = _text(row.get("标题"))
        account_name = _text(row.get("公众号"))
        content = _text(row.get("文章内容"))
        original_url = _text(row.get("链接"))

        if not title:
            issues.append(RowIssue(row_number, "missing title"))
            continue
        if not account_name:
            issues.append(RowIssue(row_number, "missing account"))
            continue
        if not original_url and not content:
            issues.append(RowIssue(row_number, "both URL and content are missing"))
            continue

        try:
            published_at = _normalize_datetime(
                row.get("日期"), timezone=source_timezone, workbook_epoch=workbook_epoch
            )
        except ValueError as exc:
            issues.append(RowIssue(row_number, f"invalid publication date: {exc}"))
            continue

        canonical_url = canonicalize_article_url(original_url)
        content_hash = _sha256_text(content) if content else None
        external_id = external_id_for_article(
            url=canonical_url,
            account_name=account_name,
            title=title,
            published_at=published_at,
            content_hash=content_hash,
        )
        metadata = {
            "source_kind": "xlsx",
            "source_sheet": sheet_name,
            "source_fingerprint": source_fingerprint,
            "source_rows": [row_number],
            "original_urls": [original_url] if original_url else [],
            "category": _text(row.get("类别")),
            "media_name": _text(row.get("媒体名称")),
            "author": _text(row.get("作者")),
            "topic": _text(row.get("话题")),
            "classifications": [
                value
                for value in (
                    _text(row.get("一级分类")),
                    _text(row.get("二级分类")),
                    _text(row.get("三级分类")),
                )
                if value
            ],
        }
        article = NormalizedArticle(
            external_id=external_id,
            account_name=account_name,
            title=title,
            published_at=published_at,
            url=canonical_url,
            content_snapshot=content,
            content_hash=content_hash,
            read_count=_nonnegative_int(row.get("阅读量")),
            like_count=_nonnegative_int(row.get("点赞量")),
            share_count=_nonnegative_int(row.get("分享")),
            recommend_count=_nonnegative_int(row.get("推荐")),
            metadata=metadata,
            source_reference=f"{source_path}#{sheet_name}!{row_number}",
        )

        existing = articles_by_id.get(external_id)
        if existing is None:
            articles_by_id[external_id] = article
        else:
            articles_by_id[external_id] = _merge_duplicate(existing, article, source_path, sheet_name)
            collapsed_rows += 1

    return ArticleBatch(
        source_kind="xlsx",
        source_path=str(source_path),
        source_fingerprint=source_fingerprint,
        source_rows=source_rows,
        articles=tuple(articles_by_id.values()),
        skipped_rows=len(issues),
        collapsed_rows=collapsed_rows,
        issues=tuple(issues),
        account_segment="demo_pb",
    )


def _merge_duplicate(
    left: NormalizedArticle,
    right: NormalizedArticle,
    source_path: Path,
    sheet_name: str,
) -> NormalizedArticle:
    metadata = dict(left.metadata)
    metadata["source_rows"] = sorted(
        set(left.metadata.get("source_rows", [])) | set(right.metadata.get("source_rows", []))
    )
    metadata["original_urls"] = list(
        dict.fromkeys(left.metadata.get("original_urls", []) + right.metadata.get("original_urls", []))
    )
    metadata["duplicate_rows_collapsed"] = len(metadata["source_rows"]) - 1
    if left.account_name != right.account_name or left.title != right.title:
        metadata["identity_conflict"] = True

    content_article = right if len(right.content_snapshot) > len(left.content_snapshot) else left
    source_rows = ",".join(str(value) for value in metadata["source_rows"])
    return replace(
        left,
        published_at=min(
            value for value in (left.published_at, right.published_at) if value is not None
        ) if left.published_at or right.published_at else None,
        content_snapshot=content_article.content_snapshot,
        content_hash=content_article.content_hash,
        read_count=max(left.read_count, right.read_count),
        like_count=max(left.like_count, right.like_count),
        share_count=max(left.share_count, right.share_count),
        recommend_count=max(left.recommend_count, right.recommend_count),
        metadata=metadata,
        source_reference=f"{source_path}#{sheet_name}!{source_rows}",
    )


def _normalize_datetime(value: Any, *, timezone: tzinfo, workbook_epoch: datetime) -> str | None:
    if _is_blank(value):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, (int, float)):
        parsed = from_excel(value, epoch=workbook_epoch)
    elif isinstance(value, str):
        text = value.strip()
        parsed = None
        for date_format in (
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d",
        ):
            try:
                parsed = datetime.strptime(text, date_format)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError as exc:
                raise ValueError(text) from exc
    else:
        raise ValueError(str(value))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.isoformat(timespec="seconds")


def _load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        raise XlsxImportError(f"Unknown timezone: {name}")


def _nonnegative_int(value: Any) -> int:
    if _is_blank(value):
        return 0
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("+", "")
        multiplier = 10_000 if text.endswith("万") else 1
        if multiplier != 1:
            text = text[:-1]
        try:
            return max(0, int(float(text) * multiplier))
        except ValueError:
            return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _text(value: Any) -> str:
    return "" if _is_blank(value) else str(value).strip()


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
