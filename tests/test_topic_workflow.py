import json
from pathlib import Path

import pytest

from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.demo_pipeline import build_demo_topics
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.topic_workflow import (
    TopicTransitionError,
    TopicWorkflowError,
    set_candidate_review_track,
    transition_candidate,
    update_candidate_editorial_fields,
    update_topic_package,
)


def _prepared_database(tmp_path: Path) -> tuple[Path, int]:
    path = tmp_path / "var" / "demo" / "radar.sqlite3"
    articles = tuple(
        NormalizedArticle(
            external_id=f"article-{index}",
            account_name=f"公众号{index}",
            title="同一底层事件获得多号关注",
            published_at=f"2026-03-2{index}T09:00:00+08:00",
            url=f"https://example.com/{index}",
            content_snapshot="相同稿件",
            content_hash="same-content",
            read_count=100 * index,
        )
        for index in (1, 2)
    )
    import_article_batch(
        path,
        ArticleBatch(
            source_kind="xlsx",
            source_path="D:/source.xlsx",
            source_fingerprint="fingerprint",
            source_rows=2,
            articles=articles,
        ),
    )
    build_demo_topics(path)
    connection = connect_database(path, readonly=True)
    try:
        candidate_id = int(connection.execute("SELECT id FROM topic_candidates").fetchone()[0])
    finally:
        connection.close()
    return path, candidate_id


def test_human_gate_creates_and_confirms_topic_package(tmp_path: Path) -> None:
    path, candidate_id = _prepared_database(tmp_path)

    first = transition_candidate(path, candidate_id, "pending_decision")
    second = transition_candidate(path, candidate_id, "package_drafting")
    third = transition_candidate(path, candidate_id, "pending_confirmation")
    fourth = transition_candidate(path, candidate_id, "selected")

    assert first.package_id is None
    assert second.package_status == "drafting"
    assert third.package_status == "pending_confirmation"
    assert fourth.package_status == "confirmed"

    connection = connect_database(path, readonly=True)
    try:
        assert connection.execute(
            "SELECT status FROM topic_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()[0] == "selected"
        package = connection.execute(
            """
            SELECT status, submitted_at, confirmed_at, evidence_status,
                   audience, core_question
            FROM topic_packages WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
        assert package["status"] == "confirmed"
        assert package["submitted_at"] is not None
        assert package["confirmed_at"] is not None
        assert package["evidence_status"] == "needs_evidence"
        assert package["audience"]
        assert package["core_question"]
        assert connection.execute(
            "SELECT COUNT(*) FROM topic_events WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()[0] == 4
    finally:
        connection.close()


def test_topic_package_edit_is_traced_and_locked_after_submission(tmp_path: Path) -> None:
    path, candidate_id = _prepared_database(tmp_path)
    transition_candidate(path, candidate_id, "pending_decision")
    transition_candidate(path, candidate_id, "package_drafting")

    edit = update_topic_package(
        path,
        candidate_id,
        title="人工确认前的最终选题",
        audience="品牌方与企业决策者",
        core_question="这次行业变化会怎样影响产品立项？",
        why_now="多个发现源在近期集中讨论。",
        industry_angle="区分传播增量、事实增量与决策影响。",
        selection_dimensions=["监管与合规", "业务含义", "业务含义"],
        evidence_gaps="需要补充监管原文和企业公告。",
        research_directions="核实时间线、受影响企业和行动窗口。",
        risk_notes="公众号仅作发现源，事实尚待补证。",
    )
    submitted = transition_candidate(path, candidate_id, "pending_confirmation")

    assert edit.missing_fields == ()
    assert "title" in edit.changed_fields
    assert submitted.package_status == "pending_confirmation"
    with pytest.raises(TopicWorkflowError, match="Only a topic package"):
        update_topic_package(
            path,
            candidate_id,
            title="提交后直接改动",
            audience="品牌方",
            core_question="问题",
            why_now="现在",
            industry_angle="角度",
            selection_dimensions=["业务含义"],
            evidence_gaps="缺口",
            research_directions="方向",
            risk_notes="风险",
        )
    with pytest.raises(TopicWorkflowError, match="请先退回"):
        update_candidate_editorial_fields(
            path,
            candidate_id,
            proposed_title="不应直接修改",
            audience="品牌方",
            core_question="问题",
            why_now="现在",
            angle="角度",
        )

    connection = connect_database(path, readonly=True)
    try:
        package = connection.execute(
            "SELECT * FROM topic_packages WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        assert package["title"] == "人工确认前的最终选题"
        assert package["status"] == "pending_confirmation"
        assert package["submitted_at"] is not None
        assert json.loads(package["selection_dimensions_json"]) == ["监管与合规", "业务含义"]
        assert connection.execute(
            """
            SELECT COUNT(*) FROM topic_events
            WHERE candidate_id = ? AND event_type = 'topic_package_edited'
            """,
            (candidate_id,),
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_incomplete_topic_package_cannot_be_submitted(tmp_path: Path) -> None:
    path, candidate_id = _prepared_database(tmp_path)
    transition_candidate(path, candidate_id, "pending_decision")
    transition_candidate(path, candidate_id, "package_drafting")

    edit = update_topic_package(
        path,
        candidate_id,
        title="仍需完善的选题",
        audience="企业决策者",
        core_question="核心问题",
        why_now="时效理由",
        industry_angle="差异化角度",
        selection_dimensions=["业务含义"],
        evidence_gaps="待补证事实",
        research_directions="研究方向",
        risk_notes="",
    )

    assert edit.missing_fields == ("risk_notes",)
    with pytest.raises(TopicTransitionError, match="风险提示"):
        transition_candidate(path, candidate_id, "pending_confirmation")

    connection = connect_database(path, readonly=True)
    try:
        candidate_status = connection.execute(
            "SELECT status FROM topic_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()[0]
        package = connection.execute(
            "SELECT status, submitted_at FROM topic_packages WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        assert candidate_status == "package_drafting"
        assert package["status"] == "drafting"
        assert package["submitted_at"] is None
    finally:
        connection.close()


def test_transition_cannot_skip_human_gates_and_archive_needs_reason(tmp_path: Path) -> None:
    path, candidate_id = _prepared_database(tmp_path)

    with pytest.raises(TopicTransitionError, match="not allowed"):
        transition_candidate(path, candidate_id, "selected")
    with pytest.raises(TopicTransitionError, match="requires a reason"):
        transition_candidate(path, candidate_id, "archived")

    result = transition_candidate(path, candidate_id, "archived", reason="证据较弱")
    assert result.to_status == "archived"


def test_manual_track_and_editorial_changes_survive_demo_rebuild(tmp_path: Path) -> None:
    path, candidate_id = _prepared_database(tmp_path)

    track = set_candidate_review_track(
        path,
        candidate_id,
        "research",
        reason="人工判断有明确决策价值",
    )
    edit = update_candidate_editorial_fields(
        path,
        candidate_id,
        proposed_title="人工编辑后的行业观察题目",
        audience="品牌方决策者",
        core_question="这次传播是否带来新的行业事实？",
        why_now="多号同步传播，需要先核对共同稿源。",
        angle="区分事实增量与传播增量。",
    )
    build_demo_topics(path)

    assert track.to_track == "research"
    assert "proposed_title" in edit.changed_fields
    connection = connect_database(path, readonly=True)
    try:
        candidate = connection.execute(
            """
            SELECT proposed_title, review_track, track_source,
                   track_reason, editor_modified
            FROM topic_candidates WHERE id = ?
            """,
            (candidate_id,),
        ).fetchone()
        assert candidate["proposed_title"] == "人工编辑后的行业观察题目"
        assert candidate["review_track"] == "research"
        assert candidate["track_source"] == "manual"
        assert candidate["track_reason"] == "人工判断有明确决策价值"
        assert candidate["editor_modified"] == 1
        event_types = {
            row["event_type"]
            for row in connection.execute(
                "SELECT event_type FROM topic_events WHERE candidate_id = ?",
                (candidate_id,),
            )
        }
        assert {"review_track_changed", "candidate_edited"} <= event_types
    finally:
        connection.close()
