from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from .database import connect_database, transaction
from .schema import initialize_schema


SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


class MonitoringCycleError(ValueError):
    """Raised when a monitoring-cycle operation violates the editorial boundary."""


@dataclass(frozen=True)
class MonitoringCycleResult:
    id: int
    slug: str
    label: str
    start_date: str | None
    end_date: str | None
    data_cutoff: str | None
    status: str
    closed_at: str | None


def list_monitoring_cycles(database_path: Path) -> list[dict[str, object]]:
    initialize_schema(database_path)
    connection = connect_database(database_path, readonly=True)
    try:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT mc.*,
                       COUNT(DISTINCT mca.article_id) AS article_count,
                       COUNT(DISTINCT tc.id) AS cluster_count,
                       COUNT(DISTINCT s.id) AS signal_count,
                       COUNT(DISTINCT c.id) AS candidate_count,
                       MAX(ce.version) AS latest_export_version,
                       MAX(ce.created_at) AS latest_export_at,
                       (
                           SELECT ce2.relative_path
                           FROM cycle_exports ce2
                           WHERE ce2.cycle_id = mc.id
                           ORDER BY ce2.version DESC LIMIT 1
                       ) AS latest_export_path
                FROM monitoring_cycles mc
                LEFT JOIN monitoring_cycle_articles mca ON mca.cycle_id = mc.id
                LEFT JOIN topic_clusters tc ON tc.cycle_id = mc.id
                LEFT JOIN industry_signals s ON s.cluster_id = tc.id
                LEFT JOIN topic_candidates c ON c.signal_id = s.id
                LEFT JOIN cycle_exports ce ON ce.cycle_id = mc.id
                GROUP BY mc.id
                ORDER BY (mc.status = 'open') DESC, mc.end_date DESC, mc.id DESC
                """
            )
        ]
    finally:
        connection.close()


def get_monitoring_cycle(
    connection: sqlite3.Connection,
    *,
    cycle_id: int | None = None,
    slug: str | None = None,
) -> sqlite3.Row | None:
    if cycle_id is not None:
        return connection.execute(
            "SELECT * FROM monitoring_cycles WHERE id = ?", (cycle_id,)
        ).fetchone()
    if slug:
        return connection.execute(
            "SELECT * FROM monitoring_cycles WHERE slug = ?", (slug,)
        ).fetchone()
    return connection.execute(
        """
        SELECT * FROM monitoring_cycles
        ORDER BY (status = 'open') DESC, end_date DESC, id DESC
        LIMIT 1
        """
    ).fetchone()


def create_monitoring_cycle(
    database_path: Path,
    *,
    slug: str,
    label: str,
    start_date: str,
    end_date: str,
    data_cutoff: str | None = None,
    actor: str = "operator",
) -> MonitoringCycleResult:
    cleaned_slug = slug.strip().lower()
    cleaned_label = label.strip()
    if not SLUG_PATTERN.fullmatch(cleaned_slug):
        raise MonitoringCycleError(
            "期次标识只能使用 2—64 位小写字母、数字、点、下划线或连字符"
        )
    if not cleaned_label or len(cleaned_label) > 120:
        raise MonitoringCycleError("期次名称必须为 1—120 个字符")
    start = _parse_date(start_date, "开始日期")
    end = _parse_date(end_date, "结束日期")
    if start > end:
        raise MonitoringCycleError("开始日期不能晚于结束日期")
    cutoff = _parse_datetime(data_cutoff, "数据截止时间") if data_cutoff else None

    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            open_cycle = connection.execute(
                "SELECT slug FROM monitoring_cycles WHERE status = 'open'"
            ).fetchone()
            if open_cycle is not None:
                raise MonitoringCycleError(
                    f"当前仍有开放期次 {open_cycle['slug']}；请先关闭后再新建下一期"
                )
            try:
                cursor = connection.execute(
                    f"""
                    INSERT INTO monitoring_cycles(
                        slug, label, start_date, end_date, data_cutoff, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'open', ({SQL_NOW}), ({SQL_NOW}))
                    """,
                    (
                        cleaned_slug,
                        cleaned_label,
                        start.isoformat(),
                        end.isoformat(),
                        cutoff,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise MonitoringCycleError(f"期次标识已存在：{cleaned_slug}") from exc
            cycle_id = int(cursor.lastrowid)
            _record_cycle_event(
                connection,
                cycle_id,
                "cycle_created",
                actor=actor,
                payload={
                    "slug": cleaned_slug,
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                },
            )
            row = get_monitoring_cycle(connection, cycle_id=cycle_id)
            if row is None:
                raise MonitoringCycleError("新建期次后无法读取记录")
        return _cycle_result(row)
    finally:
        connection.close()


def close_monitoring_cycle(
    database_path: Path,
    cycle_id: int,
    *,
    reason: str | None = None,
    actor: str = "operator",
) -> MonitoringCycleResult:
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            row = get_monitoring_cycle(connection, cycle_id=cycle_id)
            if row is None:
                raise MonitoringCycleError(f"监测期次 {cycle_id} 不存在")
            if row["status"] != "open":
                raise MonitoringCycleError("该监测期次已经关闭")
            connection.execute(
                f"""
                UPDATE monitoring_cycles SET
                    status = 'closed', closed_at = ({SQL_NOW}), updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (cycle_id,),
            )
            _record_cycle_event(
                connection,
                cycle_id,
                "cycle_closed",
                actor=actor,
                reason=(reason or "").strip() or None,
            )
            updated = get_monitoring_cycle(connection, cycle_id=cycle_id)
            if updated is None:
                raise MonitoringCycleError("关闭期次后无法读取记录")
        return _cycle_result(updated)
    finally:
        connection.close()


def ensure_import_cycle(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    published_at_values: Iterable[str | None],
) -> sqlite3.Row | None:
    """Return the single open cycle, creating the first cycle for an initial import."""
    published_values = list(published_at_values)
    open_cycle = connection.execute(
        "SELECT * FROM monitoring_cycles WHERE status = 'open' ORDER BY id LIMIT 1"
    ).fetchone()
    if open_cycle is not None:
        return open_cycle

    any_cycle = connection.execute("SELECT 1 FROM monitoring_cycles LIMIT 1").fetchone()
    if any_cycle is not None:
        raise MonitoringCycleError("当前没有开放监测期次；请先新建下一期再导入数据")

    dates = sorted(
        value[:10]
        for value in published_values
        if value and len(value) >= 10
    )
    if not dates:
        return None
    start_date = dates[0]
    end_date = dates[-1]
    month = start_date[:7]
    if source_kind == "xlsx":
        slug = f"demo-{month}"
        label = f"Demo {month}"
    else:
        slug = f"cycle-{month}"
        label = f"监测期次 {month}"
    cursor = connection.execute(
        f"""
        INSERT INTO monitoring_cycles(
            slug, label, start_date, end_date, data_cutoff, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'open', ({SQL_NOW}), ({SQL_NOW}))
        """,
        (
            slug,
            label,
            start_date,
            end_date,
            max((value for value in published_values if value), default=None),
        ),
    )
    cycle_id = int(cursor.lastrowid)
    _record_cycle_event(
        connection,
        cycle_id,
        "cycle_created",
        actor="importer",
        payload={"source_kind": source_kind, "automatic": True},
    )
    return get_monitoring_cycle(connection, cycle_id=cycle_id)


def attach_article_to_cycle(
    connection: sqlite3.Connection,
    cycle: sqlite3.Row,
    article_id: int,
    published_at: str | None,
) -> str:
    published_date = published_at[:10] if published_at and len(published_at) >= 10 else None
    start_date = cycle["start_date"]
    end_date = cycle["end_date"]
    role = "in_period"
    if published_date and start_date and published_date < start_date:
        role = "context"
    if published_date and end_date and published_date > end_date:
        role = "context"
    connection.execute(
        """
        INSERT INTO monitoring_cycle_articles(cycle_id, article_id, role)
        VALUES (?, ?, ?)
        ON CONFLICT(cycle_id, article_id) DO UPDATE SET role = excluded.role
        """,
        (cycle["id"], article_id, role),
    )
    return role


def _record_cycle_event(
    connection: sqlite3.Connection,
    cycle_id: int,
    event_type: str,
    *,
    actor: str,
    reason: str | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO monitoring_cycle_events(
            cycle_id, event_type, actor, reason, payload_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            cycle_id,
            event_type,
            actor.strip() or "operator",
            reason,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def _cycle_result(row: sqlite3.Row) -> MonitoringCycleResult:
    return MonitoringCycleResult(
        id=int(row["id"]),
        slug=str(row["slug"]),
        label=str(row["label"]),
        start_date=row["start_date"],
        end_date=row["end_date"],
        data_cutoff=row["data_cutoff"],
        status=str(row["status"]),
        closed_at=row["closed_at"],
    )


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise MonitoringCycleError(f"{label}必须使用 YYYY-MM-DD 格式") from exc


def _parse_datetime(value: str, label: str) -> str:
    cleaned = value.strip()
    try:
        datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MonitoringCycleError(f"{label}必须使用 ISO 8601 格式") from exc
    return cleaned
