import json
from pathlib import Path

import pytest

from nutrition_signal_radar.agent_gateway import AgentRunResult, AgentToolCall
from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.cycle_service import close_monitoring_cycle
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.demo_pipeline import build_demo_topics
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.research_workflow import (
    ResearchWorkflowError,
    confirm_research_task,
    create_research_task,
    finalize_research_draft,
    get_research_task,
    regenerate_offline_draft_demo,
    run_offline_draft_demo,
    run_online_research,
    save_draft_revision,
    update_research_task,
)
from nutrition_signal_radar.topic_workflow import transition_candidate


ONLINE_JSON = """
{
  "title": "新版原料目录的影响判断",
  "key_judgment": "核心判断：目录扩围会改变备案路径。",
  "article_body": "# 新版原料目录的影响判断\\n\\n面向企业决策者的正文。",
  "action_implications": "优先核对原料是否入目录，再决定备案节奏。",
  "uncertainty_notes": "部分公告仍在征求意见，时间窗口待补证。",
  "sources": [
    {
      "claim": "总局发布征求意见稿",
      "title": "关于征求新版目录意见的公告",
      "url": "https://www.samr.gov.cn/notice/1",
      "publisher": "市场监管总局",
      "source_type": "监管原文"
    },
    {
      "claim": "论文讨论益生菌安全性",
      "title": "益生菌菌株安全性研究",
      "url": "https://journal.example/paper/1",
      "publisher": "示例期刊",
      "source_type": "论文"
    }
  ]
}
"""


class _FakeOnlineRunner:
    def __init__(self, result: AgentRunResult) -> None:
        self.result = result
        self.requests: list[object] = []

    def run(self, request: object) -> AgentRunResult:
        self.requests.append(request)
        return self.result


def _successful_online_result() -> AgentRunResult:
    return AgentRunResult(
        status="completed",
        final_text=ONLINE_JSON,
        tool_calls=(
            AgentToolCall(
                tool_name="web_search",
                arguments={"query": "原料目录"},
                result='{"urls":["https://www.samr.gov.cn/notice/1"]}',
                is_error=False,
            ),
        ),
        event_lines=(),
        exit_code=0,
        stderr="",
    )


def _prepared_database(tmp_path: Path) -> tuple[Path, int, int]:
    path = tmp_path / "var" / "demo" / "radar.sqlite3"
    articles = tuple(
        NormalizedArticle(
            external_id=f"research-article-{index}",
            account_name=f"行业公众号{index}",
            title="同一行业事实获得多号关注",
            published_at=f"2026-03-2{index}T09:00:00+08:00",
            url=f"https://example.com/research/{index}",
            content_snapshot="相同发现线索",
            content_hash="research-same-content",
        )
        for index in (1, 2)
    )
    import_article_batch(
        path,
        ArticleBatch(
            source_kind="xlsx",
            source_path="D:/source.xlsx",
            source_fingerprint="research-fingerprint",
            source_rows=2,
            articles=articles,
        ),
    )
    build_demo_topics(path)
    connection = connect_database(path, readonly=True)
    try:
        candidate_id = int(
            connection.execute("SELECT id FROM topic_candidates").fetchone()[0]
        )
        cycle_id = int(
            connection.execute("SELECT id FROM monitoring_cycles").fetchone()[0]
        )
    finally:
        connection.close()
    return path, candidate_id, cycle_id


def _select_topic(path: Path, candidate_id: int) -> None:
    transition_candidate(path, candidate_id, "pending_decision")
    transition_candidate(path, candidate_id, "package_drafting")
    transition_candidate(path, candidate_id, "pending_confirmation")
    transition_candidate(path, candidate_id, "selected")


def _save_task(path: Path, task: dict[str, object], **changes: object) -> None:
    values = {
        "research_question": task["research_question"],
        "audience": task["audience"],
        "decision_use": task["decision_use"],
        "scope": task["scope"],
        "geography": task["geography"],
        "time_range": task["time_range"],
        "exclusions": task["exclusions"],
        "selection_dimensions": task["selection_dimensions"],
        "evidence_gaps": task["evidence_gaps"],
        "output_requirements": task["output_requirements"],
    }
    values.update(changes)
    update_research_task(path, int(task["id"]), **values)


def test_only_a_confirmed_selected_topic_can_create_a_research_task(
    tmp_path: Path,
) -> None:
    path, candidate_id, _ = _prepared_database(tmp_path)

    with pytest.raises(ResearchWorkflowError, match="还没有选题包"):
        create_research_task(path, candidate_id)

    transition_candidate(path, candidate_id, "pending_decision")
    transition_candidate(path, candidate_id, "package_drafting")
    transition_candidate(path, candidate_id, "pending_confirmation")
    with pytest.raises(ResearchWorkflowError, match="只有人工确认"):
        create_research_task(path, candidate_id)


def test_online_research_creates_review_draft_with_needs_evidence_sources(
    tmp_path: Path,
) -> None:
    path, candidate_id, _ = _prepared_database(tmp_path)
    _select_topic(path, candidate_id)
    task_id = create_research_task(path, candidate_id).task_id
    confirm_research_task(path, task_id)
    runner = _FakeOnlineRunner(_successful_online_result())

    mutation = run_online_research(path, task_id, runner)
    task = get_research_task(path, task_id=task_id)

    assert mutation.draft_id > 0
    assert mutation.version == 1
    assert mutation.task_status == "draft_review"
    assert task is not None
    assert task["status"] == "draft_review"
    assert task["can_run_online_research"] is False
    draft = task["latest_draft"]
    assert draft is not None
    assert draft["is_online_research"] is True
    assert draft["is_offline_demo"] is False
    assert draft["generated_by"] == "pi_web_access_v1"
    assert len(draft["sources"]) == 2
    assert {source["source_type"] for source in draft["sources"]} == {
        "regulator",
        "research_paper",
    }
    assert all(source["evidence_status"] == "needs_evidence" for source in draft["sources"])
    event_types = {event["event_type"] for event in task["events"]}
    assert "online_research_started" in event_types
    assert "online_research_draft_generated" in event_types
    assert len(runner.requests) == 1
    exchange_dir = path.parent / "raw" / "agent"
    assert exchange_dir.is_dir()
    assert list(exchange_dir.glob("*-input.json"))


def test_online_research_failure_reverts_task_without_a_draft(tmp_path: Path) -> None:
    path, candidate_id, _ = _prepared_database(tmp_path)
    _select_topic(path, candidate_id)
    task_id = create_research_task(path, candidate_id).task_id
    confirm_research_task(path, task_id)
    failed = AgentRunResult(
        status="failed",
        final_text="",
        tool_calls=(),
        event_lines=(),
        exit_code=1,
        stderr="provider temporarily unavailable",
    )
    runner = _FakeOnlineRunner(failed)

    mutation = run_online_research(path, task_id, runner)
    task = get_research_task(path, task_id=task_id)

    assert mutation.draft_id == 0
    assert mutation.task_status == "pending_start"
    assert task is not None
    assert task["status"] == "pending_start"
    assert task["drafts"] == []
    assert any(event["event_type"] == "online_research_failed" for event in task["events"])
    assert any(
        "provider temporarily unavailable" == (event["reason"] or "")
        for event in task["events"]
    )


def test_online_research_rejects_body_with_internal_markers(tmp_path: Path) -> None:
    path, candidate_id, _ = _prepared_database(tmp_path)
    _select_topic(path, candidate_id)
    task_id = create_research_task(path, candidate_id).task_id
    confirm_research_task(path, task_id)
    payload = json.loads(ONLINE_JSON)
    payload["article_body"] = "# 标题\n\n该数据仍需待补证。"
    marker_result = AgentRunResult(
        status="completed",
        final_text=json.dumps(payload, ensure_ascii=False),
        tool_calls=(),
        event_lines=(),
        exit_code=0,
        stderr="",
    )

    mutation = run_online_research(path, task_id, _FakeOnlineRunner(marker_result))
    task = get_research_task(path, task_id=task_id)

    assert mutation.draft_id == 0
    assert task is not None
    assert task["status"] == "pending_start"
    assert any(event["event_type"] == "online_research_failed" for event in task["events"])


def test_online_research_accepts_signals_structured_agent_output(
    tmp_path: Path,
) -> None:
    path, candidate_id, _ = _prepared_database(tmp_path)
    _select_topic(path, candidate_id)
    task_id = create_research_task(path, candidate_id).task_id
    confirm_research_task(path, task_id)
    payload = {
        "title": "保健食品虚假宣传典型案例与执法尺度",
        "executive_summary": "宁夏等地案例表明宣称合规正延伸到直播与私域。",
        "signals": [
            {
                "category": "监管动态",
                "title": "宁夏发布典型案例",
                "detail": "宁夏市场监管部门公布多起保健食品虚假宣传处罚案例。",
                "decision_implication": "企业应全渠道审查宣称。",
                "source_type": "权威媒体",
                "urls": ["https://example.com/ningxia"],
            },
            {
                "category": "监管动态",
                "title": "直播场景执法",
                "detail": "直播话术成为执法重点。具体金额待补证。",
                "decision_implication": "直播脚本需合规审查。",
                "source_type": "监管原文",
                "urls": ["https://example.com/samr"],
            },
        ],
        "uncertainties_and_caveats": ["部分处罚决定书未公开全文。"],
    }
    runner = _FakeOnlineRunner(
        AgentRunResult(
            status="completed",
            final_text=json.dumps(payload, ensure_ascii=False),
            tool_calls=(),
            event_lines=(),
            exit_code=0,
            stderr="",
        )
    )

    mutation = run_online_research(path, task_id, runner)
    task = get_research_task(path, task_id=task_id)

    assert mutation.draft_id > 0
    assert task is not None
    draft = task["latest_draft"]
    assert "## 监管动态" in draft["article_body"]
    assert "宁夏发布典型案例" in draft["article_body"]
    assert "待补证" not in draft["article_body"]
    assert "已从可发布正文中移除" in draft["uncertainty_notes"]
    assert {source["source_type"] for source in draft["sources"]} == {
        "regulator",
        "media",
    }


def test_confirmed_topic_creates_one_editable_prefilled_task(
    tmp_path: Path,
) -> None:
    path, candidate_id, _ = _prepared_database(tmp_path)
    _select_topic(path, candidate_id)

    created = create_research_task(path, candidate_id)
    repeated = create_research_task(path, candidate_id)
    task = get_research_task(path, candidate_id=candidate_id)

    assert task is not None
    assert created.created is True
    assert repeated.created is False
    assert repeated.task_id == created.task_id
    assert task["status"] == "task_drafting"
    assert task["start_mode"] == "manual"
    assert task["editable"] is True
    assert task["can_confirm"] is True
    assert task["research_question"]
    assert task["selection_dimensions"]
    assert len(task["known_evidence"]) == 2
    assert "不会" not in str(task["output_requirements"])
    assert "可直接作为微信公众号发布" in str(task["output_requirements"])
    assert "不得出现“待补证”" in str(task["output_requirements"])


def test_closed_cycle_keeps_topic_read_only_but_allows_downstream_task(
    tmp_path: Path,
) -> None:
    path, candidate_id, cycle_id = _prepared_database(tmp_path)
    _select_topic(path, candidate_id)
    close_monitoring_cycle(path, cycle_id, reason="本期已归档")

    result = create_research_task(path, candidate_id)
    task = get_research_task(path, task_id=result.task_id)

    assert task is not None
    assert task["cycle_status"] == "closed"
    assert task["candidate_status"] == "selected"
    connection = connect_database(path, readonly=True)
    try:
        assert connection.execute(
            "SELECT status FROM topic_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()[0] == "selected"
    finally:
        connection.close()


def test_task_can_be_edited_then_confirmed_and_is_locked(tmp_path: Path) -> None:
    path, candidate_id, _ = _prepared_database(tmp_path)
    _select_topic(path, candidate_id)
    created = create_research_task(path, candidate_id)
    task = get_research_task(path, task_id=created.task_id)
    assert task is not None

    _save_task(
        path,
        task,
        decision_use="支持品牌方判断是否需要调整注册与供应链节奏。",
        selection_dimensions=["监管与合规", "业务含义"],
    )
    confirmed = confirm_research_task(path, created.task_id)
    locked = get_research_task(path, task_id=created.task_id)

    assert confirmed.status == "pending_start"
    assert locked is not None
    assert locked["status"] == "pending_start"
    assert locked["editable"] is False
    assert locked["confirmed_at"] is not None
    assert locked["decision_use"].startswith("支持品牌方")
    assert [event["event_type"] for event in locked["events"]] == [
        "task_confirmed",
        "task_edited",
        "task_created",
    ]
    with pytest.raises(ResearchWorkflowError, match="不能继续修改"):
        _save_task(path, locked, audience="新的目标读者")


def test_incomplete_task_cannot_pass_the_human_gate(tmp_path: Path) -> None:
    path, candidate_id, _ = _prepared_database(tmp_path)
    _select_topic(path, candidate_id)
    created = create_research_task(path, candidate_id)
    task = get_research_task(path, task_id=created.task_id)
    assert task is not None
    _save_task(path, task, audience="")

    with pytest.raises(ResearchWorkflowError, match="目标读者"):
        confirm_research_task(path, created.task_id)

    draft = get_research_task(path, task_id=created.task_id)
    assert draft is not None
    assert draft["status"] == "task_drafting"
    assert draft["can_confirm"] is False
    assert "目标读者" in draft["missing_field_labels"]


def test_offline_article_demo_starts_only_after_task_confirmation(tmp_path: Path) -> None:
    path, candidate_id, _ = _prepared_database(tmp_path)
    _select_topic(path, candidate_id)
    created = create_research_task(path, candidate_id)

    with pytest.raises(ResearchWorkflowError, match="已人工确认"):
        run_offline_draft_demo(path, created.task_id)

    confirm_research_task(path, created.task_id)
    result = run_offline_draft_demo(path, created.task_id)
    task = get_research_task(path, task_id=created.task_id)

    assert task is not None
    assert result.version == 1
    assert task["status"] == "draft_review"
    assert task["can_run_offline_demo"] is False
    assert task["can_finalize"] is True
    assert task["latest_draft"]["generated_by"] == "offline_article_demo_v1"
    assert "离线 Demo 初稿（未联网检索）" in task["latest_draft"]["article_body"]
    assert r"\n> 本稿" not in task["latest_draft"]["article_body"]
    assert "？。现阶段" not in task["latest_draft"]["article_body"]
    assert len(task["latest_draft"]["sources"]) == 2
    assert {
        source["evidence_status"] for source in task["latest_draft"]["sources"]
    } == {"needs_evidence"}
    assert [event["event_type"] for event in task["events"][:2]] == [
        "offline_demo_draft_generated",
        "offline_demo_started",
    ]


def test_draft_revisions_are_immutable_and_final_article_is_local_markdown(
    tmp_path: Path,
) -> None:
    path, candidate_id, _ = _prepared_database(tmp_path)
    _select_topic(path, candidate_id)
    created = create_research_task(path, candidate_id)
    confirm_research_task(path, created.task_id)
    first = run_offline_draft_demo(path, created.task_id)

    second = regenerate_offline_draft_demo(
        path,
        created.task_id,
        revision_reason="补充更清楚的品牌方行动框架。",
    )
    after_regeneration = get_research_task(path, task_id=created.task_id)
    assert after_regeneration is not None
    assert second.version == 2
    assert [draft["status"] for draft in after_regeneration["drafts"]] == [
        "review",
        "superseded",
    ]
    assert after_regeneration["drafts"][1]["id"] == first.draft_id

    latest = after_regeneration["latest_draft"]
    third = save_draft_revision(
        path,
        created.task_id,
        int(latest["id"]),
        title=str(latest["title"]),
        article_body=str(latest["article_body"]) + "\n\n## 人工补充\n\n保留待补证边界。\n",
        key_judgment=str(latest["key_judgment"]),
        action_implications=str(latest["action_implications"]),
        uncertainty_notes=str(latest["uncertainty_notes"]),
        revision_reason="补充人工编辑段落。",
    )
    after_edit = get_research_task(path, task_id=created.task_id)
    assert after_edit is not None
    assert third.version == 3
    assert after_edit["latest_draft"]["generated_by"] == "manual_editor"
    assert [draft["status"] for draft in after_edit["drafts"]] == [
        "review",
        "superseded",
        "superseded",
    ]

    with pytest.raises(ResearchWorkflowError, match="最终审阅说明不能为空"):
        finalize_research_draft(
            path, created.task_id, third.draft_id, review_note=""
        )

    final = finalize_research_draft(
        path,
        created.task_id,
        third.draft_id,
        review_note="人工审阅完成；未补齐的事实继续保留待补证标记。",
    )
    completed = get_research_task(path, task_id=created.task_id)

    assert final.task_status == "completed"
    assert final.draft_status == "final"
    assert final.final_markdown_path is not None
    assert final.final_markdown_path.is_file()
    archived = final.final_markdown_path.read_text(encoding="utf-8")
    assert "人工审阅完成" in archived
    assert "待补证" in archived
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["latest_draft"]["status"] == "final"
    assert completed["latest_draft"]["final_markdown_absolute_path"] == str(
        final.final_markdown_path
    )
    with pytest.raises(ResearchWorkflowError, match="只有稿件待审"):
        regenerate_offline_draft_demo(
            path,
            created.task_id,
            revision_reason="完成后不应再生成。",
        )
