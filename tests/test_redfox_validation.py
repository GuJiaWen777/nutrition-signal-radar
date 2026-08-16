import json
from datetime import date
from pathlib import Path

from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.redfox_gateway import (
    RedFoxAccountBatch,
    RedFoxAccountCandidate,
    RedFoxAccountSearch,
)
from nutrition_signal_radar.redfox_validation import (
    validate_redfox_accounts,
    write_redfox_validation_report,
)


def _candidate(account: str, account_name: str) -> RedFoxAccountCandidate:
    return RedFoxAccountCandidate(
        account=account,
        account_name=account_name,
        verify_info="认证",
        redfox_index=80.0,
        last_publish_time="2026-08-13T08:00:00+08:00",
        last_article_title="最近文章",
        account_type="企业",
    )


def _batch(
    account: str,
    *,
    article_count: int = 2,
    full_body_count: int = 2,
    collapsed_rows: int = 0,
    pagination_truncated: bool = False,
    detail_requests: int = 1,
    detail_successes: int | None = None,
    detail_eligible_articles: int | None = None,
) -> RedFoxAccountBatch:
    articles = tuple(
        NormalizedArticle(
            external_id=f"redfox:{account}:{index}",
            account_name=account,
            title=f"文章 {index}",
            published_at=f"2026-08-{13 - index:02d}T08:00:00+08:00",
            url=f"https://mp.weixin.qq.com/s/{account}-{index}",
            content_snapshot="完整正文" if index < full_body_count else "摘要",
            content_hash=f"hash-{account}-{index}",
            metadata={"redfox_body_state": "inline" if index < full_body_count else "summary"},
        )
        for index in range(article_count)
    )
    source_rows = article_count + collapsed_rows
    batch = ArticleBatch(
        source_kind="redfox",
        source_path="https://redfox.hk/story/api/gzhData/queryWorkList",
        source_fingerprint=f"sha256:{account}",
        source_rows=source_rows,
        articles=articles,
        collapsed_rows=collapsed_rows,
    )
    resolved_detail_successes = (
        min(detail_requests, full_body_count)
        if detail_successes is None
        else detail_successes
    )
    resolved_detail_eligible = (
        max(detail_requests, article_count - full_body_count)
        if detail_eligible_articles is None
        else detail_eligible_articles
    )
    return RedFoxAccountBatch(
        batch=batch,
        total=source_rows,
        pages_fetched=1,
        pagination_truncated=pagination_truncated,
        detail_eligible_articles=resolved_detail_eligible,
        detail_requests=detail_requests,
        detail_successes=resolved_detail_successes,
        detail_budget_exhausted=resolved_detail_eligible > detail_requests,
        detail_errors=(),
        articles_with_full_body=full_body_count,
        raw_snapshot_paths=(f"/runtime/raw/{account}.json",),
    )


class _Gateway:
    def __init__(
        self,
        searches: dict[str, tuple[RedFoxAccountCandidate, ...]],
        batches: dict[str, RedFoxAccountBatch],
    ) -> None:
        self.searches = searches
        self.batches = batches
        self.fetch_calls: list[dict[str, object]] = []

    def search_accounts(self, keyword: str, *, offset: int = 0) -> RedFoxAccountSearch:
        assert offset == 0
        return RedFoxAccountSearch(
            candidates=self.searches.get(keyword, ()),
            raw_snapshot_path=f"/runtime/raw/search-{keyword}.json",
        )

    def fetch_account_batch(self, **kwargs: object) -> RedFoxAccountBatch:
        self.fetch_calls.append(dict(kwargs))
        return self.batches[str(kwargs["account"])]


def test_validation_preserves_name_confirmation_gate_and_writes_reports(tmp_path: Path) -> None:
    exact = _candidate("wx-exact", "精确账号")
    low_body = _candidate("wx-low", "正文不足")
    gateway = _Gateway(
        {
            "精确账号": (exact,),
            "候选账号": (_candidate("wx-similar", "候选账号官方"),),
            "缺失账号": (),
            "正文不足": (low_body,),
        },
        {
            "wx-exact": _batch("wx-exact", collapsed_rows=1, detail_requests=1),
            "wx-low": _batch("wx-low", full_body_count=0, detail_requests=1),
        },
    )

    report = validate_redfox_accounts(
        gateway,
        accounts=("精确账号", "候选账号", "缺失账号", "正文不足"),
        as_of=date(2026, 8, 14),
        lookback_days=30,
        max_pages=3,
        max_detail_requests=3,
        min_detail_success_rate=0.7,
    )
    artifacts = write_redfox_validation_report(report, tmp_path / "var" / "exports")

    assert report.publish_time_start == "2026-07-16"
    assert report.publish_time_end == "2026-08-14"
    assert [account.status for account in report.accounts] == [
        "matched",
        "needs_confirmation",
        "not_found",
        "body_insufficient",
    ]
    assert [account.recommended_action for account in report.accounts] == [
        "enable",
        "correct_name",
        "pause",
        "pause",
    ]
    assert [call["account"] for call in gateway.fetch_calls] == ["wx-exact", "wx-low"]
    assert sum(batch.detail_requests for batch in gateway.batches.values()) <= 3
    assert report.accounts[0].duplicate_rate == 0.3333
    assert report.accounts[0].body_coverage == 1.0
    assert report.accounts[0].detail_success_rate == 1.0
    assert report.accounts[3].detail_success_rate == 0.0
    assert report.ready_for_next_phase is False

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")
    assert payload["accounts"][0]["recommended_action"] == "enable"
    assert "| 请求账号 | 状态 | 建议 |" in markdown
    assert "不会写入来源配置、SQLite、选题、研究或发布流程" in markdown
    assert "未执行文章列表请求。" in markdown


def test_validation_can_recommend_enable_for_one_exact_complete_account() -> None:
    candidate = _candidate("wx-ready", "可用账号")
    gateway = _Gateway(
        {"可用账号": (candidate,)},
        {"wx-ready": _batch("wx-ready", detail_requests=0)},
    )

    report = validate_redfox_accounts(
        gateway,
        accounts=("可用账号",),
        as_of=date(2026, 8, 14),
        max_detail_requests=0,
    )

    assert report.ready_for_next_phase is True
    assert report.enable_count == 1
    assert gateway.fetch_calls[0]["max_detail_requests"] == 0


def test_validation_distinguishes_successful_body_samples_from_budget_limited_coverage() -> None:
    candidate = _candidate("wx-sampled", "抽样账号")
    gateway = _Gateway(
        {"抽样账号": (candidate,)},
        {
            "wx-sampled": _batch(
                "wx-sampled",
                article_count=10,
                full_body_count=2,
                detail_requests=2,
                detail_successes=2,
                detail_eligible_articles=10,
            )
        },
    )

    report = validate_redfox_accounts(
        gateway,
        accounts=("抽样账号",),
        as_of=date(2026, 8, 14),
        min_detail_success_rate=0.7,
    )

    health = report.accounts[0]
    assert health.status == "matched"
    assert health.recommended_action == "enable"
    assert health.detail_success_rate == 1.0
    assert health.body_coverage == 0.2
    assert health.detail_budget_exhausted is True
