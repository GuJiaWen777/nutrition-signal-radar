import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.cli import main
from nutrition_signal_radar.cycle_service import MonitoringCycleError, close_monitoring_cycle
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.demo_pipeline import build_demo_topics
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.signal_generation import (
    GENERATOR,
    analyze_cycle_signals,
)


VALID_ANALYSIS = {
    "underlying_event": "市场监管部门发布益生菌保健食品监管新规，涉及注册与备案路径调整。",
    "consensus": "两家公众号一致认为监管收紧将影响备案节奏。",
    "disagreements": "一方强调注册门槛，另一方侧重备案路径。",
    "business_implications": "原料商与品牌方需重审合规表达与备案安排。",
    "data_gaps": "缺少监管原文全文与正式实施日期。",
    "affected_actors": ["品牌方", "原料商", "合规负责人"],
}


class _FakeGateway:
    """Return one content per call (exceptions allowed), repeating the last."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def analyze(self, system_prompt: str, user_prompt: str, *, json_object: bool = True):
        self.calls.append(user_prompt)
        item = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(status="completed", content=item, error="")


def _analyzed_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "var" / "radar.sqlite3"
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="redfox",
            source_path="https://redfox.hk/example",
            source_fingerprint="signal-analysis-fixture",
            source_rows=2,
            articles=(
                NormalizedArticle(
                    external_id="sa1",
                    account_name="公众号甲",
                    title="益生菌监管政策发布",
                    published_at="2026-08-14T09:30:00+08:00",
                    url="https://example.com/sa1",
                    content_snapshot="总局发布益生菌保健食品监管新规，涉及注册与备案要求。",
                    content_hash="sa1",
                ),
                NormalizedArticle(
                    external_id="sa2",
                    account_name="公众号乙",
                    title="益生菌监管政策正式发布",
                    published_at="2026-08-14T10:30:00+08:00",
                    url="https://example.com/sa2",
                    content_snapshot="市场监管部门发布益生菌监管文件，行业关注备案路径变化。",
                    content_hash="sa2",
                ),
            ),
        ),
    )
    build_demo_topics(database_path)
    return database_path


def _candidate_rows(database_path: Path) -> list[tuple[object, ...]]:
    connection = connect_database(database_path, readonly=True)
    try:
        return list(
            connection.execute(
                "SELECT id, score, status, review_track FROM topic_candidates ORDER BY id"
            )
        )
    finally:
        connection.close()


def test_signal_analysis_updates_interpretive_fields_without_touching_scores(
    tmp_path: Path,
) -> None:
    database_path = _analyzed_database(tmp_path)
    before_candidates = _candidate_rows(database_path)
    gateway = _FakeGateway([json.dumps(VALID_ANALYSIS, ensure_ascii=False)])

    result = analyze_cycle_signals(database_path, gateway, max_signals=30)

    assert result.updated == 1
    assert result.failed == 0
    assert result.generated_by == GENERATOR
    connection = connect_database(database_path, readonly=True)
    try:
        row = connection.execute("SELECT * FROM industry_signals").fetchone()
        assert row["underlying_event"] == VALID_ANALYSIS["underlying_event"]
        assert row["consensus"] == VALID_ANALYSIS["consensus"]
        assert row["disagreements"] == VALID_ANALYSIS["disagreements"]
        assert row["business_implications"] == VALID_ANALYSIS["business_implications"]
        assert row["data_gaps"] == VALID_ANALYSIS["data_gaps"]
        assert json.loads(row["affected_actors_json"]) == VALID_ANALYSIS["affected_actors"]
        assert row["generated_by"] == GENERATOR
        assert row["evidence_status"] == "needs_evidence"
        assert connection.execute(
            "SELECT COUNT(*) FROM monitoring_cycle_events "
            "WHERE event_type = 'signal_analyzed'"
        ).fetchone()[0] == 1
    finally:
        connection.close()
    assert _candidate_rows(database_path) == before_candidates
    assert len(gateway.calls) == 1
    assert "益生菌监管政策发布" in gateway.calls[0]


def test_signal_analysis_retries_invalid_output_within_bounds(tmp_path: Path) -> None:
    database_path = _analyzed_database(tmp_path)
    gateway = _FakeGateway(
        ["这不是 JSON", json.dumps(VALID_ANALYSIS, ensure_ascii=False)]
    )

    result = analyze_cycle_signals(database_path, gateway, max_signals=30)

    assert result.updated == 1
    assert result.outcomes[0].attempts == 2


def test_signal_analysis_failure_keeps_original_rule_fields(tmp_path: Path) -> None:
    database_path = _analyzed_database(tmp_path)
    connection = connect_database(database_path, readonly=True)
    try:
        before = connection.execute("SELECT * FROM industry_signals").fetchone()
        original_event = before["underlying_event"]
        original_generator = before["generated_by"]
    finally:
        connection.close()
    gateway = _FakeGateway([json.dumps({"unexpected": "shape"}, ensure_ascii=False)])

    result = analyze_cycle_signals(database_path, gateway, max_signals=30)

    assert result.updated == 0
    assert result.failed == 1
    connection = connect_database(database_path, readonly=True)
    try:
        after = connection.execute("SELECT * FROM industry_signals").fetchone()
        assert after["underlying_event"] == original_event
        assert after["generated_by"] == original_generator
        assert connection.execute(
            "SELECT COUNT(*) FROM monitoring_cycle_events "
            "WHERE event_type = 'signal_analysis_failed'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_signal_analysis_stops_at_closed_cycle(tmp_path: Path) -> None:
    database_path = _analyzed_database(tmp_path)
    connection = connect_database(database_path, readonly=True)
    try:
        cycle_id = int(
            connection.execute("SELECT id FROM monitoring_cycles").fetchone()[0]
        )
    finally:
        connection.close()
    close_monitoring_cycle(database_path, cycle_id)
    gateway = _FakeGateway([json.dumps(VALID_ANALYSIS, ensure_ascii=False)])

    with pytest.raises(MonitoringCycleError, match="已关闭"):
        analyze_cycle_signals(database_path, gateway, max_signals=30)
    assert gateway.calls == []


def test_signal_analysis_cli_requires_live_acknowledgement(tmp_path, capsys) -> None:
    result = main(["--project-root", str(tmp_path), "signals", "analyze"])

    assert result == 2
    captured = capsys.readouterr()
    assert "live" in captured.err
