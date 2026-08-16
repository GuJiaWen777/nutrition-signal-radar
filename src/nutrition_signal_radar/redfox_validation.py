"""Controlled, non-mutating RedFox availability validation for Phase 0.

This module deliberately stops at an operator-readable quality report. It does
not add rows to the source registry, import articles into SQLite, build
signals, start research, or publish anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Protocol

from .redfox_gateway import (
    DEFAULT_REDFOX_ACCOUNT_NAMES,
    RedFoxAccountBatch,
    RedFoxAccountCandidate,
    RedFoxAccountSearch,
    normalize_account_name,
)

_CST = timezone(timedelta(hours=8), name="Asia/Shanghai")


class RedFoxValidationGateway(Protocol):
    """The small live boundary needed by the pure validation service."""

    def search_accounts(self, keyword: str, *, offset: int = 0) -> RedFoxAccountSearch: ...

    def fetch_account_batch(
        self,
        *,
        account: str,
        account_name: str,
        publish_time_start: str,
        publish_time_end: str,
        max_pages: int,
        max_detail_requests: int,
    ) -> RedFoxAccountBatch: ...


@dataclass(frozen=True)
class RedFoxAccountHealth:
    """A human decision recommendation for one requested public account."""

    requested_name: str
    status: str
    recommended_action: str
    message: str
    candidates: tuple[RedFoxAccountCandidate, ...]
    selected_account: str | None
    selected_account_name: str | None
    verify_info: str | None
    article_count: int
    latest_published_at: str | None
    articles_with_full_body: int
    body_coverage: float
    detail_eligible_articles: int
    detail_success_rate: float | None
    detail_budget_exhausted: bool
    duplicate_count: int
    duplicate_rate: float
    total_reported_by_provider: int | None
    pages_fetched: int
    pagination_truncated: bool
    detail_requests: int
    detail_successes: int
    detail_errors: tuple[str, ...]
    raw_snapshot_paths: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "requested_name": self.requested_name,
            "status": self.status,
            "recommended_action": self.recommended_action,
            "message": self.message,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "selected_account": self.selected_account,
            "selected_account_name": self.selected_account_name,
            "verify_info": self.verify_info,
            "article_count": self.article_count,
            "latest_published_at": self.latest_published_at,
            "articles_with_full_body": self.articles_with_full_body,
            "body_coverage": self.body_coverage,
            "detail_eligible_articles": self.detail_eligible_articles,
            "detail_success_rate": self.detail_success_rate,
            "detail_budget_exhausted": self.detail_budget_exhausted,
            "duplicate_count": self.duplicate_count,
            "duplicate_rate": self.duplicate_rate,
            "total_reported_by_provider": self.total_reported_by_provider,
            "pages_fetched": self.pages_fetched,
            "pagination_truncated": self.pagination_truncated,
            "detail_requests": self.detail_requests,
            "detail_successes": self.detail_successes,
            "detail_errors": list(self.detail_errors),
            "raw_snapshot_paths": list(self.raw_snapshot_paths),
        }


@dataclass(frozen=True)
class RedFoxValidationReport:
    """A read-only validation report, suitable for a later human decision."""

    generated_at: str
    publish_time_start: str
    publish_time_end: str
    requested_accounts: tuple[str, ...]
    max_pages_per_account: int
    max_detail_requests_total: int
    min_detail_success_rate: float
    accounts: tuple[RedFoxAccountHealth, ...]

    @property
    def ready_for_next_phase(self) -> bool:
        return bool(self.accounts) and all(
            account.recommended_action == "enable" for account in self.accounts
        )

    @property
    def enable_count(self) -> int:
        return sum(account.recommended_action == "enable" for account in self.accounts)

    def as_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "publish_time_start": self.publish_time_start,
            "publish_time_end": self.publish_time_end,
            "requested_accounts": list(self.requested_accounts),
            "max_pages_per_account": self.max_pages_per_account,
            "max_detail_requests_total": self.max_detail_requests_total,
            "min_detail_success_rate": self.min_detail_success_rate,
            "enable_count": self.enable_count,
            "ready_for_next_phase": self.ready_for_next_phase,
            "accounts": [account.as_dict() for account in self.accounts],
        }


@dataclass(frozen=True)
class RedFoxValidationArtifacts:
    json_path: Path
    markdown_path: Path

    def as_dict(self) -> dict[str, str]:
        return {"json_path": str(self.json_path), "markdown_path": str(self.markdown_path)}


@dataclass(frozen=True)
class _SearchState:
    requested_name: str
    candidates: tuple[RedFoxAccountCandidate, ...]
    selected: RedFoxAccountCandidate | None
    status: str | None
    recommended_action: str | None
    message: str | None
    raw_snapshot_path: str | None


def validate_redfox_accounts(
    gateway: RedFoxValidationGateway,
    *,
    accounts: Iterable[str] = DEFAULT_REDFOX_ACCOUNT_NAMES,
    lookback_days: int = 30,
    as_of: date | None = None,
    max_pages: int = 3,
    max_detail_requests: int = 30,
    min_detail_success_rate: float = 0.7,
) -> RedFoxValidationReport:
    """Probe confirmed names in a bounded window without enabling a source.

    Exact-name matching is the only automatic resolution permitted here. A
    fuzzy or multiple exact search result becomes a ``correct_name`` human
    action and is never fetched as if it were the requested account.
    """

    requested_accounts = _normalize_requested_accounts(accounts)
    _validate_limits(
        lookback_days=lookback_days,
        max_pages=max_pages,
        max_detail_requests=max_detail_requests,
        min_detail_success_rate=min_detail_success_rate,
    )
    end_date = as_of or datetime.now(_CST).date()
    start_date = end_date - timedelta(days=lookback_days - 1)
    publish_time_start = start_date.isoformat()
    publish_time_end = end_date.isoformat()

    states = [_search_state_for_account(gateway, account) for account in requested_accounts]
    remaining_details = max_detail_requests
    remaining_confirmed = sum(state.selected is not None for state in states)
    health_rows: list[RedFoxAccountHealth] = []

    for state in states:
        if state.selected is None:
            health_rows.append(_unresolved_health(state))
            continue

        detail_allowance = _fair_detail_allowance(remaining_details, remaining_confirmed)
        remaining_confirmed -= 1
        try:
            batch = gateway.fetch_account_batch(
                account=state.selected.account,
                account_name=state.selected.account_name,
                publish_time_start=publish_time_start,
                publish_time_end=publish_time_end,
                max_pages=max_pages,
                max_detail_requests=detail_allowance,
            )
        except Exception as exc:  # One provider failure must not hide other account conclusions.
            health_rows.append(
                _error_health(
                    state,
                    message=f"RedFox 拉取失败：{_safe_exception_summary(exc)}",
                )
            )
            continue

        remaining_details = max(0, remaining_details - batch.detail_requests)
        health_rows.append(
            _batch_health(
                state,
                batch,
                min_detail_success_rate=min_detail_success_rate,
            )
        )

    return RedFoxValidationReport(
        generated_at=datetime.now(_CST).isoformat(timespec="seconds"),
        publish_time_start=publish_time_start,
        publish_time_end=publish_time_end,
        requested_accounts=requested_accounts,
        max_pages_per_account=max_pages,
        max_detail_requests_total=max_detail_requests,
        min_detail_success_rate=min_detail_success_rate,
        accounts=tuple(health_rows),
    )


def write_redfox_validation_report(
    report: RedFoxValidationReport,
    exports_directory: Path,
) -> RedFoxValidationArtifacts:
    """Atomically replace the latest report pair under the local runtime tree."""

    directory = Path(exports_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "redfox_validation.json"
    markdown_path = directory / "redfox_validation.md"
    _write_text_atomically(
        json_path,
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n",
    )
    _write_text_atomically(markdown_path, render_redfox_validation_markdown(report))
    return RedFoxValidationArtifacts(json_path=json_path, markdown_path=markdown_path)


def render_redfox_validation_markdown(report: RedFoxValidationReport) -> str:
    """Render concise operator-facing evidence without article bodies or secrets."""

    lines = [
        "# RedFox 六账号可用性验证",
        "",
        f"生成时间：{report.generated_at}",
        f"回溯窗口：{report.publish_time_start} 至 {report.publish_time_end}（含首尾）",
        f"上限：每账号 {report.max_pages_per_account} 页；全程最多 {report.max_detail_requests_total} 次正文请求。",
        f"正文抽样成功率门槛：{report.min_detail_success_rate:.0%}。",
        "",
        "此报告只做只读验证：不会写入来源配置、SQLite、选题、研究或发布流程。",
        "",
        "| 请求账号 | 状态 | 建议 | 文章 | 最近文章 | 正文抽样 | 当前覆盖 | 重复率 | 说明 |",
        "| --- | --- | --- | ---: | --- | --- | ---: | ---: | --- |",
    ]
    for account in report.accounts:
        lines.append(
            "| "
            f"{_markdown_cell(account.requested_name)} | "
            f"{_markdown_cell(account.status)} | "
            f"{_markdown_cell(_action_label(account.recommended_action))} | "
            f"{account.article_count} | "
            f"{_markdown_cell(account.latest_published_at or '—')} | "
            f"{_format_detail_sample(account)} | "
            f"{account.body_coverage:.0%} | "
            f"{account.duplicate_rate:.0%} | "
            f"{_markdown_cell(account.message)} |"
        )

    lines.extend(("", "## 账号细节", ""))
    for account in report.accounts:
        lines.extend(
            (
                f"### {_markdown_cell(account.requested_name)}",
                "",
                f"- RedFox 精确账号：{_markdown_cell(account.selected_account or '未选择')}",
                f"- RedFox 展示名：{_markdown_cell(account.selected_account_name or '未选择')}",
                f"- 认证信息：{_markdown_cell(account.verify_info or '未提供')}",
                f"- 分页：{_pagination_summary(account)}",
                f"- 正文抽样：{_format_detail_sample(account)}；当前完整正文覆盖 {account.body_coverage:.0%}。",
            )
        )
        if _article_list_was_attempted(account):
            lines.append(
                f"- 缺失正文候选：{account.detail_eligible_articles} 篇；"
                f"{'达到本账号抽样预算' if account.detail_budget_exhausted else '没有因抽样预算跳过正文请求'}。"
            )
        if account.candidates:
            candidate_text = "；".join(
                f"{candidate.account_name or '未命名'} ({candidate.account or '无 account'})"
                for candidate in account.candidates
            )
            lines.append(f"- 搜索候选：{_markdown_cell(candidate_text)}。")
        if account.detail_errors:
            errors = _markdown_cell("；".join(account.detail_errors[:3]))
            suffix = "（其余已省略）" if len(account.detail_errors) > 3 else ""
            lines.append(f"- 正文错误摘要：{errors}{suffix}")
        lines.append("")

    decision = "可以进入下一阶段的人审" if report.ready_for_next_phase else "尚不满足下一阶段条件"
    lines.extend(
        (
            "## 总结",
            "",
            f"建议启用：{report.enable_count}/{len(report.accounts)}。结论：{decision}。",
            "“启用”仅是建议；仍需操作者人工确认账号名称和数据质量后，再配置正式来源。",
            "",
        )
    )
    return "\n".join(lines)


def _search_state_for_account(
    gateway: RedFoxValidationGateway,
    requested_name: str,
) -> _SearchState:
    try:
        search = gateway.search_accounts(requested_name)
    except Exception as exc:
        return _SearchState(
            requested_name=requested_name,
            candidates=(),
            selected=None,
            status="search_error",
            recommended_action="pause",
            message=f"RedFox 搜索失败：{_safe_exception_summary(exc)}",
            raw_snapshot_path=None,
        )

    candidates = search.candidates
    exact_by_account = {
        candidate.account: candidate
        for candidate in candidates
        if candidate.account
        and candidate.account_name
        and normalize_account_name(candidate.account_name)
        == normalize_account_name(requested_name)
    }
    exact_matches = tuple(exact_by_account.values())
    if len(exact_matches) == 1:
        return _SearchState(
            requested_name=requested_name,
            candidates=candidates,
            selected=exact_matches[0],
            status=None,
            recommended_action=None,
            message=None,
            raw_snapshot_path=search.raw_snapshot_path,
        )
    if not candidates:
        return _SearchState(
            requested_name=requested_name,
            candidates=(),
            selected=None,
            status="not_found",
            recommended_action="pause",
            message="未找到候选账号；请人工确认名称后再试。",
            raw_snapshot_path=search.raw_snapshot_path,
        )
    if exact_matches:
        message = "存在多个同名 RedFox 账号，不能自动选择；请人工确认 account 标识。"
    else:
        message = "仅得到非精确名称候选，不能自动选择；请修正名称或人工确认 account 标识。"
    return _SearchState(
        requested_name=requested_name,
        candidates=candidates,
        selected=None,
        status="needs_confirmation",
        recommended_action="correct_name",
        message=message,
        raw_snapshot_path=search.raw_snapshot_path,
    )


def _unresolved_health(state: _SearchState) -> RedFoxAccountHealth:
    return RedFoxAccountHealth(
        requested_name=state.requested_name,
        status=state.status or "needs_confirmation",
        recommended_action=state.recommended_action or "correct_name",
        message=state.message or "账号尚未确认。",
        candidates=state.candidates,
        selected_account=None,
        selected_account_name=None,
        verify_info=None,
        article_count=0,
        latest_published_at=None,
        articles_with_full_body=0,
        body_coverage=0.0,
        detail_eligible_articles=0,
        detail_success_rate=None,
        detail_budget_exhausted=False,
        duplicate_count=0,
        duplicate_rate=0.0,
        total_reported_by_provider=None,
        pages_fetched=0,
        pagination_truncated=False,
        detail_requests=0,
        detail_successes=0,
        detail_errors=(),
        raw_snapshot_paths=_unique_paths((state.raw_snapshot_path,)),
    )


def _error_health(state: _SearchState, *, message: str) -> RedFoxAccountHealth:
    selected = state.selected
    return RedFoxAccountHealth(
        requested_name=state.requested_name,
        status="fetch_error",
        recommended_action="pause",
        message=message,
        candidates=state.candidates,
        selected_account=selected.account if selected else None,
        selected_account_name=selected.account_name if selected else None,
        verify_info=selected.verify_info if selected else None,
        article_count=0,
        latest_published_at=None,
        articles_with_full_body=0,
        body_coverage=0.0,
        detail_eligible_articles=0,
        detail_success_rate=None,
        detail_budget_exhausted=False,
        duplicate_count=0,
        duplicate_rate=0.0,
        total_reported_by_provider=None,
        pages_fetched=0,
        pagination_truncated=False,
        detail_requests=0,
        detail_successes=0,
        detail_errors=(),
        raw_snapshot_paths=_unique_paths((state.raw_snapshot_path,)),
    )


def _batch_health(
    state: _SearchState,
    batch: RedFoxAccountBatch,
    *,
    min_detail_success_rate: float,
) -> RedFoxAccountHealth:
    selected = state.selected
    assert selected is not None
    articles = batch.batch.articles
    article_count = len(articles)
    body_coverage = (
        round(batch.articles_with_full_body / article_count, 4) if article_count else 0.0
    )
    detail_success_rate = (
        round(batch.detail_successes / batch.detail_requests, 4)
        if batch.detail_requests
        else None
    )
    duplicate_count = batch.batch.collapsed_rows
    duplicate_rate = (
        round(duplicate_count / batch.batch.source_rows, 4) if batch.batch.source_rows else 0.0
    )
    latest = max((article.published_at for article in articles if article.published_at), default=None)

    if batch.pagination_truncated:
        status = "incomplete"
        action = "pause"
        message = "已触及分页上限，30 天窗口并未完整读取；请提高 --max-pages 后重新验证。"
    elif article_count == 0:
        status = "no_articles"
        action = "pause"
        message = "账号已精确匹配，但在回溯窗口内没有可用文章。"
    elif batch.detail_eligible_articles and detail_success_rate is None:
        status = "body_not_sampled"
        action = "pause"
        message = "列表未提供完整正文，且本次没有正文抽样；无法判断正文接口是否可用。"
    elif (
        detail_success_rate is not None
        and detail_success_rate < min_detail_success_rate
    ):
        status = "body_insufficient"
        action = "pause"
        message = (
            f"正文抽样成功率 {detail_success_rate:.0%} 低于 "
            f"{min_detail_success_rate:.0%} 门槛；"
            "不建议用作主题聚类来源。"
        )
    else:
        status = "matched"
        action = "enable"
        if batch.detail_budget_exhausted:
            message = (
                "名称精确匹配，窗口完整，正文抽样成功；当前正文覆盖受抽样预算限制。"
                "等待人工确认后才可启用。"
            )
        else:
            message = "名称精确匹配，窗口完整，正文可用；等待人工确认后才可启用。"

    return RedFoxAccountHealth(
        requested_name=state.requested_name,
        status=status,
        recommended_action=action,
        message=message,
        candidates=state.candidates,
        selected_account=selected.account,
        selected_account_name=selected.account_name,
        verify_info=selected.verify_info or None,
        article_count=article_count,
        latest_published_at=latest,
        articles_with_full_body=batch.articles_with_full_body,
        body_coverage=body_coverage,
        detail_eligible_articles=batch.detail_eligible_articles,
        detail_success_rate=detail_success_rate,
        detail_budget_exhausted=batch.detail_budget_exhausted,
        duplicate_count=duplicate_count,
        duplicate_rate=duplicate_rate,
        total_reported_by_provider=batch.total,
        pages_fetched=batch.pages_fetched,
        pagination_truncated=batch.pagination_truncated,
        detail_requests=batch.detail_requests,
        detail_successes=batch.detail_successes,
        detail_errors=batch.detail_errors,
        raw_snapshot_paths=_unique_paths((state.raw_snapshot_path, *batch.raw_snapshot_paths)),
    )


def _normalize_requested_accounts(accounts: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in accounts:
        account = str(value).strip()
        comparison = normalize_account_name(account)
        if not comparison or comparison in seen:
            continue
        seen.add(comparison)
        normalized.append(account)
    if not normalized:
        raise ValueError("At least one RedFox account name is required")
    return tuple(normalized)


def _validate_limits(
    *,
    lookback_days: int,
    max_pages: int,
    max_detail_requests: int,
    min_detail_success_rate: float,
) -> None:
    if lookback_days < 1:
        raise ValueError("lookback_days must be at least 1")
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    if max_detail_requests < 0:
        raise ValueError("max_detail_requests cannot be negative")
    if not 0 < min_detail_success_rate <= 1:
        raise ValueError("min_detail_success_rate must be greater than 0 and at most 1")


def _fair_detail_allowance(remaining: int, remaining_accounts: int) -> int:
    if remaining <= 0 or remaining_accounts <= 0:
        return 0
    return remaining // remaining_accounts + int(remaining % remaining_accounts > 0)


def _safe_exception_summary(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    return f"{type(exc).__name__}: {text}"[:500]


def _unique_paths(paths: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(path for path in paths if path))


def _write_text_atomically(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _markdown_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _action_label(action: str) -> str:
    return {"enable": "启用", "correct_name": "修正名称", "pause": "暂停"}.get(action, action)


def _format_detail_sample(account: RedFoxAccountHealth) -> str:
    if not _article_list_was_attempted(account):
        return "未执行"
    if account.detail_success_rate is None:
        return "无需请求" if account.detail_eligible_articles == 0 else "未抽样"
    return (
        f"{account.detail_successes}/{account.detail_requests} "
        f"({account.detail_success_rate:.0%})"
    )


def _article_list_was_attempted(account: RedFoxAccountHealth) -> bool:
    return bool(
        account.pages_fetched
        or account.article_count
        or account.total_reported_by_provider is not None
    )


def _pagination_summary(account: RedFoxAccountHealth) -> str:
    if not _article_list_was_attempted(account):
        return "未执行文章列表请求。"
    if account.pagination_truncated:
        return f"{account.pages_fetched} 页；已触及页数上限。"
    return f"{account.pages_fetched} 页；完整读取到当前窗口结束。"
