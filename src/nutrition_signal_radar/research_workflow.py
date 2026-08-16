from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .agent_gateway import AgentRunResult, PiAgentRequest, save_agent_exchange
from .database import connect_database, transaction
from .schema import initialize_schema
from .source_registry import list_priority_sites


SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

RESEARCH_TASK_FIELD_LABELS = {
    "research_question": "核心研究问题",
    "audience": "目标读者",
    "decision_use": "决策用途",
    "scope": "研究范围",
    "geography": "地域范围",
    "time_range": "时间范围",
    "exclusions": "明确排除项",
    "selection_dimensions": "判断维度",
    "known_evidence": "已有公众号线索",
    "evidence_gaps": "待补证主张",
    "output_requirements": "稿件要求",
}

EDITABLE_TEXT_FIELDS = (
    "research_question",
    "audience",
    "decision_use",
    "scope",
    "geography",
    "time_range",
    "exclusions",
    "evidence_gaps",
    "output_requirements",
)

MAX_FIELD_LENGTHS = {
    "research_question": 2_000,
    "audience": 500,
    "decision_use": 2_000,
    "scope": 4_000,
    "geography": 500,
    "time_range": 500,
    "exclusions": 4_000,
    "evidence_gaps": 4_000,
    "output_requirements": 4_000,
}

MAX_DRAFT_FIELD_LENGTHS = {
    "title": 500,
    "article_body": 32_000,
    "key_judgment": 4_000,
    "action_implications": 4_000,
    "uncertainty_notes": 4_000,
    "revision_reason": 1_000,
    "review_note": 1_000,
}

OFFLINE_DEMO_GENERATOR = "offline_article_demo_v1"
ONLINE_RESEARCH_GENERATOR = "pi_web_access_v1"


class OnlineResearchRunner(Protocol):
    def run(self, request: PiAgentRequest) -> AgentRunResult: ...


class ResearchWorkflowError(ValueError):
    """Raised when a research task would bypass an editorial gate."""


@dataclass(frozen=True)
class ResearchTaskMutationResult:
    task_id: int
    candidate_id: int
    status: str
    created: bool = False
    changed_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchDraftMutationResult:
    task_id: int
    draft_id: int
    version: int
    task_status: str
    draft_status: str
    final_markdown_path: Path | None = None


def research_task_missing_fields(
    task: Mapping[str, object] | sqlite3.Row,
) -> tuple[str, ...]:
    missing: list[str] = []
    for field in EDITABLE_TEXT_FIELDS:
        if not str(_record_value(task, field, "") or "").strip():
            missing.append(field)
    if not _json_list_value(
        task,
        parsed_key="selection_dimensions",
        raw_key="selection_dimensions_json",
    ):
        missing.append("selection_dimensions")
    if not _json_list_value(
        task,
        parsed_key="known_evidence",
        raw_key="known_evidence_json",
    ):
        missing.append("known_evidence")
    return tuple(missing)


def create_research_task(
    database_path: Path,
    candidate_id: int,
    *,
    actor: str = "operator",
) -> ResearchTaskMutationResult:
    """Create one editable task brief from one confirmed topic package.

    Monitoring-cycle closure intentionally does not block this downstream action:
    the confirmed package remains immutable, while the new research task owns its
    own lifecycle and audit trail.
    """

    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            package = connection.execute(
                """
                SELECT p.*, c.id AS candidate_id, c.status AS candidate_status,
                       mc.start_date AS cycle_start_date,
                       mc.end_date AS cycle_end_date
                FROM topic_packages p
                JOIN topic_candidates c ON c.id = p.candidate_id
                JOIN industry_signals s ON s.id = c.signal_id
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                JOIN monitoring_cycles mc ON mc.id = tc.cycle_id
                WHERE c.id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if package is None:
                raise ResearchWorkflowError("该候选还没有选题包，不能生成研究任务书")
            if package["candidate_status"] != "selected" or package["status"] != "confirmed":
                raise ResearchWorkflowError("只有人工确认的已选题才能生成研究任务书")

            existing = connection.execute(
                "SELECT id, status FROM research_tasks WHERE topic_package_id = ?",
                (package["id"],),
            ).fetchone()
            if existing is not None:
                return ResearchTaskMutationResult(
                    task_id=int(existing["id"]),
                    candidate_id=candidate_id,
                    status=str(existing["status"]),
                    created=False,
                )

            audience = str(package["audience"] or "").strip()
            scope_parts = [
                str(package["industry_angle"] or "").strip(),
                str(package["research_directions"] or "").strip(),
            ]
            scope = "\n".join(value for value in scope_parts if value)
            cycle_start = str(package["cycle_start_date"] or "").strip()
            cycle_end = str(package["cycle_end_date"] or "").strip()
            if cycle_start and cycle_end:
                time_range = (
                    f"重点核查 {cycle_start} 至 {cycle_end} 的新增信号；"
                    "必要时回溯相关监管、科研与市场背景。"
                )
            else:
                time_range = "以本期行业信号为起点，必要时回溯相关背景。"

            cursor = connection.execute(
                f"""
                INSERT INTO research_tasks(
                    topic_package_id, research_question, audience, decision_use,
                    scope, geography, time_range, exclusions,
                    selection_dimensions_json, known_evidence_json,
                    evidence_gaps, output_requirements, status, start_mode,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'task_drafting', 'manual', ({SQL_NOW}), ({SQL_NOW}))
                """,
                (
                    package["id"],
                    str(package["core_question"] or "").strip(),
                    audience,
                    f"为{audience or '目标读者'}提供可用于行业判断与内容决策的分析。",
                    scope,
                    "以选题所涉市场为准，优先核查中国公开资料。",
                    time_range,
                    (
                        "不把公众号发现线索表述为已确认事实；不使用付费数据库或"
                        "内部数据库；不触发微信公众号发布。"
                    ),
                    str(package["selection_dimensions_json"] or "[]"),
                    str(package["evidence_json"] or "[]"),
                    str(package["evidence_gaps"] or "").strip(),
                    (
                        "产出适合微信公众号阅读的文章型深度研究稿：聚焦一个核心判断，"
                        "正文简洁，建议约 1800—2500 字；重要事实关联互联网公开来源；"
                        "暂时无法补足的一手证据明确标记“待补证”。"
                    ),
                ),
            )
            task_id = int(cursor.lastrowid)
            _record_event(
                connection,
                task_id,
                "task_created",
                actor=actor,
                to_status="task_drafting",
                payload={
                    "candidate_id": candidate_id,
                    "topic_package_id": int(package["id"]),
                    "start_mode": "manual",
                },
            )
        return ResearchTaskMutationResult(
            task_id=task_id,
            candidate_id=candidate_id,
            status="task_drafting",
            created=True,
        )
    finally:
        connection.close()


def update_research_task(
    database_path: Path,
    task_id: int,
    *,
    research_question: str,
    audience: str,
    decision_use: str,
    scope: str,
    geography: str,
    time_range: str,
    exclusions: str,
    selection_dimensions: list[str],
    evidence_gaps: str,
    output_requirements: str,
    actor: str = "operator",
) -> ResearchTaskMutationResult:
    values = {
        "research_question": research_question,
        "audience": audience,
        "decision_use": decision_use,
        "scope": scope,
        "geography": geography,
        "time_range": time_range,
        "exclusions": exclusions,
        "evidence_gaps": evidence_gaps,
        "output_requirements": output_requirements,
    }
    cleaned = {
        field: _clean_text(field, value)
        for field, value in values.items()
    }
    dimensions = _clean_dimensions(selection_dimensions)

    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            row = _task_row(connection, task_id=task_id)
            if row is None:
                raise ResearchWorkflowError(f"研究任务 {task_id} 不存在")
            if row["status"] != "task_drafting":
                raise ResearchWorkflowError("研究任务书已确认；当前版本不能继续修改")

            changed_fields = tuple(
                field
                for field in EDITABLE_TEXT_FIELDS
                if str(row[field] or "") != cleaned[field]
            )
            old_dimensions = _json_list(str(row["selection_dimensions_json"] or "[]"))
            if old_dimensions != dimensions:
                changed_fields += ("selection_dimensions",)

            if changed_fields:
                connection.execute(
                    f"""
                    UPDATE research_tasks SET
                        research_question = ?, audience = ?, decision_use = ?,
                        scope = ?, geography = ?, time_range = ?, exclusions = ?,
                        selection_dimensions_json = ?, evidence_gaps = ?,
                        output_requirements = ?, updated_at = ({SQL_NOW})
                    WHERE id = ?
                    """,
                    (
                        cleaned["research_question"],
                        cleaned["audience"],
                        cleaned["decision_use"],
                        cleaned["scope"],
                        cleaned["geography"],
                        cleaned["time_range"],
                        cleaned["exclusions"],
                        json.dumps(dimensions, ensure_ascii=False),
                        cleaned["evidence_gaps"],
                        cleaned["output_requirements"],
                        task_id,
                    ),
                )
                _record_event(
                    connection,
                    task_id,
                    "task_edited",
                    actor=actor,
                    payload={"changed_fields": list(changed_fields)},
                )
            candidate_id = int(row["candidate_id"])
        return ResearchTaskMutationResult(
            task_id=task_id,
            candidate_id=candidate_id,
            status="task_drafting",
            changed_fields=changed_fields,
        )
    finally:
        connection.close()


def confirm_research_task(
    database_path: Path,
    task_id: int,
    *,
    actor: str = "operator",
) -> ResearchTaskMutationResult:
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            row = _task_row(connection, task_id=task_id)
            if row is None:
                raise ResearchWorkflowError(f"研究任务 {task_id} 不存在")
            if row["status"] != "task_drafting":
                raise ResearchWorkflowError("只有待确认的研究任务书可以确认")
            missing = research_task_missing_fields(row)
            if missing:
                labels = "、".join(RESEARCH_TASK_FIELD_LABELS[field] for field in missing)
                raise ResearchWorkflowError(f"研究任务书尚不完整：{labels}")

            connection.execute(
                f"""
                UPDATE research_tasks SET
                    status = 'pending_start', confirmed_at = ({SQL_NOW}),
                    updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (task_id,),
            )
            _record_event(
                connection,
                task_id,
                "task_confirmed",
                actor=actor,
                from_status="task_drafting",
                to_status="pending_start",
                payload={"start_mode": str(row["start_mode"])},
            )
            candidate_id = int(row["candidate_id"])
        return ResearchTaskMutationResult(
            task_id=task_id,
            candidate_id=candidate_id,
            status="pending_start",
        )
    finally:
        connection.close()


def run_offline_draft_demo(
    database_path: Path,
    task_id: int,
    *,
    actor: str = "operator",
) -> ResearchDraftMutationResult:
    """Create a clearly labelled, non-networked article-shaped Demo draft.

    This is intentionally not a substitute for a public-web research Agent.  It
    only turns the confirmed task brief and its discovery sources into a
    reviewable article structure, and keeps every discovery source marked
    ``needs_evidence``.
    """

    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            task = _task_row(connection, task_id=task_id)
            if task is None:
                raise ResearchWorkflowError(f"研究任务 {task_id} 不存在")
            if task["status"] != "pending_start":
                raise ResearchWorkflowError("只有已人工确认、待启动的任务可以启动离线文章 Demo")
            if _latest_draft_row(connection, task_id) is not None:
                raise ResearchWorkflowError("该研究任务已有稿件，不能再次创建首版 Demo")

            connection.execute(
                f"""
                UPDATE research_tasks SET
                    status = 'researching', start_requested_at = ({SQL_NOW}),
                    started_at = ({SQL_NOW}), updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (task_id,),
            )
            _record_event(
                connection,
                task_id,
                "offline_demo_started",
                actor=actor,
                from_status="pending_start",
                to_status="researching",
                payload={
                    "generator": OFFLINE_DEMO_GENERATOR,
                    "network_access": False,
                },
            )
            draft_id, version = _insert_offline_demo_draft(
                connection,
                task,
                revision_reason=None,
            )
            connection.execute(
                f"""
                UPDATE research_tasks SET
                    status = 'draft_review', updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (task_id,),
            )
            _record_event(
                connection,
                task_id,
                "offline_demo_draft_generated",
                actor=actor,
                draft_id=draft_id,
                from_status="researching",
                to_status="draft_review",
                payload={
                    "generator": OFFLINE_DEMO_GENERATOR,
                    "version": version,
                    "network_access": False,
                },
            )
        return ResearchDraftMutationResult(
            task_id=task_id,
            draft_id=draft_id,
            version=version,
            task_status="draft_review",
            draft_status="review",
        )
    finally:
        connection.close()


def run_online_research(
    database_path: Path,
    task_id: int,
    runner: OnlineResearchRunner,
    *,
    actor: str = "operator",
) -> ResearchDraftMutationResult:
    """Run the confirmed task through the public-web pi research Agent.

    The human gate stays intact: only a confirmed ``pending_start`` task can
    be started, the task is marked ``researching`` while the external call is
    in flight, and a failed or non-JSON result reverts it to ``pending_start``
    with an audit event instead of inventing a draft.
    """

    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            task = _task_row(connection, task_id=task_id)
            if task is None:
                raise ResearchWorkflowError(f"研究任务 {task_id} 不存在")
            if task["status"] != "pending_start":
                raise ResearchWorkflowError("只有已人工确认、待启动的任务可以启动联网研究")
            if task["confirmed_at"] is None:
                raise ResearchWorkflowError("研究任务书尚未通过人工确认")
            if _latest_draft_row(connection, task_id) is not None:
                raise ResearchWorkflowError("该研究任务已有稿件，不能再次创建首版")

            connection.execute(
                f"""
                UPDATE research_tasks SET
                    status = 'researching', start_requested_at = ({SQL_NOW}),
                    started_at = ({SQL_NOW}), updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (task_id,),
            )
            _record_event(
                connection,
                task_id,
                "online_research_started",
                actor=actor,
                from_status="pending_start",
                to_status="researching",
                payload={
                    "generator": ONLINE_RESEARCH_GENERATOR,
                    "network_access": True,
                },
            )
    finally:
        connection.close()

    request = _build_online_research_request(database_path, task)
    try:
        result = runner.run(request)
    except Exception as exc:  # External runner failures must not fake a draft.
        result = AgentRunResult(
            status="failed",
            final_text="",
            tool_calls=(),
            event_lines=(),
            exit_code=-1,
            stderr=f"{type(exc).__name__}: {exc}"[:2000],
        )
    save_agent_exchange(database_path.parent / "raw" / "agent", request, result)

    parsed: dict[str, object] | None = None
    error: str | None = None
    if result.status == "completed":
        try:
            parsed = _parse_online_research_output(result.final_text)
        except ResearchWorkflowError as exc:
            error = str(exc)
    else:
        error = result.stderr.strip() or f"研究 Agent 状态异常：{result.status}"

    connection = connect_database(database_path)
    try:
        with transaction(connection):
            if parsed is None:
                connection.execute(
                    f"""
                    UPDATE research_tasks SET
                        status = 'pending_start', updated_at = ({SQL_NOW})
                    WHERE id = ?
                    """,
                    (task_id,),
                )
                _record_event(
                    connection,
                    task_id,
                    "online_research_failed",
                    actor=actor,
                    from_status="researching",
                    to_status="pending_start",
                    reason=error,
                    payload={
                        "generator": ONLINE_RESEARCH_GENERATOR,
                        "agent_status": result.status,
                    },
                )
                return ResearchDraftMutationResult(
                    task_id=task_id,
                    draft_id=0,
                    version=0,
                    task_status="pending_start",
                    draft_status="review",
                )

            draft_id, version = _insert_online_research_draft(
                connection, task, parsed, result
            )
            connection.execute(
                f"""
                UPDATE research_tasks SET
                    status = 'draft_review', updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (task_id,),
            )
            _record_event(
                connection,
                task_id,
                "online_research_draft_generated",
                actor=actor,
                draft_id=draft_id,
                from_status="researching",
                to_status="draft_review",
                payload={
                    "generator": ONLINE_RESEARCH_GENERATOR,
                    "version": version,
                    "source_count": len(_online_sources(parsed)),
                    "web_tool_calls": result.web_tool_calls,
                    "network_access": True,
                },
            )
        return ResearchDraftMutationResult(
            task_id=task_id,
            draft_id=draft_id,
            version=version,
            task_status="draft_review",
            draft_status="review",
        )
    finally:
        connection.close()


def regenerate_offline_draft_demo(
    database_path: Path,
    task_id: int,
    *,
    revision_reason: str,
    actor: str = "operator",
) -> ResearchDraftMutationResult:
    """Return a review draft and create an immutable, labelled Demo successor."""

    reason = _clean_draft_text("revision_reason", revision_reason)
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            task = _task_row(connection, task_id=task_id)
            if task is None:
                raise ResearchWorkflowError(f"研究任务 {task_id} 不存在")
            if task["status"] != "draft_review":
                raise ResearchWorkflowError("只有稿件待审的任务可以退回并重新生成 Demo")
            previous = _latest_review_draft_row(connection, task_id)
            if previous is None:
                raise ResearchWorkflowError("当前没有可退回的稿件版本")

            _supersede_draft(connection, int(previous["id"]))
            draft_id, version = _insert_offline_demo_draft(
                connection,
                task,
                revision_reason=reason,
                supersedes_draft_id=int(previous["id"]),
            )
            _record_event(
                connection,
                task_id,
                "offline_demo_draft_regenerated",
                actor=actor,
                draft_id=draft_id,
                from_status="draft_review",
                to_status="draft_review",
                reason=reason,
                payload={
                    "generator": OFFLINE_DEMO_GENERATOR,
                    "version": version,
                    "supersedes_draft_id": int(previous["id"]),
                    "network_access": False,
                },
            )
        return ResearchDraftMutationResult(
            task_id=task_id,
            draft_id=draft_id,
            version=version,
            task_status="draft_review",
            draft_status="review",
        )
    finally:
        connection.close()


def save_draft_revision(
    database_path: Path,
    task_id: int,
    draft_id: int,
    *,
    title: str,
    article_body: str,
    key_judgment: str,
    action_implications: str,
    uncertainty_notes: str,
    revision_reason: str,
    actor: str = "operator",
) -> ResearchDraftMutationResult:
    """Save an editor's changes as a new version instead of overwriting a draft."""

    cleaned = {
        "title": _clean_draft_text("title", title),
        "article_body": _clean_draft_text("article_body", article_body),
        "key_judgment": _clean_draft_text("key_judgment", key_judgment),
        "action_implications": _clean_draft_text(
            "action_implications", action_implications
        ),
        "uncertainty_notes": _clean_draft_text(
            "uncertainty_notes", uncertainty_notes
        ),
        "revision_reason": _clean_draft_text("revision_reason", revision_reason),
    }
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            task = _task_row(connection, task_id=task_id)
            if task is None:
                raise ResearchWorkflowError(f"研究任务 {task_id} 不存在")
            if task["status"] != "draft_review":
                raise ResearchWorkflowError("只有稿件待审的任务可以保存修订")
            previous = _latest_review_draft_row(connection, task_id)
            if previous is None or int(previous["id"]) != draft_id:
                raise ResearchWorkflowError("只能基于当前最新的待审稿件保存修订")

            if all(
                str(previous[field] or "") == cleaned[field]
                for field in (
                    "title",
                    "article_body",
                    "key_judgment",
                    "action_implications",
                    "uncertainty_notes",
                )
            ):
                raise ResearchWorkflowError("正文没有变化；无需创建新的稿件版本")

            _supersede_draft(connection, draft_id)
            version = int(previous["version"]) + 1
            cursor = connection.execute(
                f"""
                INSERT INTO research_drafts(
                    research_task_id, supersedes_draft_id, title, article_body,
                    key_judgment, action_implications, uncertainty_notes, version,
                    revision_reason, change_summary, generated_by, status, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual_editor', 'review', ({SQL_NOW}))
                """,
                (
                    task_id,
                    draft_id,
                    cleaned["title"],
                    cleaned["article_body"],
                    cleaned["key_judgment"],
                    cleaned["action_implications"],
                    cleaned["uncertainty_notes"],
                    version,
                    cleaned["revision_reason"],
                    "人工编辑保存为新版本；未覆盖原稿。",
                ),
            )
            new_draft_id = int(cursor.lastrowid)
            _copy_draft_sources(connection, draft_id, new_draft_id)
            connection.execute(
                f"UPDATE research_tasks SET updated_at = ({SQL_NOW}) WHERE id = ?",
                (task_id,),
            )
            _record_event(
                connection,
                task_id,
                "draft_revised",
                actor=actor,
                draft_id=new_draft_id,
                from_status="draft_review",
                to_status="draft_review",
                reason=cleaned["revision_reason"],
                payload={
                    "version": version,
                    "supersedes_draft_id": draft_id,
                    "generated_by": "manual_editor",
                },
            )
        return ResearchDraftMutationResult(
            task_id=task_id,
            draft_id=new_draft_id,
            version=version,
            task_status="draft_review",
            draft_status="review",
        )
    finally:
        connection.close()


def finalize_research_draft(
    database_path: Path,
    task_id: int,
    draft_id: int,
    *,
    review_note: str,
    actor: str = "operator",
) -> ResearchDraftMutationResult:
    """Human-confirm one reviewed draft and write an immutable local Markdown file."""

    note = _clean_draft_text("review_note", review_note)
    initialize_schema(database_path)
    connection = connect_database(database_path)
    final_path: Path | None = None
    try:
        with transaction(connection):
            task = _task_row(connection, task_id=task_id)
            if task is None:
                raise ResearchWorkflowError(f"研究任务 {task_id} 不存在")
            if task["status"] != "draft_review":
                raise ResearchWorkflowError("只有稿件待审的任务可以确认最终文章")
            draft = _draft_row(connection, draft_id=draft_id, task_id=task_id)
            latest = _latest_draft_row(connection, task_id)
            if draft is None or latest is None or int(latest["id"]) != draft_id:
                raise ResearchWorkflowError("只能确认当前最新的稿件版本")
            if draft["status"] != "review":
                raise ResearchWorkflowError("当前稿件不是可确认的待审版本")

            sources = _draft_sources(connection, draft_id)
            final_path = _final_markdown_path(
                database_path,
                task_id=task_id,
                version=int(draft["version"]),
            )
            _write_new_markdown(
                final_path,
                _final_markdown_content(task, draft, sources, review_note=note),
            )
            relative_path = _relative_to_database(database_path, final_path)
            connection.execute(
                f"""
                UPDATE research_drafts SET
                    status = 'final', reviewed_at = ({SQL_NOW}),
                    final_markdown_relative_path = ?
                WHERE id = ?
                """,
                (relative_path, draft_id),
            )
            connection.execute(
                f"""
                UPDATE research_tasks SET
                    status = 'completed', updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (task_id,),
            )
            _record_event(
                connection,
                task_id,
                "draft_finalized",
                actor=actor,
                draft_id=draft_id,
                from_status="draft_review",
                to_status="completed",
                reason=note,
                payload={
                    "version": int(draft["version"]),
                    "final_markdown_relative_path": relative_path,
                },
            )
        return ResearchDraftMutationResult(
            task_id=task_id,
            draft_id=draft_id,
            version=int(draft["version"]),
            task_status="completed",
            draft_status="final",
            final_markdown_path=final_path,
        )
    except BaseException:
        if final_path is not None:
            final_path.unlink(missing_ok=True)
        raise
    finally:
        connection.close()


def list_research_tasks(
    database_path: Path,
    *,
    cycle_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return compact task cards for the current monitoring-cycle workspace."""

    initialize_schema(database_path)
    connection = connect_database(database_path, readonly=True)
    try:
        conditions = ""
        parameters: tuple[object, ...] = ()
        if cycle_id is not None:
            conditions = "WHERE mc.id = ?"
            parameters = (cycle_id,)
        rows = connection.execute(
            f"""
            SELECT rt.id, rt.status, rt.start_mode, rt.updated_at, rt.confirmed_at,
                   p.candidate_id, p.title AS topic_title,
                   mc.id AS cycle_id, mc.slug AS cycle_slug, mc.label AS cycle_label,
                   rd.id AS latest_draft_id, rd.version AS latest_draft_version,
                   rd.status AS latest_draft_status,
                   rd.final_markdown_relative_path,
                   (SELECT COUNT(*) FROM research_drafts counted
                    WHERE counted.research_task_id = rt.id) AS draft_count
            FROM research_tasks rt
            JOIN topic_packages p ON p.id = rt.topic_package_id
            JOIN topic_candidates c ON c.id = p.candidate_id
            JOIN industry_signals s ON s.id = c.signal_id
            JOIN topic_clusters tc ON tc.id = s.cluster_id
            JOIN monitoring_cycles mc ON mc.id = tc.cycle_id
            LEFT JOIN research_drafts rd ON rd.id = (
                SELECT latest.id FROM research_drafts latest
                WHERE latest.research_task_id = rt.id
                ORDER BY latest.version DESC LIMIT 1
            )
            {conditions}
            ORDER BY CASE rt.status
                WHEN 'draft_review' THEN 0
                WHEN 'pending_start' THEN 1
                WHEN 'task_drafting' THEN 2
                WHEN 'researching' THEN 3
                WHEN 'completed' THEN 4
                ELSE 5
            END, rt.updated_at DESC, rt.id DESC
            """,
            parameters,
        )
        payloads = [dict(row) for row in rows]
        for payload in payloads:
            relative_path = payload.get("final_markdown_relative_path")
            payload["final_markdown_absolute_path"] = (
                str((database_path.parent / str(relative_path)).resolve())
                if relative_path
                else None
            )
        return payloads
    finally:
        connection.close()


def get_research_task(
    database_path: Path,
    *,
    candidate_id: int | None = None,
    task_id: int | None = None,
) -> dict[str, Any] | None:
    if (candidate_id is None) == (task_id is None):
        raise ValueError("Provide exactly one of candidate_id or task_id")
    initialize_schema(database_path)
    connection = connect_database(database_path, readonly=True)
    try:
        row = _task_row(connection, candidate_id=candidate_id, task_id=task_id)
        if row is None:
            return None
        payload = dict(row)
        payload["selection_dimensions"] = _json_list(
            str(payload.pop("selection_dimensions_json", "[]") or "[]")
        )
        payload["known_evidence"] = _json_list(
            str(payload.pop("known_evidence_json", "[]") or "[]")
        )
        missing = research_task_missing_fields(payload)
        payload["missing_fields"] = list(missing)
        payload["missing_field_labels"] = [
            RESEARCH_TASK_FIELD_LABELS[field] for field in missing
        ]
        payload["can_confirm"] = payload["status"] == "task_drafting" and not missing
        payload["editable"] = payload["status"] == "task_drafting"
        drafts = _research_draft_payloads(connection, database_path, int(payload["id"]))
        payload["drafts"] = drafts
        payload["latest_draft"] = drafts[0] if drafts else None
        payload["can_run_offline_demo"] = payload["status"] == "pending_start"
        payload["can_run_online_research"] = payload["status"] == "pending_start"
        payload["can_finalize"] = bool(
            payload["status"] == "draft_review"
            and payload["latest_draft"]
            and payload["latest_draft"]["status"] == "review"
        )
        payload["events"] = [
            _event_payload(event)
            for event in connection.execute(
                """
                SELECT draft_id, event_type, actor, from_status, to_status, reason,
                       payload_json, created_at
                FROM research_events
                WHERE research_task_id = ?
                ORDER BY id DESC LIMIT 30
                """,
                (payload["id"],),
            )
        ]
        return payload
    finally:
        connection.close()


def _task_row(
    connection: sqlite3.Connection,
    *,
    candidate_id: int | None = None,
    task_id: int | None = None,
) -> sqlite3.Row | None:
    selector = "rt.id = ?" if task_id is not None else "c.id = ?"
    value = task_id if task_id is not None else candidate_id
    return connection.execute(
        f"""
        SELECT rt.*, p.candidate_id, p.title AS topic_title,
               c.status AS candidate_status,
               mc.id AS cycle_id, mc.slug AS cycle_slug,
               mc.label AS cycle_label, mc.status AS cycle_status
        FROM research_tasks rt
        JOIN topic_packages p ON p.id = rt.topic_package_id
        JOIN topic_candidates c ON c.id = p.candidate_id
        JOIN industry_signals s ON s.id = c.signal_id
        JOIN topic_clusters tc ON tc.id = s.cluster_id
        JOIN monitoring_cycles mc ON mc.id = tc.cycle_id
        WHERE {selector}
        """,
        (value,),
    ).fetchone()


def _draft_row(
    connection: sqlite3.Connection,
    *,
    draft_id: int,
    task_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM research_drafts WHERE id = ? AND research_task_id = ?",
        (draft_id, task_id),
    ).fetchone()


def _latest_draft_row(
    connection: sqlite3.Connection, task_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM research_drafts
        WHERE research_task_id = ?
        ORDER BY version DESC, id DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()


def _latest_review_draft_row(
    connection: sqlite3.Connection, task_id: int
) -> sqlite3.Row | None:
    latest = _latest_draft_row(connection, task_id)
    if latest is None or latest["status"] != "review":
        return None
    return latest


def _supersede_draft(connection: sqlite3.Connection, draft_id: int) -> None:
    cursor = connection.execute(
        "UPDATE research_drafts SET status = 'superseded' WHERE id = ? AND status = 'review'",
        (draft_id,),
    )
    if cursor.rowcount != 1:
        raise ResearchWorkflowError("当前稿件版本不能被退回")


def _insert_offline_demo_draft(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    revision_reason: str | None,
    supersedes_draft_id: int | None = None,
) -> tuple[int, int]:
    task_id = int(task["id"])
    previous = _latest_draft_row(connection, task_id)
    version = int(previous["version"]) + 1 if previous is not None else 1
    content = _build_offline_demo_content(task, revision_reason=revision_reason)
    cursor = connection.execute(
        f"""
        INSERT INTO research_drafts(
            research_task_id, supersedes_draft_id, title, article_body,
            key_judgment, action_implications, uncertainty_notes, version,
            revision_reason, change_summary, generated_by, status, generated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'review', ({SQL_NOW}))
        """,
        (
            task_id,
            supersedes_draft_id,
            content["title"],
            content["article_body"],
            content["key_judgment"],
            content["action_implications"],
            content["uncertainty_notes"],
            version,
            revision_reason,
            content["change_summary"],
            OFFLINE_DEMO_GENERATOR,
        ),
    )
    draft_id = int(cursor.lastrowid)
    _insert_discovery_sources_from_task(connection, draft_id, task)
    return draft_id, version


def _insert_discovery_sources_from_task(
    connection: sqlite3.Connection,
    draft_id: int,
    task: sqlite3.Row,
) -> None:
    evidence = _json_list(str(task["known_evidence_json"] or "[]"))
    for item in evidence:
        if isinstance(item, Mapping):
            title = str(item.get("title") or "未命名公众号发现线索").strip()
            source_name = str(item.get("account_name") or "公众号发现源").strip()
            reference = str(item.get("url") or "").strip()
            published_at = str(item.get("published_at") or "").strip()
        else:
            title = str(item or "未命名公众号发现线索").strip()
            source_name = "公众号发现源"
            reference = ""
            published_at = ""
        note = "随研究任务书带入的发现线索，不等于一手证据。"
        if published_at:
            note = f"{note} 原文日期：{published_at[:10]}。"
        connection.execute(
            """
            INSERT INTO research_sources(
                draft_id, claim, source_type, source_name, title,
                url_or_reference, evidence_status, source_note
            ) VALUES (?, ?, 'wechat_discovery', ?, ?, ?, 'needs_evidence', ?)
            """,
            (
                draft_id,
                "用于发现研究问题；不得据此单独确认监管、科研、功效或市场事实。",
                source_name,
                title,
                reference,
                note,
            ),
        )


def _build_online_research_request(
    database_path: Path,
    task: sqlite3.Row,
) -> PiAgentRequest:
    evidence = _json_list(str(task["known_evidence_json"] or "[]"))
    known = []
    for item in evidence:
        if isinstance(item, Mapping):
            title = str(item.get("title") or "").strip()
            reference = str(item.get("url") or "").strip()
            known.append(f"{title}｜{reference}" if reference else title)
        else:
            known.append(str(item or "").strip())
    enabled_sites = [site for site in list_priority_sites(database_path) if site.enabled]
    scope_parts = [str(task["scope"] or "").strip()]
    if str(task["geography"] or "").strip():
        scope_parts.append(f"地域范围：{str(task['geography']).strip()}")
    if str(task["exclusions"] or "").strip():
        scope_parts.append(f"明确排除：{str(task['exclusions']).strip()}")
    if enabled_sites:
        scope_parts.append(
            "优先检索公开网站：" + "、".join(site.domain for site in enabled_sites[:12])
        )
    return PiAgentRequest(
        research_question=str(task["research_question"] or "").strip(),
        audience=str(task["audience"] or "").strip(),
        decision_use=str(task["decision_use"] or "").strip(),
        scope="\n".join(value for value in scope_parts if value),
        time_range=str(task["time_range"] or "").strip(),
        known_evidence=tuple(value for value in known if value),
        evidence_gaps=str(task["evidence_gaps"] or "").strip(),
        output_requirements=str(task["output_requirements"] or "").strip(),
    )


def _parse_online_research_output(text: str) -> dict[str, object]:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ResearchWorkflowError("研究 Agent 未返回合法 JSON，任务已回退待启动") from exc
    if not isinstance(parsed, dict):
        raise ResearchWorkflowError("研究 Agent 返回的不是 JSON 对象")
    if not str(parsed.get("title") or "").strip():
        raise ResearchWorkflowError("研究 Agent 返回的 JSON 缺少标题")
    if not str(parsed.get("article_body") or "").strip():
        raise ResearchWorkflowError("研究 Agent 返回的 JSON 缺少正文")
    return parsed


def _online_sources(parsed: Mapping[str, object]) -> list[dict[str, str]]:
    raw = parsed.get("sources") or []
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url and not title:
            continue
        cleaned.append(
            {
                "claim": str(item.get("claim") or "").strip()[:2000] or title,
                "title": title[:500] or "未命名来源页面",
                "url": url,
                "publisher": str(item.get("publisher") or "未命名发布机构").strip()[:200],
                "source_type": _normalize_source_type(str(item.get("source_type") or "other")),
            }
        )
    return cleaned[:80]


def _normalize_source_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    mapping = {
        "regulator": {"regulator", "regulatory", "监管", "监管原文", "官方公告", "政府", "government"},
        "research_paper": {"research_paper", "paper", "journal", "论文", "学术", "期刊"},
        "company_announcement": {"company_announcement", "announcement", "企业公告", "公司公告", "信披"},
        "market_data": {"market_data", "report", "市场数据", "数据报告", "研报"},
        "media": {"media", "news", "媒体", "新闻", "权威媒体"},
        "wechat_discovery": {"wechat_discovery", "wechat", "公众号"},
    }
    for canonical, aliases in mapping.items():
        if normalized in aliases:
            return canonical
    return "other"


def _insert_online_research_draft(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    parsed: Mapping[str, object],
    result: AgentRunResult,
) -> tuple[int, int]:
    task_id = int(task["id"])
    sources = _online_sources(parsed)
    title = _clean_agent_text("title", str(parsed.get("title") or ""))
    article_body = _clean_agent_text("article_body", str(parsed.get("article_body") or ""))
    key_judgment = _clean_agent_text(
        "key_judgment", str(parsed.get("key_judgment") or ""), required=False
    )
    action_implications = _clean_agent_text(
        "action_implications", str(parsed.get("action_implications") or ""), required=False
    )
    uncertainty_notes = _clean_agent_text(
        "uncertainty_notes", str(parsed.get("uncertainty_notes") or ""), required=False
    )
    if not uncertainty_notes:
        uncertainty_notes = "研究 Agent 未单列不确定性；所有来源仍需人工打开具体页面复核后再确认事实。"
    cursor = connection.execute(
        f"""
        INSERT INTO research_drafts(
            research_task_id, supersedes_draft_id, title, article_body,
            key_judgment, action_implications, uncertainty_notes, version,
            revision_reason, change_summary, generated_by, status, generated_at
        ) VALUES (?, NULL, ?, ?, ?, ?, ?, 1, NULL, ?, ?, 'review', ({SQL_NOW}))
        """,
        (
            task_id,
            title,
            article_body,
            key_judgment,
            action_implications,
            uncertainty_notes,
            (
                f"公开互联网研究 V1：pi 通过 web_search / fetch_content 执行 "
                f"{result.web_tool_calls} 次检索并返回 {len(sources)} 条来源；"
                "所有来源仍为待补证，须人工复核。"
            ),
            ONLINE_RESEARCH_GENERATOR,
        ),
    )
    draft_id = int(cursor.lastrowid)
    for source in sources:
        connection.execute(
            f"""
            INSERT INTO research_sources(
                draft_id, claim, source_type, source_name, title,
                url_or_reference, evidence_status, accessed_at, source_note
            ) VALUES (?, ?, ?, ?, ?, ?, 'needs_evidence', ({SQL_NOW}), ?)
            """,
            (
                draft_id,
                source["claim"],
                source["source_type"],
                source["publisher"],
                source["title"],
                source["url"] or "未提供具体 URL",
                "由 pi 研究 Agent 返回；仍需人工打开具体页面复核事实，不得自动视为已核实。",
            ),
        )
    return draft_id, 1


def _clean_agent_text(field: str, value: str, *, required: bool = True) -> str:
    cleaned = str(value or "").strip()
    maximum = MAX_DRAFT_FIELD_LENGTHS[field]
    if required and not cleaned:
        labels = {
            "title": "文章标题",
            "article_body": "文章正文",
            "key_judgment": "核心判断",
            "action_implications": "行动启示",
            "uncertainty_notes": "不确定性说明",
        }
        raise ResearchWorkflowError(f"研究 Agent 返回的内容缺少{labels[field]}")
    return cleaned[:maximum]


def _copy_draft_sources(
    connection: sqlite3.Connection, source_draft_id: int, target_draft_id: int
) -> None:
    connection.execute(
        """
        INSERT INTO research_sources(
            draft_id, claim, source_type, source_name, title,
            url_or_reference, evidence_status, accessed_at, source_note
        )
        SELECT ?, claim, source_type, source_name, title,
               url_or_reference, evidence_status, accessed_at, source_note
        FROM research_sources WHERE draft_id = ? ORDER BY id
        """,
        (target_draft_id, source_draft_id),
    )


def _draft_sources(
    connection: sqlite3.Connection, draft_id: int
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT id, claim, source_type, source_name, title,
                   url_or_reference, evidence_status, accessed_at, source_note
            FROM research_sources WHERE draft_id = ? ORDER BY id
            """,
            (draft_id,),
        )
    )


def _research_draft_payloads(
    connection: sqlite3.Connection,
    database_path: Path,
    task_id: int,
) -> list[dict[str, Any]]:
    drafts: list[dict[str, Any]] = []
    for row in connection.execute(
        """
        SELECT * FROM research_drafts
        WHERE research_task_id = ? ORDER BY version DESC, id DESC
        """,
        (task_id,),
    ):
        payload = dict(row)
        payload["sources"] = [dict(source) for source in _draft_sources(connection, int(row["id"]))]
        relative_path = payload.get("final_markdown_relative_path")
        payload["final_markdown_absolute_path"] = (
            str((database_path.parent / str(relative_path)).resolve())
            if relative_path
            else None
        )
        payload["is_offline_demo"] = payload["generated_by"] == OFFLINE_DEMO_GENERATOR
        payload["is_online_research"] = payload["generated_by"] == ONLINE_RESEARCH_GENERATOR
        drafts.append(payload)
    return drafts


def _build_offline_demo_content(
    task: sqlite3.Row,
    *,
    revision_reason: str | None,
) -> dict[str, str]:
    title = _one_line(str(task["topic_title"] or task["research_question"] or "行业判断选题"))
    question = _one_line(
        str(task["research_question"] or "这件事会如何影响决策？")
    ).rstrip("。！？?!")
    audience = _one_line(str(task["audience"] or "品牌方、原料商及企业决策者"))
    dimensions = _json_list(str(task["selection_dimensions_json"] or "[]"))
    dimension_text = "、".join(_one_line(str(item)) for item in dimensions[:3] if str(item).strip())
    evidence = _json_list(str(task["known_evidence_json"] or "[]"))
    gaps = _bullet_fragments(str(task["evidence_gaps"] or ""))
    if not gaps:
        gaps = ["补充具体的一手页面，确认关键事实、适用范围与时间点。"]
    action_implications = (
        "先把公众号讨论拆回可核查的事实问题；再根据监管、科研、市场或企业原始材料，"
        "判断是否需要调整产品、供应链、合规沟通或内容表达。"
    )
    key_judgment = (
        f"当前能确认的是：这一问题已由 {len(evidence)} 条公众号发现线索带入审阅；"
        "尚不能据此确认任何监管、科研、功效或市场结论。对"
        f"{audience}而言，真正需要判断的不是“有没有热点”，而是哪些一手事实会改变决策。"
    )
    uncertainty_notes = (
        "本版未调用联网研究 Agent，也没有访问公开网页。所有带入公众号材料均为发现源；"
        "涉及监管、功效、科研、市场数据和企业行动的内容必须补充具体一手来源，"
        "否则只能保留“待补证”表述。"
    )
    dimension_sentence = (
        f"建议优先按 {dimension_text} 三个维度核查。"
        if dimension_text
        else "建议先按事实边界、业务影响和行动窗口三个维度核查。"
    )
    gap_lines = "\n".join(f"- 待补证：{gap}" for gap in gaps[:4])
    reason_line = (
        f"\n> 本次版本依据人工退回意见重新组织：{revision_reason}\n"
        if revision_reason
        else ""
    )
    article_body = (
        f"# {title}\n\n"
        "> **离线 Demo 初稿（未联网检索）**\n"
        "> 本稿只根据已确认的研究任务书和带入的公众号发现线索组织文章结构，"
        "不新增外部事实，也不能替代深度研究。\n"
        f"{reason_line}\n"
        "## 先给结论\n\n"
        f"{key_judgment}\n\n"
        "## 为什么这个问题值得现在判断\n\n"
        f"研究问题是：{question}。现阶段更适合把公众号内容作为发现入口，"
        "而不是直接把它当成结论。"
        f"{dimension_sentence}\n\n"
        "## 企业决策者应先回答的三个问题\n\n"
        "1. 这件事是否有明确的一手文件、原始论文、企业公告或数据原文可回溯？\n"
        "2. 若事实成立，它会影响产品立项、原料选择、合规表达还是渠道沟通？\n"
        "3. 哪些判断仍缺时间范围、适用对象或反例，不能提前写成确定性结论？\n\n"
        "## 建议的下一步\n\n"
        f"{action_implications}\n\n"
        "## 待补证后再下判断\n\n"
        f"{gap_lines}\n\n"
        "## 说明\n\n"
        f"{uncertainty_notes}\n"
    )
    summary = "离线文章结构 Demo：仅使用任务书和已有发现线索。"
    if revision_reason:
        summary = f"{summary} 已根据人工退回原因生成新版本。"
    return {
        "title": title,
        "article_body": article_body,
        "key_judgment": key_judgment,
        "action_implications": action_implications,
        "uncertainty_notes": uncertainty_notes,
        "change_summary": summary,
    }


def _final_markdown_content(
    task: sqlite3.Row,
    draft: sqlite3.Row,
    sources: list[sqlite3.Row],
    *,
    review_note: str,
) -> str:
    lines = [
        "---",
        f"title: {json.dumps(str(draft['title']), ensure_ascii=False)}",
        f"research_task_id: {int(task['id'])}",
        f"draft_version: {int(draft['version'])}",
        "status: final_human_confirmed",
        f"generated_by: {json.dumps(str(draft['generated_by']), ensure_ascii=False)}",
        "---",
        "",
        str(draft["article_body"]).strip(),
        "",
        "## 人工审阅说明",
        "",
        review_note,
        "",
        "## 来源与证据状态",
        "",
    ]
    if not sources:
        lines.append("- 暂无来源记录；不得把任何事实主张写成已确认。")
    for source in sources:
        status = "已核实" if source["evidence_status"] == "verified" else "待补证"
        name = str(source["source_name"] or "未命名来源")
        title = str(source["title"] or "未命名材料")
        reference = str(source["url_or_reference"] or "无具体页面")
        lines.append(f"- [{status}] {name}｜{title}｜{reference}")
    lines.extend(
        [
            "",
            "> 本地 Markdown 归档仅代表人工确认的文章版本；不包含公众号草稿箱写入或发布授权。",
            "",
        ]
    )
    return "\n".join(lines)


def _final_markdown_path(database_path: Path, *, task_id: int, version: int) -> Path:
    return (
        database_path.parent
        / "article-drafts"
        / f"task-{task_id:04d}"
        / f"article-v{version:03d}-final.md"
    )


def _relative_to_database(database_path: Path, target: Path) -> str:
    return target.resolve().relative_to(database_path.parent.resolve()).as_posix()


def _write_new_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ResearchWorkflowError("该最终 Markdown 归档已存在，系统不会覆盖旧版本")
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary_path, path)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)
        raise ResearchWorkflowError(f"无法写入最终 Markdown：{exc}") from exc


def _clean_draft_text(field: str, value: str) -> str:
    cleaned = str(value or "").strip()
    maximum = MAX_DRAFT_FIELD_LENGTHS[field]
    labels = {
        "title": "文章标题",
        "article_body": "文章正文",
        "key_judgment": "核心判断",
        "action_implications": "行动启示",
        "uncertainty_notes": "不确定性说明",
        "revision_reason": "退回或修订原因",
        "review_note": "最终审阅说明",
    }
    label = labels[field]
    if not cleaned:
        raise ResearchWorkflowError(f"{label}不能为空")
    if len(cleaned) > maximum:
        raise ResearchWorkflowError(f"{label}不能超过 {maximum} 个字符")
    return cleaned


def _one_line(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _bullet_fragments(value: str) -> list[str]:
    fragments = [
        _one_line(part)
        for part in re.split(r"[\r\n；;。]+", str(value or ""))
    ]
    return [fragment for fragment in fragments if fragment]


def _clean_text(field: str, value: str) -> str:
    cleaned = str(value or "").strip()
    maximum = MAX_FIELD_LENGTHS[field]
    if len(cleaned) > maximum:
        raise ResearchWorkflowError(
            f"{RESEARCH_TASK_FIELD_LABELS[field]}不能超过 {maximum} 个字符"
        )
    return cleaned


def _clean_dimensions(values: list[str]) -> list[str]:
    if not isinstance(values, list):
        raise ResearchWorkflowError("selection_dimensions must be a list")
    cleaned: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in cleaned:
            continue
        if len(text) > 200:
            raise ResearchWorkflowError("单个判断维度不能超过 200 个字符")
        cleaned.append(text)
    if len(cleaned) > 12:
        raise ResearchWorkflowError("判断维度不能超过 12 项")
    return cleaned


def _record_event(
    connection: sqlite3.Connection,
    task_id: int,
    event_type: str,
    *,
    actor: str,
    draft_id: int | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    reason: str | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO research_events(
            research_task_id, draft_id, event_type, actor, from_status, to_status,
            reason, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            draft_id,
            event_type,
            actor,
            from_status,
            to_status,
            reason,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def _event_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    payload["payload"] = _json_object(
        str(payload.pop("payload_json", "{}") or "{}")
    )
    return payload


def _record_value(
    record: Mapping[str, object] | sqlite3.Row,
    key: str,
    default: object = None,
) -> object:
    try:
        return record[key]
    except (IndexError, KeyError):
        return default


def _json_list_value(
    record: Mapping[str, object] | sqlite3.Row,
    *,
    parsed_key: str,
    raw_key: str,
) -> list[Any]:
    parsed = _record_value(record, parsed_key)
    if isinstance(parsed, list):
        return parsed
    return _json_list(str(_record_value(record, raw_key, "[]") or "[]"))


def _json_list(raw: str) -> list[Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
