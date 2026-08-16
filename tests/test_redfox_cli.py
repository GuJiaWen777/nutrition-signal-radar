import json
from pathlib import Path

from nutrition_signal_radar.cli import main
from nutrition_signal_radar.redfox_validation import (
    RedFoxAccountHealth,
    RedFoxValidationReport,
)
from nutrition_signal_radar.redfox_gateway import RedFoxListedWork, RedFoxWideSearchPage


def test_redfox_cli_requires_explicit_live_acknowledgement(tmp_path: Path, capsys) -> None:
    result = main(["--project-root", str(tmp_path), "redfox", "validate", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 2
    assert payload["status"] == "protected"
    assert "--live" in payload["message"]
    assert not (tmp_path / "var").exists()


def test_redfox_broad_search_requires_explicit_live_acknowledgement(
    tmp_path: Path, capsys
) -> None:
    result = main(
        [
            "--project-root",
            str(tmp_path),
            "redfox",
            "broad-search",
            "--keyword",
            "Bonny营养领航",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 2
    assert payload["status"] == "protected"
    assert "--live" in payload["message"]
    assert not (tmp_path / "var").exists()


def test_redfox_cli_does_not_attempt_live_calls_without_a_project_key(
    tmp_path: Path,
    capsys,
) -> None:
    result = main(
        ["--project-root", str(tmp_path), "redfox", "validate", "--live", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 1
    assert payload == {
        "status": "not_configured",
        "message": "REDFOX_API_KEY is not configured for this project root.",
    }
    assert not (tmp_path / "var").exists()


def test_redfox_cli_returns_success_when_validation_report_recommends_pause(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    secret = "unit-test-redfox-key"
    (tmp_path / ".env").write_text(f"REDFOX_API_KEY={secret}\n", encoding="utf-8")
    report = RedFoxValidationReport(
        generated_at="2026-08-14T18:00:00+08:00",
        publish_time_start="2026-07-16",
        publish_time_end="2026-08-14",
        requested_accounts=("待确认账号",),
        max_pages_per_account=3,
        max_detail_requests_total=30,
        min_detail_success_rate=0.7,
        accounts=(
            RedFoxAccountHealth(
                requested_name="待确认账号",
                status="needs_confirmation",
                recommended_action="correct_name",
                message="需要人工确认。",
                candidates=(),
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
                raw_snapshot_paths=(),
            ),
        ),
    )

    class _Gateway:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    monkeypatch.setattr("nutrition_signal_radar.cli.RedFoxGateway", _Gateway)
    monkeypatch.setattr(
        "nutrition_signal_radar.cli.validate_redfox_accounts",
        lambda *args, **kwargs: report,
    )

    result = main(
        ["--project-root", str(tmp_path), "redfox", "validate", "--live", "--json"]
    )
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)

    assert result == 0
    assert payload["ready_for_next_phase"] is False
    assert payload["accounts"][0]["recommended_action"] == "correct_name"
    assert secret not in rendered
    assert Path(payload["artifacts"]["json_path"]).is_file()


def test_redfox_broad_search_json_survives_characters_outside_console_codepage(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    (tmp_path / ".env").write_text(
        "REDFOX_API_KEY=unit-test-redfox-key\n",
        encoding="utf-8",
    )

    class _Gateway:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def search_wide_articles(
            self,
            keyword: str,
            *,
            offset: int = 0,
            sort_type: str = "2",
        ) -> RedFoxWideSearchPage:
            return RedFoxWideSearchPage(
                keyword=keyword,
                sort_type=sort_type,
                works=(
                    RedFoxListedWork(
                        payload={
                            "title": "益生菌\u200c新规解读",
                            "author": "营养健康评论",
                            "publishTime": "2026-08-14T09:00:00+08:00",
                        },
                        raw_snapshot_path=None,
                    ),
                ),
                total=1,
                offset=offset,
                has_more=False,
                raw_snapshot_path=None,
            )

    monkeypatch.setattr("nutrition_signal_radar.cli.RedFoxGateway", _Gateway)

    result = main(
        [
            "--project-root",
            str(tmp_path),
            "redfox",
            "broad-search",
            "--keyword",
            "益生菌",
            "--live",
            "--json",
        ]
    )
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)

    assert result == 0
    assert payload["result_count"] == 1
    assert payload["results"][0]["title"] == "益生菌\u200c新规解读"
