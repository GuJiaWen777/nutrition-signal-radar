from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .database import connect_database, transaction
from .schema import initialize_schema


SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "candidate_pool": ("pending_decision", "archived"),
    "pending_decision": ("candidate_pool", "package_drafting", "archived"),
    "package_drafting": ("pending_decision", "pending_confirmation", "archived"),
    "pending_confirmation": ("package_drafting", "selected", "archived"),
    "selected": (),
    "archived": (),
}


class TopicWorkflowError(ValueError):
    """Raised when an editorial operation violates a workflow guard."""


class TopicTransitionError(TopicWorkflowError):
    """Raised when a candidate transition would bypass a human gate."""


@dataclass(frozen=True)
class TopicTransitionResult:
    candidate_id: int
    from_status: str
    to_status: str
    package_id: int | None
    package_status: str | None


@dataclass(frozen=True)
class CandidateTrackResult:
    candidate_id: int
    from_track: str
    to_track: str
    track_source: str
    track_reason: str


@dataclass(frozen=True)
class CandidateEditResult:
    candidate_id: int
    changed_fields: tuple[str, ...]


@dataclass(frozen=True)
class TopicPackageEditResult:
    candidate_id: int
    package_id: int
    changed_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]


PACKAGE_FIELD_LABELS = {
    "title": "最终选题标题",
    "audience": "目标读者",
    "core_question": "核心问题",
    "why_now": "为什么现在",
    "industry_angle": "差异化行业判断角度",
    "selection_dimensions": "选题判断维度",
    "evidence": "公众号线索",
    "evidence_gaps": "待补证事实",
    "research_directions": "研究方向",
    "risk_notes": "风险提示",
}


def topic_package_missing_fields(package: Mapping[str, object] | sqlite3.Row) -> tuple[str, ...]:
    missing: list[str] = []
    for field in (
        "title",
        "audience",
        "core_question",
        "why_now",
        "industry_angle",
        "evidence_gaps",
        "research_directions",
        "risk_notes",
    ):
        if not str(_record_value(package, field, "") or "").strip():
            missing.append(field)
    if not _json_list_value(package, "selection_dimensions", "selection_dimensions_json"):
        missing.append("selection_dimensions")
    if not _json_list_value(package, "evidence", "evidence_json"):
        missing.append("evidence")
    return tuple(missing)


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
    parsed_key: str,
    raw_key: str,
) -> list[object]:
    value = _record_value(record, parsed_key)
    if isinstance(value, list):
        return value
    raw = _record_value(record, raw_key, "[]")
    if not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _ensure_cycle_open(
    record: Mapping[str, object] | sqlite3.Row,
    *,
    error_type: type[TopicWorkflowError] = TopicWorkflowError,
) -> None:
    cycle_status = _record_value(record, "cycle_status")
    if cycle_status is None:
        raise error_type("该候选未归属监测期次，不能修改")
    if cycle_status != "open":
        raise error_type("监测期次已关闭；历史选题只能查看，不能修改")


def transition_candidate(
    database_path: Path,
    candidate_id: int,
    to_status: str,
    *,
    reason: str | None = None,
    actor: str = "operator",
) -> TopicTransitionResult:
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            candidate = connection.execute(
                """
                SELECT c.*, s.cluster_id, s.evidence_status AS signal_evidence_status,
                       s.data_gaps, s.business_implications,
                       mc.status AS cycle_status
                FROM topic_candidates c
                JOIN industry_signals s ON s.id = c.signal_id
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                JOIN monitoring_cycles mc ON mc.id = tc.cycle_id
                WHERE c.id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                raise TopicTransitionError(f"Candidate {candidate_id} does not exist")
            _ensure_cycle_open(candidate, error_type=TopicTransitionError)

            from_status = str(candidate["status"])
            if to_status not in ALLOWED_TRANSITIONS.get(from_status, ()):
                raise TopicTransitionError(
                    f"Transition {from_status} -> {to_status} is not allowed"
                )
            cleaned_reason = (reason or "").strip()
            if to_status == "archived" and not cleaned_reason:
                raise TopicTransitionError("Archiving requires a reason")

            package_id: int | None = None
            package_status: str | None = None
            if to_status in {"package_drafting", "pending_confirmation", "selected"}:
                package_id = _ensure_topic_package(connection, candidate)

            if to_status == "package_drafting" and package_id is not None:
                connection.execute(
                    f"""
                    UPDATE topic_packages SET
                        status = 'drafting', submitted_at = NULL,
                        confirmed_at = NULL, updated_at = ({SQL_NOW})
                    WHERE id = ?
                    """,
                    (package_id,),
                )
                package_status = "drafting"
            elif to_status == "pending_confirmation" and package_id is not None:
                package = connection.execute(
                    "SELECT * FROM topic_packages WHERE id = ?", (package_id,)
                ).fetchone()
                if package is None or package["status"] != "drafting":
                    raise TopicTransitionError(
                        "Topic package must be in drafting before submission"
                    )
                missing_fields = topic_package_missing_fields(package)
                if missing_fields:
                    labels = "、".join(PACKAGE_FIELD_LABELS[field] for field in missing_fields)
                    raise TopicTransitionError(f"选题包尚未完整：{labels}")
                connection.execute(
                    f"""
                    UPDATE topic_packages SET
                        status = 'pending_confirmation', confirmed_at = NULL,
                        submitted_at = ({SQL_NOW}), updated_at = ({SQL_NOW})
                    WHERE id = ?
                    """,
                    (package_id,),
                )
                package_status = "pending_confirmation"
            elif to_status == "selected" and package_id is not None:
                current_package = connection.execute(
                    "SELECT * FROM topic_packages WHERE id = ?", (package_id,)
                ).fetchone()
                if current_package is None or current_package["status"] != "pending_confirmation":
                    raise TopicTransitionError(
                        "Topic package must be pending confirmation before selection"
                    )
                missing_fields = topic_package_missing_fields(current_package)
                if missing_fields:
                    labels = "、".join(PACKAGE_FIELD_LABELS[field] for field in missing_fields)
                    raise TopicTransitionError(f"选题包尚未完整：{labels}")
                connection.execute(
                    f"""
                    UPDATE topic_packages SET
                        status = 'confirmed', confirmed_at = ({SQL_NOW}),
                        updated_at = ({SQL_NOW})
                    WHERE id = ?
                    """,
                    (package_id,),
                )
                package_status = "confirmed"

            connection.execute(
                f"""
                UPDATE topic_candidates SET
                    status = ?, archived_reason = ?, updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (
                    to_status,
                    cleaned_reason if to_status == "archived" else None,
                    candidate_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO topic_events(
                    candidate_id, event_type, actor, from_status, to_status, reason, payload_json
                ) VALUES (?, 'status_transition', ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    actor.strip() or "operator",
                    from_status,
                    to_status,
                    cleaned_reason or None,
                    json.dumps(
                        {"package_id": package_id, "package_status": package_status},
                        ensure_ascii=False,
                    ),
                ),
            )

            if package_id is None:
                package = connection.execute(
                    "SELECT id, status FROM topic_packages WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()
                if package is not None:
                    package_id = int(package["id"])
                    package_status = str(package["status"])

        return TopicTransitionResult(
            candidate_id=candidate_id,
            from_status=from_status,
            to_status=to_status,
            package_id=package_id,
            package_status=package_status,
        )
    finally:
        connection.close()


def set_candidate_review_track(
    database_path: Path,
    candidate_id: int,
    review_track: str,
    *,
    reason: str | None = None,
    actor: str = "operator",
) -> CandidateTrackResult:
    if review_track not in {"research", "watch"}:
        raise TopicWorkflowError("Review track must be research or watch")

    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            candidate = connection.execute(
                """
                SELECT c.id, c.status, c.review_track, c.track_source,
                       mc.status AS cycle_status
                FROM topic_candidates c
                JOIN industry_signals s ON s.id = c.signal_id
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                JOIN monitoring_cycles mc ON mc.id = tc.cycle_id
                WHERE c.id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                raise TopicWorkflowError(f"Candidate {candidate_id} does not exist")
            _ensure_cycle_open(candidate)
            if candidate["status"] in {"selected", "archived"}:
                raise TopicWorkflowError("Selected or archived candidates cannot change review track")

            from_track = str(candidate["review_track"])
            cleaned_reason = (reason or "").strip() or "人工调整审阅队列"
            connection.execute(
                f"""
                UPDATE topic_candidates SET
                    review_track = ?, track_source = 'manual', track_reason = ?,
                    updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (review_track, cleaned_reason, candidate_id),
            )
            connection.execute(
                """
                INSERT INTO topic_events(
                    candidate_id, event_type, actor, reason, payload_json
                ) VALUES (?, 'review_track_changed', ?, ?, ?)
                """,
                (
                    candidate_id,
                    actor.strip() or "operator",
                    cleaned_reason,
                    json.dumps(
                        {
                            "from_track": from_track,
                            "to_track": review_track,
                            "previous_source": candidate["track_source"],
                            "track_source": "manual",
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        return CandidateTrackResult(
            candidate_id=candidate_id,
            from_track=from_track,
            to_track=review_track,
            track_source="manual",
            track_reason=cleaned_reason,
        )
    finally:
        connection.close()


def update_candidate_editorial_fields(
    database_path: Path,
    candidate_id: int,
    *,
    proposed_title: str,
    audience: str,
    core_question: str,
    why_now: str,
    angle: str,
    actor: str = "operator",
) -> CandidateEditResult:
    values = {
        "proposed_title": proposed_title.strip(),
        "audience": audience.strip(),
        "core_question": core_question.strip(),
        "why_now": why_now.strip(),
        "angle": angle.strip(),
    }
    limits = {
        "proposed_title": 240,
        "audience": 160,
        "core_question": 1_000,
        "why_now": 1_500,
        "angle": 1_500,
    }
    for field, value in values.items():
        if not value:
            raise TopicWorkflowError(f"{field} must not be empty")
        if len(value) > limits[field]:
            raise TopicWorkflowError(f"{field} exceeds {limits[field]} characters")

    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            candidate = connection.execute(
                """
                SELECT c.*, mc.status AS cycle_status
                FROM topic_candidates c
                JOIN industry_signals s ON s.id = c.signal_id
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                JOIN monitoring_cycles mc ON mc.id = tc.cycle_id
                WHERE c.id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                raise TopicWorkflowError(f"Candidate {candidate_id} does not exist")
            _ensure_cycle_open(candidate)
            if candidate["status"] in {"pending_confirmation", "selected", "archived"}:
                raise TopicWorkflowError(
                    "待确认、已选题或已归档的候选不能直接编辑；请先退回选题包完善"
                )

            changed_fields = tuple(
                field for field, value in values.items() if str(candidate[field]) != value
            )
            if not changed_fields:
                return CandidateEditResult(candidate_id, ())

            connection.execute(
                f"""
                UPDATE topic_candidates SET
                    proposed_title = ?, audience = ?, core_question = ?,
                    why_now = ?, angle = ?, editor_modified = 1,
                    updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (
                    values["proposed_title"],
                    values["audience"],
                    values["core_question"],
                    values["why_now"],
                    values["angle"],
                    candidate_id,
                ),
            )
            connection.execute(
                f"""
                UPDATE topic_packages SET
                    title = ?, audience = ?, core_question = ?, why_now = ?,
                    industry_angle = ?, research_directions = ?,
                    updated_at = ({SQL_NOW})
                WHERE candidate_id = ? AND status = 'drafting'
                """,
                (
                    values["proposed_title"],
                    values["audience"],
                    values["core_question"],
                    values["why_now"],
                    values["angle"],
                    values["core_question"],
                    candidate_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO topic_events(
                    candidate_id, event_type, actor, payload_json
                ) VALUES (?, 'candidate_edited', ?, ?)
                """,
                (
                    candidate_id,
                    actor.strip() or "operator",
                    json.dumps({"changed_fields": list(changed_fields)}, ensure_ascii=False),
                ),
            )
        return CandidateEditResult(candidate_id, changed_fields)
    finally:
        connection.close()


def update_topic_package(
    database_path: Path,
    candidate_id: int,
    *,
    title: str,
    audience: str,
    core_question: str,
    why_now: str,
    industry_angle: str,
    selection_dimensions: list[str],
    evidence_gaps: str,
    research_directions: str,
    risk_notes: str,
    actor: str = "operator",
) -> TopicPackageEditResult:
    values = {
        "title": title.strip(),
        "audience": audience.strip(),
        "core_question": core_question.strip(),
        "why_now": why_now.strip(),
        "industry_angle": industry_angle.strip(),
        "evidence_gaps": evidence_gaps.strip(),
        "research_directions": research_directions.strip(),
        "risk_notes": risk_notes.strip(),
    }
    limits = {
        "title": 240,
        "audience": 160,
        "core_question": 1_500,
        "why_now": 2_000,
        "industry_angle": 2_000,
        "evidence_gaps": 4_000,
        "research_directions": 4_000,
        "risk_notes": 3_000,
    }
    for field, value in values.items():
        if len(value) > limits[field]:
            raise TopicWorkflowError(f"{field} exceeds {limits[field]} characters")

    if not isinstance(selection_dimensions, list):
        raise TopicWorkflowError("selection_dimensions must be a list")
    dimensions: list[str] = []
    for raw_dimension in selection_dimensions:
        dimension = str(raw_dimension).strip()
        if not dimension or dimension in dimensions:
            continue
        if len(dimension) > 80:
            raise TopicWorkflowError("Each selection dimension must be at most 80 characters")
        dimensions.append(dimension)
    if len(dimensions) > 12:
        raise TopicWorkflowError("A topic package can have at most 12 selection dimensions")

    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            row = connection.execute(
                """
                SELECT p.*, c.status AS candidate_status,
                       mc.status AS cycle_status
                FROM topic_packages p
                JOIN topic_candidates c ON c.id = p.candidate_id
                JOIN industry_signals s ON s.id = c.signal_id
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                JOIN monitoring_cycles mc ON mc.id = tc.cycle_id
                WHERE p.candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise TopicWorkflowError(
                    "Topic package does not exist; move the candidate to package drafting first"
                )
            _ensure_cycle_open(row)
            if row["candidate_status"] != "package_drafting" or row["status"] != "drafting":
                raise TopicWorkflowError(
                    "Only a topic package in 选题包完善 can be edited"
                )

            current_dimensions = [
                str(value)
                for value in _json_list_value(
                    row, "selection_dimensions", "selection_dimensions_json"
                )
            ]
            changed_fields = [
                field for field, value in values.items() if str(row[field] or "") != value
            ]
            if current_dimensions != dimensions:
                changed_fields.append("selection_dimensions")

            if changed_fields:
                connection.execute(
                    f"""
                    UPDATE topic_packages SET
                        title = ?, audience = ?, core_question = ?, why_now = ?,
                        industry_angle = ?, selection_dimensions_json = ?,
                        evidence_gaps = ?, research_directions = ?, risk_notes = ?,
                        editor_modified = 1, updated_at = ({SQL_NOW})
                    WHERE id = ?
                    """,
                    (
                        values["title"],
                        values["audience"],
                        values["core_question"],
                        values["why_now"],
                        values["industry_angle"],
                        json.dumps(dimensions, ensure_ascii=False),
                        values["evidence_gaps"],
                        values["research_directions"],
                        values["risk_notes"],
                        row["id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO topic_events(
                        candidate_id, event_type, actor, payload_json
                    ) VALUES (?, 'topic_package_edited', ?, ?)
                    """,
                    (
                        candidate_id,
                        actor.strip() or "operator",
                        json.dumps(
                            {
                                "package_id": int(row["id"]),
                                "changed_fields": changed_fields,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )

            updated = connection.execute(
                "SELECT * FROM topic_packages WHERE id = ?", (row["id"],)
            ).fetchone()
            if updated is None:
                raise TopicWorkflowError("Topic package disappeared during update")
            missing_fields = topic_package_missing_fields(updated)

        return TopicPackageEditResult(
            candidate_id=candidate_id,
            package_id=int(row["id"]),
            changed_fields=tuple(changed_fields),
            missing_fields=missing_fields,
        )
    finally:
        connection.close()


def _ensure_topic_package(connection: sqlite3.Connection, candidate: sqlite3.Row) -> int:
    existing = connection.execute(
        "SELECT id FROM topic_packages WHERE candidate_id = ?", (candidate["id"],)
    ).fetchone()
    if existing is not None:
        return int(existing["id"])

    evidence = [
        dict(row)
        for row in connection.execute(
            """
            SELECT a.id AS article_id, a.title, a.url, a.published_at,
                   sa.name AS account_name, ca.is_primary_evidence
            FROM cluster_articles ca
            JOIN source_articles a ON a.id = ca.article_id
            JOIN source_accounts sa ON sa.id = a.account_id
            WHERE ca.cluster_id = ?
            ORDER BY ca.is_primary_evidence DESC, a.read_count DESC, a.id
            LIMIT 8
            """,
            (candidate["cluster_id"],),
        )
    ]
    risks = json.loads(candidate["risk_flags_json"] or "[]")
    dimensions = _selection_dimensions(str(candidate["proposed_title"]))
    cursor = connection.execute(
        f"""
        INSERT INTO topic_packages(
            candidate_id, title, audience, core_question, why_now, industry_angle,
            selection_dimensions_json, evidence_json, evidence_status,
            evidence_gaps, research_directions, risk_notes, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'needs_evidence', ?, ?, ?, 'drafting', ({SQL_NOW}), ({SQL_NOW}))
        """,
        (
            candidate["id"],
            candidate["proposed_title"],
            candidate["audience"],
            candidate["core_question"],
            candidate["why_now"],
            candidate["angle"],
            json.dumps(dimensions, ensure_ascii=False),
            json.dumps(evidence, ensure_ascii=False),
            candidate["data_gaps"],
            candidate["core_question"],
            "；".join(str(risk) for risk in risks),
        ),
    )
    return int(cursor.lastrowid)


def _selection_dimensions(title: str) -> list[str]:
    dimensions = ["核心趋势与市场信号", "业务含义"]
    if any(keyword in title for keyword in ("监管", "法规", "审批", "合规", "召回")):
        dimensions.insert(0, "监管与合规")
    if any(keyword in title for keyword in ("科研", "临床", "药物", "技术", "证据")):
        dimensions.insert(0, "科研证据与科学叙事")
    if any(keyword in title for keyword in ("品牌", "产品", "市场", "销售", "渠道")):
        dimensions.insert(0, "产品与市场动作")
    return list(dict.fromkeys(dimensions))


def set_signal_priority(
    database_path: Path,
    signal_id: int,
    priority: int | None,
    *,
    actor: str = "operator",
) -> dict[str, object]:
    """Set the human ordering override for one signal; None restores rule order.

    Signals with a priority are listed first in the brief and the publishable
    article prompt, in ascending priority order; unset signals keep their rule
    score order.  Priority survives rule rebuilds (the deterministic upsert
    never overwrites the column) and is cleared if the signal disappears.
    """
    if priority is not None and priority < 0:
        raise ValueError("priority must be a non-negative integer or None")
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            row = connection.execute(
                """
                SELECT s.id, s.signal_title, tc.cycle_id, mc.status AS cycle_status
                FROM industry_signals s
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                JOIN monitoring_cycles mc ON mc.id = tc.cycle_id
                WHERE s.id = ?
                """,
                (signal_id,),
            ).fetchone()
            if row is None:
                raise TopicWorkflowError(f"行业信号 {signal_id} 不存在")
            _ensure_cycle_open(row)
            connection.execute(
                f"""
                UPDATE industry_signals SET priority = ?, updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (priority, signal_id),
            )
            connection.execute(
                """
                INSERT INTO monitoring_cycle_events(cycle_id, event_type, actor, payload_json)
                VALUES (?, 'signal_priority_set', ?, ?)
                """,
                (
                    int(row["cycle_id"]),
                    (actor or "operator").strip() or "operator",
                    json.dumps(
                        {
                            "signal_id": signal_id,
                            "priority": priority,
                            "signal_title": str(row["signal_title"]),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
        return {
            "signal_id": signal_id,
            "priority": priority,
            "cycle_id": int(row["cycle_id"]),
        }
    finally:
        connection.close()
