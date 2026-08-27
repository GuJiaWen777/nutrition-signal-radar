"""Versioned, local weekly signal-brief exports.

The weekly brief is a horizontal scan of already generated industry signals. It
does not confirm facts, change topic status, create research tasks, call an
LLM, or publish anywhere.  A separate human-confirmed topic still starts the
vertical deep-research branch.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import uuid4

from .agent_gateway import AgentRunResult, PiAgentRequest, save_agent_exchange
from .article_contract import normalize_source_type
from .cycle_service import MonitoringCycleError, get_monitoring_cycle
from .database import connect_database, transaction
from .schema import initialize_schema
from .weekly_profiles import (
    DEFAULT_WEEKLY_ARTICLE_PROFILE,
    WeeklyArticleProfile,
    resolve_weekly_article_profile,
)


SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
WEEKLY_ARTICLE_GENERATOR = "pi_web_access_weekly_article_v1"
WEEKLY_ARTICLE_FILE_NAME = "weekly-article.md"

PUBLISHABLE_FORBIDDEN_MARKERS = (
    "待补证",
    "公众号发现线索",
    "内部状态",
    "人工审阅",
    "证据状态",
    "横向扫描草稿",
    "草稿",
    "待人工",
    "draft_for_human_review",
    "未核实",
    "未经核实",
    "尚未核实",
    "需要核实",
    "内部工作",
)


class WeeklyArticleError(ValueError):
    """Raised when a weekly article cannot be generated or confirmed safely."""


class WeeklyArticleRunner(Protocol):
    def run(self, request: PiAgentRequest) -> AgentRunResult: ...


@dataclass(frozen=True)
class WeeklySignalBriefResult:
    brief_id: int
    cycle_id: int
    cycle_slug: str
    version: int
    directory: Path
    markdown_path: Path
    manifest_path: Path
    signal_count: int
    source_article_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "brief_id": self.brief_id,
            "cycle_id": self.cycle_id,
            "cycle_slug": self.cycle_slug,
            "version": self.version,
            "directory": str(self.directory),
            "markdown_path": str(self.markdown_path),
            "manifest_path": str(self.manifest_path),
            "signal_count": self.signal_count,
            "source_article_count": self.source_article_count,
            "status": "draft_for_human_review",
        }


def list_weekly_signal_briefs(
    database_path: Path,
    *,
    cycle_id: int | None = None,
    cycle_slug: str | None = None,
) -> list[dict[str, object]]:
    """Return the immutable brief versions for one monitoring cycle."""

    if cycle_id is None and cycle_slug is None:
        raise ValueError("Provide one of cycle_id or cycle_slug")
    initialize_schema(database_path)
    database = Path(database_path).resolve()
    connection = connect_database(database, readonly=True)
    try:
        cycle = get_monitoring_cycle(
            connection,
            cycle_id=cycle_id,
            slug=cycle_slug,
        )
        if cycle is None:
            return []
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, cycle_id, version, relative_path, manifest_sha256,
                       file_count, generated_by, created_at
                FROM weekly_signal_briefs
                WHERE cycle_id = ?
                ORDER BY version DESC, id DESC
                """,
                (int(cycle["id"]),),
            )
        ]
    finally:
        connection.close()
    for row in rows:
        directory = database.parent / str(row["relative_path"])
        row["directory_absolute"] = str(directory)
        markdown = directory / "weekly-signal-brief.md"
        row["markdown_absolute_path"] = str(markdown)
        row["exists"] = markdown.is_file()
        row["cycle_id"] = int(cycle["id"])
        row["cycle_slug"] = str(cycle["slug"])
    return rows


def read_weekly_signal_brief(
    database_path: Path,
    *,
    cycle_id: int,
    version: int,
) -> dict[str, object] | None:
    """Read one immutable brief version and its markdown content."""

    if version < 1:
        raise ValueError("brief version must be at least 1")
    initialize_schema(database_path)
    database = Path(database_path).resolve()
    connection = connect_database(database, readonly=True)
    try:
        row = connection.execute(
            """
            SELECT id, cycle_id, version, relative_path, manifest_sha256,
                   file_count, generated_by, created_at
            FROM weekly_signal_briefs
            WHERE cycle_id = ? AND version = ?
            """,
            (cycle_id, version),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    payload = dict(row)
    directory = database.parent / str(payload["relative_path"])
    markdown = directory / "weekly-signal-brief.md"
    payload["directory_absolute"] = str(directory)
    payload["markdown_absolute_path"] = str(markdown)
    payload["exists"] = markdown.is_file()
    payload["markdown"] = markdown.read_text(encoding="utf-8") if markdown.is_file() else ""
    return payload


def edit_weekly_signal_brief(
    database_path: Path,
    *,
    cycle_id: int,
    version: int,
    markdown: str,
    actor: str = "operator",
) -> dict[str, object]:
    """Overwrite one brief version's markdown in place with an audit event.

    Signal briefs are human-review drafts: while the monitoring cycle is open,
    the operator may correct wording directly in the current version instead of
    creating a new vNNN.  The manifest hash and an audit event keep the edit
    traceable.  Closed cycles stay read-only.
    """

    cleaned = str(markdown or "").strip()
    if not cleaned:
        raise ValueError("信号快报正文不能为空")
    if len(cleaned) > 200_000:
        raise ValueError("信号快报正文不能超过 200000 个字符")
    if version < 1:
        raise ValueError("brief version must be at least 1")
    initialize_schema(database_path)
    database = Path(database_path).resolve()
    connection = connect_database(database)
    try:
        with transaction(connection):
            row = connection.execute(
                """
                SELECT id, cycle_id, version, relative_path
                FROM weekly_signal_briefs WHERE cycle_id = ? AND version = ?
                """,
                (cycle_id, version),
            ).fetchone()
            if row is None:
                raise ValueError(f"信号快报 v{version:03d} 不存在")
            cycle = get_monitoring_cycle(connection, cycle_id=int(row["cycle_id"]))
            if cycle is None or cycle["status"] != "open":
                raise MonitoringCycleError("监测期次已关闭；历史快报只能查看，不能编辑")

            directory = database.parent / str(row["relative_path"])
            markdown_path = directory / "weekly-signal-brief.md"
            if not markdown_path.is_file():
                raise ValueError("该版本缺少本地 Markdown 文件，无法编辑")
            old_sha256 = _sha256(markdown_path)
            _write_text_atomically(markdown_path, cleaned + "\n")
            new_sha256 = _sha256(markdown_path)

            manifest_path = directory / "manifest.json"
            manifest: dict[str, object] = {}
            if manifest_path.is_file():
                try:
                    decoded = json.loads(manifest_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    decoded = {}
                if isinstance(decoded, dict):
                    manifest = decoded
            files = manifest.get("files")
            if isinstance(files, list):
                updated_files = []
                for entry in files:
                    if isinstance(entry, Mapping) and entry.get("path") == "weekly-signal-brief.md":
                        updated_files.append(
                            {
                                "path": "weekly-signal-brief.md",
                                "sha256": new_sha256,
                                "bytes": markdown_path.stat().st_size,
                            }
                        )
                    else:
                        updated_files.append(dict(entry) if isinstance(entry, Mapping) else entry)
                manifest["files"] = updated_files
            manifest["edited"] = {
                "edited_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "edited_by": (actor or "operator").strip() or "operator",
                "markdown_sha256_before": old_sha256,
                "markdown_sha256_after": new_sha256,
            }
            _write_text_atomically(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            manifest_sha256 = _sha256(manifest_path)
            connection.execute(
                "UPDATE weekly_signal_briefs SET manifest_sha256 = ? WHERE id = ?",
                (manifest_sha256, int(row["id"])),
            )
            connection.execute(
                """
                INSERT INTO monitoring_cycle_events(cycle_id, event_type, actor, payload_json)
                VALUES (?, 'weekly_signal_brief_edited', ?, ?)
                """,
                (
                    cycle_id,
                    (actor or "operator").strip() or "operator",
                    json.dumps(
                        {
                            "version": version,
                            "markdown_sha256_before": old_sha256,
                            "markdown_sha256_after": new_sha256,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
    finally:
        connection.close()
    return {
        "brief_id": int(row["id"]),
        "cycle_id": cycle_id,
        "version": version,
        "manifest_sha256": manifest_sha256,
    }


def delete_weekly_signal_brief(
    database_path: Path,
    *,
    cycle_id: int,
    version: int,
    actor: str = "operator",
) -> dict[str, object]:
    """Remove one brief version and its files from an open monitoring cycle."""

    if version < 1:
        raise ValueError("brief version must be at least 1")
    initialize_schema(database_path)
    database = Path(database_path).resolve()
    connection = connect_database(database)
    relative_path = ""
    try:
        with transaction(connection):
            row = connection.execute(
                """
                SELECT id, cycle_id, version, relative_path
                FROM weekly_signal_briefs WHERE cycle_id = ? AND version = ?
                """,
                (cycle_id, version),
            ).fetchone()
            if row is None:
                raise ValueError(f"信号快报 v{version:03d} 不存在")
            cycle = get_monitoring_cycle(connection, cycle_id=int(row["cycle_id"]))
            if cycle is None or cycle["status"] != "open":
                raise MonitoringCycleError("监测期次已关闭；历史快报只能查看，不能删除")
            relative_path = str(row["relative_path"])
            connection.execute(
                "DELETE FROM weekly_signal_briefs WHERE id = ?", (int(row["id"]),)
            )
            connection.execute(
                """
                INSERT INTO monitoring_cycle_events(cycle_id, event_type, actor, payload_json)
                VALUES (?, 'weekly_signal_brief_deleted', ?, ?)
                """,
                (
                    cycle_id,
                    (actor or "operator").strip() or "operator",
                    json.dumps(
                        {"version": version, "relative_path": relative_path},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
    finally:
        connection.close()
    shutil.rmtree(database.parent / relative_path, ignore_errors=True)
    return {"cycle_id": cycle_id, "version": version, "deleted": True}


def edit_weekly_article(
    database_path: Path,
    *,
    cycle_id: int,
    version: int,
    markdown: str,
    actor: str = "operator",
) -> dict[str, object]:
    """Overwrite one weekly article version in place with an audit event.

    Only draft articles can be edited; a confirmed (final) article must be
    reopened first.  The publishable contract still applies to human edits:
    internal markers such as “待补证” are rejected, and the title is derived
    from the first ``#`` heading of the edited text.
    """

    cleaned = str(markdown or "").strip()
    if not cleaned:
        raise ValueError("可发布文章正文不能为空")
    if len(cleaned) > 200_000:
        raise ValueError("可发布文章正文不能超过 200000 个字符")
    forbidden = _forbidden_markers(cleaned)
    if forbidden:
        raise WeeklyArticleError(
            "编辑后的正文包含不可发布的内部标记：" + "、".join(sorted(forbidden))
        )
    if version < 1:
        raise ValueError("article version must be at least 1")
    initialize_schema(database_path)
    database = Path(database_path).resolve()
    connection = connect_database(database)
    try:
        with transaction(connection):
            row = connection.execute(
                """
                SELECT id, cycle_id, version, title, relative_path, status
                FROM weekly_articles WHERE cycle_id = ? AND version = ?
                """,
                (cycle_id, version),
            ).fetchone()
            if row is None:
                raise WeeklyArticleError(f"可发布文章 v{version:03d} 不存在")
            if str(row["status"]) != "draft_for_human_review":
                raise WeeklyArticleError("已定稿文章不能直接编辑；请先「回到待编辑」")
            cycle = get_monitoring_cycle(connection, cycle_id=int(row["cycle_id"]))
            if cycle is None or cycle["status"] != "open":
                raise MonitoringCycleError("监测期次已关闭；历史文章只能查看，不能编辑")

            directory = database.parent / str(row["relative_path"])
            markdown_path = directory / WEEKLY_ARTICLE_FILE_NAME
            if not markdown_path.is_file():
                raise WeeklyArticleError("该版本缺少本地 Markdown 文件，无法编辑")
            old_sha256 = _sha256(markdown_path)
            _write_text_atomically(markdown_path, cleaned + "\n")
            new_sha256 = _sha256(markdown_path)

            first_line = cleaned.splitlines()[0] if cleaned.splitlines() else ""
            title_match = re.match(r"^#\s+(.+)$", first_line)
            new_title = title_match.group(1).strip() if title_match else str(row["title"])

            manifest_path = directory / "manifest.json"
            manifest: dict[str, object] = {}
            if manifest_path.is_file():
                try:
                    decoded = json.loads(manifest_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    decoded = {}
                if isinstance(decoded, dict):
                    manifest = decoded
            files = manifest.get("files")
            if isinstance(files, list):
                updated_files = []
                for entry in files:
                    if isinstance(entry, Mapping) and entry.get("path") == WEEKLY_ARTICLE_FILE_NAME:
                        updated_files.append(
                            {
                                "path": WEEKLY_ARTICLE_FILE_NAME,
                                "sha256": new_sha256,
                                "bytes": markdown_path.stat().st_size,
                            }
                        )
                    else:
                        updated_files.append(dict(entry) if isinstance(entry, Mapping) else entry)
                manifest["files"] = updated_files
            manifest["title"] = new_title
            manifest["edited"] = {
                "edited_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "edited_by": (actor or "operator").strip() or "operator",
                "markdown_sha256_before": old_sha256,
                "markdown_sha256_after": new_sha256,
            }
            _write_text_atomically(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            manifest_sha256 = _sha256(manifest_path)
            connection.execute(
                """
                UPDATE weekly_articles SET title = ?, manifest_sha256 = ? WHERE id = ?
                """,
                (new_title, manifest_sha256, int(row["id"])),
            )
            connection.execute(
                """
                INSERT INTO monitoring_cycle_events(cycle_id, event_type, actor, payload_json)
                VALUES (?, 'weekly_article_edited', ?, ?)
                """,
                (
                    cycle_id,
                    (actor or "operator").strip() or "operator",
                    json.dumps(
                        {
                            "version": version,
                            "title": new_title,
                            "markdown_sha256_before": old_sha256,
                            "markdown_sha256_after": new_sha256,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
    finally:
        connection.close()
    return {
        "article_id": int(row["id"]),
        "cycle_id": cycle_id,
        "version": version,
        "title": new_title,
        "manifest_sha256": manifest_sha256,
    }


def delete_weekly_article(
    database_path: Path,
    *,
    cycle_id: int,
    version: int,
    actor: str = "operator",
) -> dict[str, object]:
    """Remove one weekly article version and its files from an open cycle."""

    if version < 1:
        raise ValueError("article version must be at least 1")
    initialize_schema(database_path)
    database = Path(database_path).resolve()
    connection = connect_database(database)
    relative_path = ""
    try:
        with transaction(connection):
            row = connection.execute(
                """
                SELECT id, cycle_id, version, relative_path
                FROM weekly_articles WHERE cycle_id = ? AND version = ?
                """,
                (cycle_id, version),
            ).fetchone()
            if row is None:
                raise WeeklyArticleError(f"可发布文章 v{version:03d} 不存在")
            cycle = get_monitoring_cycle(connection, cycle_id=int(row["cycle_id"]))
            if cycle is None or cycle["status"] != "open":
                raise MonitoringCycleError("监测期次已关闭；历史文章只能查看，不能删除")
            relative_path = str(row["relative_path"])
            connection.execute(
                "DELETE FROM weekly_articles WHERE id = ?", (int(row["id"]),)
            )
            connection.execute(
                """
                INSERT INTO monitoring_cycle_events(cycle_id, event_type, actor, payload_json)
                VALUES (?, 'weekly_article_deleted', ?, ?)
                """,
                (
                    cycle_id,
                    (actor or "operator").strip() or "operator",
                    json.dumps(
                        {"version": version, "relative_path": relative_path},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
    finally:
        connection.close()
    shutil.rmtree(database.parent / relative_path, ignore_errors=True)
    return {"cycle_id": cycle_id, "version": version, "deleted": True}


def export_weekly_signal_brief(
    database_path: Path,
    *,
    cycle_slug: str | None = None,
    max_signals: int = 8,
    actor: str = "operator",
) -> WeeklySignalBriefResult:
    """Create an immutable local weekly signal draft for one monitoring cycle."""

    if not 1 <= max_signals <= 30:
        raise ValueError("max_signals must be between 1 and 30")
    initialize_schema(database_path)
    database = Path(database_path).resolve()
    connection = connect_database(database, readonly=True)
    try:
        cycle = get_monitoring_cycle(connection, slug=cycle_slug)
        if cycle is None:
            requested = f" {cycle_slug}" if cycle_slug else ""
            raise MonitoringCycleError(f"没有可导出的监测期次{requested}")
        cycle_id = int(cycle["id"])
        snapshot = _brief_snapshot(connection, cycle_id=cycle_id, max_signals=max_signals)
        current_version = int(
            connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM weekly_signal_briefs WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()

    root = database.parent / "exports" / "weekly-signal-briefs" / str(cycle["slug"])
    root.mkdir(parents=True, exist_ok=True)
    version = current_version + 1
    final_directory = root / f"v{version:03d}"
    while final_directory.exists():
        version += 1
        final_directory = root / f"v{version:03d}"
    temporary_directory = Path(tempfile.mkdtemp(prefix=f".v{version:03d}-", dir=root))
    created_files: list[Path] = []
    exported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        markdown_path = temporary_directory / "weekly-signal-brief.md"
        _write_text(
            markdown_path,
            _render_weekly_signal_brief(cycle, snapshot, version=version, exported_at=exported_at),
        )
        created_files.append(markdown_path)
        file_entries = [
            {
                "path": path.relative_to(temporary_directory).as_posix(),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in sorted(created_files)
        ]
        manifest = {
            "brief_kind": "weekly_industry_signal_brief",
            "brief_version": 1,
            "status": "draft_for_human_review",
            "cycle": dict(cycle),
            "export": {"version": version, "exported_at": exported_at},
            "counts": snapshot["counts"],
            "constraints": [
                "行业公众号为发现源；事实性主张仍需回到一手来源补证。",
                "本快报不确认选题、不创建研究任务、不生成最终文章。",
                "本快报不触发任何微信公众号写入或发布动作。",
            ],
            "files": file_entries,
        }
        manifest_path = temporary_directory / "manifest.json"
        _write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        created_files.append(manifest_path)
        temporary_directory.replace(final_directory)
    except Exception:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise

    final_markdown = final_directory / "weekly-signal-brief.md"
    final_manifest = final_directory / "manifest.json"
    relative_path = final_directory.relative_to(database.parent).as_posix()
    write_connection = connect_database(database)
    try:
        with transaction(write_connection):
            cursor = write_connection.execute(
                f"""
                INSERT INTO weekly_signal_briefs(
                    cycle_id, version, relative_path, manifest_sha256, file_count,
                    generated_by, created_at
                ) VALUES (?, ?, ?, ?, ?, 'deterministic_signal_rules', ({SQL_NOW}))
                """,
                (
                    cycle_id,
                    version,
                    relative_path,
                    _sha256(final_manifest),
                    len(created_files),
                ),
            )
            brief_id = int(cursor.lastrowid)
            write_connection.execute(
                """
                INSERT INTO monitoring_cycle_events(cycle_id, event_type, actor, payload_json)
                VALUES (?, 'weekly_signal_brief_exported', ?, ?)
                """,
                (
                    cycle_id,
                    (actor or "operator").strip() or "operator",
                    json.dumps(
                        {
                            "brief_id": brief_id,
                            "version": version,
                            "relative_path": relative_path,
                            "signal_count": snapshot["counts"]["signals"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
    except Exception:
        shutil.rmtree(final_directory, ignore_errors=True)
        raise
    finally:
        write_connection.close()

    return WeeklySignalBriefResult(
        brief_id=brief_id,
        cycle_id=cycle_id,
        cycle_slug=str(cycle["slug"]),
        version=version,
        directory=final_directory,
        markdown_path=final_markdown,
        manifest_path=final_manifest,
        signal_count=int(snapshot["counts"]["signals"]),
        source_article_count=int(snapshot["counts"]["source_articles"]),
    )


@dataclass(frozen=True)
class WeeklyArticleResult:
    article_id: int
    cycle_id: int
    cycle_slug: str
    version: int
    title: str
    directory: Path
    markdown_path: Path
    manifest_path: Path
    signal_count: int
    source_article_count: int
    source_count: int
    status: str = "draft_for_human_review"

    def as_dict(self) -> dict[str, object]:
        return {
            "article_id": self.article_id,
            "cycle_id": self.cycle_id,
            "cycle_slug": self.cycle_slug,
            "version": self.version,
            "title": self.title,
            "directory": str(self.directory),
            "markdown_path": str(self.markdown_path),
            "manifest_path": str(self.manifest_path),
            "signal_count": self.signal_count,
            "source_article_count": self.source_article_count,
            "source_count": self.source_count,
            "status": self.status,
        }


@dataclass(frozen=True)
class WeeklyArticleMutationResult:
    article_id: int
    cycle_id: int
    cycle_slug: str
    version: int
    status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "article_id": self.article_id,
            "cycle_id": self.cycle_id,
            "cycle_slug": self.cycle_slug,
            "version": self.version,
            "status": self.status,
        }


def list_weekly_articles(
    database_path: Path,
    *,
    cycle_id: int | None = None,
    cycle_slug: str | None = None,
) -> list[dict[str, object]]:
    """Return the immutable article versions for one monitoring cycle."""

    if cycle_id is None and cycle_slug is None:
        raise ValueError("Provide one of cycle_id or cycle_slug")
    initialize_schema(database_path)
    database = Path(database_path).resolve()
    connection = connect_database(database, readonly=True)
    try:
        cycle = get_monitoring_cycle(
            connection,
            cycle_id=cycle_id,
            slug=cycle_slug,
        )
        if cycle is None:
            return []
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, cycle_id, version, title, relative_path,
                       manifest_sha256, file_count, generated_by, status,
                       confirmed_at, created_at
                FROM weekly_articles
                WHERE cycle_id = ?
                ORDER BY version DESC, id DESC
                """,
                (int(cycle["id"]),),
            )
        ]
    finally:
        connection.close()
    for row in rows:
        directory = database.parent / str(row["relative_path"])
        markdown = directory / WEEKLY_ARTICLE_FILE_NAME
        row["directory_absolute"] = str(directory)
        row["markdown_absolute_path"] = str(markdown)
        row["exists"] = markdown.is_file()
        row["cycle_slug"] = str(cycle["slug"])
    return rows


def read_weekly_article(
    database_path: Path,
    *,
    cycle_id: int,
    version: int,
) -> dict[str, object] | None:
    """Read one immutable article version and its publishable markdown."""

    if version < 1:
        raise ValueError("article version must be at least 1")
    initialize_schema(database_path)
    database = Path(database_path).resolve()
    connection = connect_database(database, readonly=True)
    try:
        row = connection.execute(
            """
            SELECT id, cycle_id, version, title, relative_path,
                   manifest_sha256, file_count, generated_by, status,
                   confirmed_at, created_at
            FROM weekly_articles
            WHERE cycle_id = ? AND version = ?
            """,
            (cycle_id, version),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    payload = dict(row)
    directory = database.parent / str(payload["relative_path"])
    markdown = directory / WEEKLY_ARTICLE_FILE_NAME
    payload["directory_absolute"] = str(directory)
    payload["markdown_absolute_path"] = str(markdown)
    payload["exists"] = markdown.is_file()
    payload["markdown"] = markdown.read_text(encoding="utf-8") if markdown.is_file() else ""
    manifest = directory / "manifest.json"
    if manifest.is_file():
        try:
            payload["manifest"] = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload["manifest"] = {}
    confirmation = directory / "confirmation.json"
    if confirmation.is_file():
        try:
            payload["confirmation"] = json.loads(confirmation.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload["confirmation"] = {}
    return payload


def generate_weekly_article(
    database_path: Path,
    runner: WeeklyArticleRunner,
    *,
    cycle_slug: str | None = None,
    max_signals: int = 8,
    actor: str = "operator",
    profile: WeeklyArticleProfile | None = None,
) -> WeeklyArticleResult:
    """Generate one publishable weekly industry article through the pi agent.

    The generated markdown is a standalone article ready for human review and
    manual publication.  Internal review language (for example “待补证”) is
    rejected from the article body; uncertainty is kept only in the manifest's
    ``editor_notes`` and ``sources`` so a human can audit before confirming.

    The selected writing profile drives the prompt and a bounded quality gate:
    when the first pass fails the profile's quality checks, the agent gets one
    revision round (style/structure only, facts and sources untouched), and any
    remaining soft issues are recorded in the manifest instead of looping.
    """

    if not 1 <= max_signals <= 30:
        raise ValueError("max_signals must be between 1 and 30")
    profile = profile or DEFAULT_WEEKLY_ARTICLE_PROFILE
    initialize_schema(database_path)
    database = Path(database_path).resolve()
    connection = connect_database(database, readonly=True)
    try:
        cycle = get_monitoring_cycle(connection, slug=cycle_slug)
        if cycle is None:
            requested = f" {cycle_slug}" if cycle_slug else ""
            raise MonitoringCycleError(f"没有可生成文章的监测期次{requested}")
        cycle_id = int(cycle["id"])
        snapshot = _brief_snapshot(connection, cycle_id=cycle_id, max_signals=max_signals)
        if not snapshot["signals"]:
            raise WeeklyArticleError("本期还没有活跃行业信号，不能生成可发布文章")
        current_version = int(
            connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM weekly_articles WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()

    request = _build_weekly_article_request(cycle, snapshot, profile)
    try:
        result = runner.run(request)
    except Exception as exc:  # External runner failures must not fake an article.
        result = AgentRunResult(
            status="failed",
            final_text="",
            tool_calls=(),
            event_lines=(),
            exit_code=-1,
            stderr=f"{type(exc).__name__}: {exc}"[:2000],
        )
    save_agent_exchange(database.parent / "raw" / "agent", request, result)

    parsed: dict[str, object] | None = None
    error: str | None = None
    if result.status == "completed":
        try:
            parsed = _parse_weekly_article_output(result.final_text)
        except WeeklyArticleError as exc:
            error = str(exc)
    else:
        error = result.stderr.strip() or f"研究 Agent 状态异常：{result.status}"
    if parsed is None:
        raise WeeklyArticleError(f"文章生成失败：{error}")

    quality_issues = _weekly_article_quality_issues(parsed, profile)
    revision_rounds = 0
    if quality_issues:
        revision_rounds += 1
        revision_request = _build_weekly_article_revision_request(
            profile, parsed, quality_issues
        )
        try:
            revision_result = runner.run(revision_request)
        except Exception as exc:  # A failed revision must not lose the first pass.
            revision_result = AgentRunResult(
                status="failed",
                final_text="",
                tool_calls=(),
                event_lines=(),
                exit_code=-1,
                stderr=f"{type(exc).__name__}: {exc}"[:2000],
            )
        save_agent_exchange(
            database.parent / "raw" / "agent", revision_request, revision_result
        )
        if revision_result.status == "completed":
            try:
                reparsed = _parse_weekly_article_output(revision_result.final_text)
            except WeeklyArticleError:
                reparsed = None
            if reparsed is not None:
                parsed = reparsed
                quality_issues = _weekly_article_quality_issues(parsed, profile)

    root = database.parent / "exports" / "weekly-articles" / str(cycle["slug"])
    root.mkdir(parents=True, exist_ok=True)
    version = current_version + 1
    final_directory = root / f"v{version:03d}"
    while final_directory.exists():
        version += 1
        final_directory = root / f"v{version:03d}"
    temporary_directory = Path(tempfile.mkdtemp(prefix=f".v{version:03d}-", dir=root))
    created_files: list[Path] = []
    exported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        markdown_path = temporary_directory / WEEKLY_ARTICLE_FILE_NAME
        _write_text(
            markdown_path,
            _render_weekly_article_markdown(parsed),
        )
        created_files.append(markdown_path)
        sources = [dict(item) for item in (parsed.get("sources") or [])]
        manifest = {
            "brief_kind": "weekly_industry_article",
            "brief_version": 1,
            "status": "draft_for_human_review",
            "title": str(parsed["title"]).strip(),
            "cycle": dict(cycle),
            "export": {
                "version": version,
                "exported_at": exported_at,
                "generator": WEEKLY_ARTICLE_GENERATOR,
            },
            "counts": snapshot["counts"],
            "editor_notes": _editor_notes_with_quality(parsed, quality_issues),
            "profile": {"key": profile.key, "label": profile.label},
            "quality_checks": {
                "revision_rounds": revision_rounds,
                "remaining_issues": list(quality_issues),
            },
            "sources": sources,
            "publish_contract": [
                "正文是可独立发布的公众号文章文本，不包含“待补证”等内部工作标记。",
                "发布前必须由操作者人工打开来源复核并确认定稿；本系统不写入公众号草稿箱。",
                "人工确认前，文章状态为 draft_for_human_review，不得对外发布。",
            ],
            "files": [],
        }
        manifest_path = temporary_directory / "manifest.json"
        _write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        created_files.append(manifest_path)
        confirmation_path = temporary_directory / "confirmation.json"
        _write_text(
            confirmation_path,
            json.dumps(
                {
                    "status": "draft_for_human_review",
                    "confirmed_at": None,
                    "updated_at": exported_at,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        created_files.append(confirmation_path)
        temporary_directory.replace(final_directory)
    except Exception:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise

    final_markdown = final_directory / WEEKLY_ARTICLE_FILE_NAME
    final_manifest = final_directory / "manifest.json"
    relative_path = final_directory.relative_to(database.parent).as_posix()
    write_connection = connect_database(database)
    try:
        with transaction(write_connection):
            cursor = write_connection.execute(
                f"""
                INSERT INTO weekly_articles(
                    cycle_id, version, title, relative_path, manifest_sha256,
                    file_count, generated_by, status, confirmed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft_for_human_review', NULL, ({SQL_NOW}))
                """,
                (
                    cycle_id,
                    version,
                    str(parsed["title"]).strip(),
                    relative_path,
                    _sha256(final_manifest),
                    len(created_files),
                    WEEKLY_ARTICLE_GENERATOR,
                ),
            )
            article_id = int(cursor.lastrowid)
            write_connection.execute(
                """
                INSERT INTO monitoring_cycle_events(cycle_id, event_type, actor, payload_json)
                VALUES (?, 'weekly_article_generated', ?, ?)
                """,
                (
                    cycle_id,
                    (actor or "operator").strip() or "operator",
                    json.dumps(
                        {
                            "article_id": article_id,
                            "version": version,
                            "title": str(parsed["title"]).strip(),
                            "relative_path": relative_path,
                            "signal_count": snapshot["counts"]["signals"],
                            "source_count": len(sources),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
    except Exception:
        shutil.rmtree(final_directory, ignore_errors=True)
        raise
    finally:
        write_connection.close()

    return WeeklyArticleResult(
        article_id=article_id,
        cycle_id=cycle_id,
        cycle_slug=str(cycle["slug"]),
        version=version,
        title=str(parsed["title"]).strip(),
        directory=final_directory,
        markdown_path=final_markdown,
        manifest_path=final_manifest,
        signal_count=int(snapshot["counts"]["signals"]),
        source_article_count=int(snapshot["counts"]["source_articles"]),
        source_count=len(sources),
    )


def confirm_weekly_article(
    database_path: Path,
    *,
    cycle_id: int,
    version: int,
    actor: str = "operator",
) -> WeeklyArticleMutationResult:
    """Mark one generated weekly article as human-confirmed for publication."""

    return _mutate_weekly_article_status(
        database_path,
        cycle_id=cycle_id,
        version=version,
        target_status="final",
        event_type="weekly_article_confirmed",
        actor=actor,
    )


def reopen_weekly_article(
    database_path: Path,
    *,
    cycle_id: int,
    version: int,
    actor: str = "operator",
) -> WeeklyArticleMutationResult:
    """Return a confirmed article to the draft queue for further editing."""

    return _mutate_weekly_article_status(
        database_path,
        cycle_id=cycle_id,
        version=version,
        target_status="draft_for_human_review",
        event_type="weekly_article_reopened",
        actor=actor,
    )


def _mutate_weekly_article_status(
    database_path: Path,
    *,
    cycle_id: int,
    version: int,
    target_status: str,
    event_type: str,
    actor: str,
) -> WeeklyArticleMutationResult:
    if version < 1:
        raise ValueError("article version must be at least 1")
    initialize_schema(database_path)
    database = Path(database_path).resolve()
    connection = connect_database(database)
    try:
        with transaction(connection):
            row = connection.execute(
                """
                SELECT id, cycle_id, version, relative_path, status
                FROM weekly_articles
                WHERE cycle_id = ? AND version = ?
                """,
                (cycle_id, version),
            ).fetchone()
            if row is None:
                raise WeeklyArticleError(f"周报文章 v{version:03d} 不存在")
            slug = str(
                connection.execute(
                    "SELECT slug FROM monitoring_cycles WHERE id = ?",
                    (cycle_id,),
                ).fetchone()[0]
            )
            if str(row["status"]) == target_status:
                return WeeklyArticleMutationResult(
                    article_id=int(row["id"]),
                    cycle_id=cycle_id,
                    cycle_slug=slug,
                    version=version,
                    status=target_status,
                )
            if target_status == "final" and str(row["status"]) != "draft_for_human_review":
                raise WeeklyArticleError("只有待人工审阅的文章可以确认定稿")
            if target_status == "draft_for_human_review" and str(row["status"]) != "final":
                raise WeeklyArticleError("只有已定稿的文章可以回到待编辑")

            directory = database.parent / str(row["relative_path"])
            confirmation_path = directory / "confirmation.json"
            if target_status == "final":
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                confirmation = {
                    "status": "final",
                    "confirmed_at": now,
                    "updated_at": now,
                    "confirmed_by": (actor or "operator").strip() or "operator",
                }
            else:
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                confirmation = {
                    "status": "draft_for_human_review",
                    "confirmed_at": None,
                    "updated_at": now,
                    "reopened_by": (actor or "operator").strip() or "operator",
                }
            if target_status == "final":
                connection.execute(
                    f"""
                    UPDATE weekly_articles SET
                        status = 'final', confirmed_at = ({SQL_NOW})
                    WHERE id = ?
                    """,
                    (int(row["id"]),),
                )
            else:
                connection.execute(
                    """
                    UPDATE weekly_articles SET
                        status = 'draft_for_human_review', confirmed_at = NULL
                    WHERE id = ?
                    """,
                    (int(row["id"]),),
                )
            connection.execute(
                """
                INSERT INTO monitoring_cycle_events(cycle_id, event_type, actor, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    cycle_id,
                    event_type,
                    (actor or "operator").strip() or "operator",
                    json.dumps(
                        {
                            "article_id": int(row["id"]),
                            "version": version,
                            "from_status": str(row["status"]),
                            "to_status": target_status,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
            # The confirmation file mirrors the committed row, so write it last
            # inside the transaction: a failure here rolls the DB change back
            # and never leaves the two states contradicting each other.
            _write_text_atomically(
                confirmation_path,
                json.dumps(confirmation, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
            )
        return WeeklyArticleMutationResult(
            article_id=int(row["id"]),
            cycle_id=cycle_id,
            cycle_slug=slug,
            version=version,
            status=target_status,
        )
    finally:
        connection.close()


def _brief_snapshot(connection, *, cycle_id: int, max_signals: int) -> dict[str, object]:
    source_article_count = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM monitoring_cycle_articles
            WHERE cycle_id = ? AND role = 'in_period'
            """,
            (cycle_id,),
        ).fetchone()[0]
    )
    signal_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM industry_signals s
            JOIN topic_clusters tc ON tc.id = s.cluster_id
            WHERE tc.cycle_id = ? AND s.status = 'active'
            """,
            (cycle_id,),
        ).fetchone()[0]
    )
    signals = [
        dict(row)
        for row in connection.execute(
            """
            WITH ranked_candidates AS (
                SELECT c.*, ROW_NUMBER() OVER (
                    PARTITION BY c.signal_id ORDER BY c.score DESC, c.id
                ) AS rank_number
                FROM topic_candidates c
                WHERE c.status != 'archived'
            )
            SELECT s.id, s.signal_title, s.underlying_event, s.trend_state,
                   s.article_count, s.independent_source_count, s.evidence_status,
                   s.consensus, s.disagreements, s.business_implications, s.data_gaps,
                   tc.first_seen_at, tc.last_seen_at,
                   c.id AS candidate_id, c.proposed_title, c.audience, c.why_now,
                   c.angle, c.score, c.review_track, c.risk_flags_json
            FROM industry_signals s
            JOIN topic_clusters tc ON tc.id = s.cluster_id
            LEFT JOIN ranked_candidates c ON c.signal_id = s.id AND c.rank_number = 1
            WHERE tc.cycle_id = ? AND s.status = 'active'
            ORDER BY (s.priority IS NULL), s.priority,
                     c.score DESC NULLS LAST, s.article_count DESC, s.id
            LIMIT ?
            """,
            (cycle_id, max_signals),
        )
    ]
    for signal in signals:
        signal["articles"] = [
            dict(row)
            for row in connection.execute(
                """
                SELECT a.title, a.url, a.published_at, sa.name AS account_name
                FROM cluster_articles ca
                JOIN source_articles a ON a.id = ca.article_id
                JOIN source_accounts sa ON sa.id = a.account_id
                WHERE ca.cluster_id = (
                    SELECT cluster_id FROM industry_signals WHERE id = ?
                )
                ORDER BY ca.is_primary_evidence DESC, a.published_at DESC, a.id
                LIMIT 3
                """,
                (signal["id"],),
            )
        ]
    return {
        "signals": signals,
        "counts": {"source_articles": source_article_count, "signals": signal_count},
    }


def _render_weekly_signal_brief(
    cycle: Any,
    snapshot: dict[str, object],
    *,
    version: int,
    exported_at: str,
) -> str:
    signals = snapshot["signals"]
    counts = snapshot["counts"]
    lines = [
        "---",
        "brief_kind: weekly_industry_signal_brief",
        "status: draft_for_human_review",
        f"cycle_slug: {_yaml(cycle['slug'])}",
        f"cycle_label: {_yaml(cycle['label'])}",
        f"cycle_status: {_yaml(cycle['status'])}",
        f"export_version: {version}",
        f"exported_at: {_yaml(exported_at)}",
        "generator: deterministic_signal_rules",
        "---",
        "",
        f"# {cycle['label']} · 本周信号快报",
        "",
        "> 自动生成的横向扫描草稿，等待人工审阅。公众号文章仅作发现线索；本文件不把待补证主张写成已核实事实。",
        "",
        "## 本期概览",
        "",
        f"- 期内来源文章：{counts['source_articles']}",
        f"- 活跃行业信号：{counts['signals']}",
        f"- 本次快报展示：{len(signals)}（按候选选题辅助分、文章量和时效排序）",
        "",
    ]
    if not signals:
        lines.extend(("", "## 信号清单", "", "本期尚无可展示的行业信号。请先完成受控同步和规则信号构建。", ""))
        return "\n".join(lines)

    lines.extend(("", "## 信号清单", ""))
    for index, signal in enumerate(signals, start=1):
        if index > 1:
            lines.extend(("---", ""))
        evidence_label = {
            "verified": "已核实（仍需人工复核）",
            "mixed": "混合 / 部分待补证",
            "needs_evidence": "待补证",
        }.get(str(signal["evidence_status"]), str(signal["evidence_status"]))
        lines.extend(
            (
                f"### {index}. {_text(signal['signal_title'])}",
                "",
                "**议题状态**：" + _trend_label(signal["trend_state"])
                + "　**讨论度**：" + f"{signal['article_count']} 篇文章；"
                f"独立来源估算 {signal['independent_source_count']}"
                + "　**证据状态**：" + evidence_label,
                "",
                f"**时间范围**：首次 {_date(signal['first_seen_at'])}；最近 {_date(signal['last_seen_at'])}。",
                "",
                f"**底层事件**：{_text(signal['underlying_event']) or '待人工补充'}",
                "",
                f"**来源共识**：{_text(signal['consensus']) or '尚未形成稳定共识'}",
                "",
                f"**来源分歧**：{_text(signal['disagreements']) or '尚未记录明显分歧'}",
                "",
                f"**对行业决策者的含义**：{_text(signal['business_implications']) or '待人工判断'}",
                "",
                f"**待补证**：{_text(signal['data_gaps']) or '需回到监管、论文、公告或数据原文核验'}",
            )
        )
        if signal.get("candidate_id"):
            track = "值得研究" if signal.get("review_track") == "research" else "传播观察"
            lines.extend(
                (
                    "",
                    "**关联候选选题（仍未立项）**",
                    "",
                    f"- 题目：{_text(signal['proposed_title'])}",
                    f"- 审阅队列：{track}；辅助分：{float(signal['score'] or 0):.1f}",
                    f"- 为什么现在：{_text(signal['why_now']) or '待人工补充'}",
                    f"- 建议角度：{_text(signal['angle']) or '待人工补充'}",
                )
            )
        articles = signal["articles"]
        lines.extend(("", "**公众号发现线索（待补证）**", ""))
        if articles:
            for article in articles:
                title = _text(article["title"]) or "未命名文章"
                url = _text(article["url"])
                link = f"[{title}]({url})" if url else title
                lines.append(
                    f"- {link}｜{_text(article['account_name']) or '未知账号'}｜{_date(article['published_at'])}"
                )
        else:
            lines.append("- 本信号尚未保留可打开的公众号文章链接。")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


def _write_text_atomically(path: Path, content: str) -> None:
    """Replace a file without exposing a partially written state."""

    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _yaml(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _text(value: object) -> str:
    return str(value or "").replace("\n", " ").strip()


def _date(value: object) -> str:
    text = _text(value)
    return text[:10] if text else "—"


def _trend_label(value: object) -> str:
    return {
        "new": "新出现",
        "rising": "升温",
        "steady": "持续",
        "falling": "回落",
        "dormant": "沉寂",
    }.get(_text(value), _text(value) or "待判断")


def _build_weekly_article_request(
    cycle: Any,
    snapshot: dict[str, object],
    profile: WeeklyArticleProfile,
) -> PiAgentRequest:
    known_evidence: list[str] = []
    evidence_gaps: list[str] = []
    for signal in snapshot["signals"]:
        title = _text(signal["signal_title"]) or "未命名信号"
        lines = [
            f"行业信号「{title}」",
            f"底层事件：{_text(signal['underlying_event']) or '未提供'}",
            (
                f"趋势：{_trend_label(signal['trend_state'])}；"
                f"讨论度：{int(signal['article_count'] or 0)} 篇；"
                f"独立来源估算：{int(signal['independent_source_count'] or 0)}"
            ),
            f"来源共识：{_text(signal['consensus']) or '未提供'}",
            f"对行业决策者的潜在含义：{_text(signal['business_implications']) or '未提供'}",
        ]
        articles = signal.get("articles") or []
        if articles:
            lines.append(
                "公众号发现线索（仅供检索入口，正文不得标注为内部线索）："
                + "；".join(
                    f"{_text(article['title'])}｜{_text(article['account_name'])}｜"
                    f"{_text(article['url'])}"
                    for article in articles[:3]
                )
            )
        known_evidence.append("\n".join(lines))
        gap = _text(signal["data_gaps"]) or "回到监管、论文、公告或数据原文核验"
        evidence_gaps.append(f"「{title}」：{gap}")

    counts = snapshot["counts"]
    return PiAgentRequest(
        research_question=(
            f"把 {_date(cycle['start_date'])} 至 {_date(cycle['end_date'])} 期间营养健康行业的关键动向，"
            "与公开来源证据一起写成一份可直接发布到微信公众号的《营养保健行业周报》。"
        ),
        audience=profile.audience,
        decision_use="作为独立的微信公众号文章对外发布；读者据此掌握近期行业关键动向与判断。",
        scope=(
            "以监测到的行业信号为主线，按信号的实际分布组织主题；"
            "可使用公开网页检索核实关键事实与数据，但不得偏离这些信号主题。"
        ),
        time_range=(
            f"正文只写 {_date(cycle['start_date'])} 至 {_date(cycle['end_date'])} "
            "这个时间段内发生的新事件；该时间段内没有新进展的信号直接省略，"
            "不得用更早的监管、市场、科研或企业事件填充正文。"
        ),
        known_evidence=tuple(known_evidence),
        evidence_gaps=(
            "以下信息仅供内部编辑参考，不能出现在正文：\n"
            + "\n".join(evidence_gaps)
            + "\n凡无法用公开来源支撑的细节一律不写进正文，只在 editor_notes 中说明。"
        ),
        output_requirements=(
            f"本次写作基于 {counts['signals']} 个活跃行业信号、{counts['source_articles']} 篇来源文章，"
            "请按下方写作规范与 JSON 结构输出。"
        ),
        profile_key=profile.key,
    )


def build_weekly_article_prompt(request: PiAgentRequest) -> str:
    """Render the weekly-article task plus its strict JSON output contract."""

    known = "\n".join(f"- {item}" for item in request.known_evidence) or "（无）"
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    profile = resolve_weekly_article_profile(request.profile_key)
    sections = [
        "立即执行以下周报文章写作任务。先用 web_search 与 fetch_content 核实关键事实，"
        "不要读取本地文件、不要自我介绍、不要请求补充信息。",
        "",
        f"今天的日期是 {today}（北京时间）。请以这个日期判断“最近”“近日”等相对表述，"
        "不要写早于监测窗口的事件。正文指代时间时一律使用具体日期"
        "（如“7月17日至8月15日”）或自然时段（如“最近两周”“8月上旬”），"
        "禁止出现“本期”“期内”“监测窗口”“本周期”等内部口径词。",
        "",
        f"核心任务：{request.research_question or '（未提供）'}",
        f"目标读者：{request.audience}",
        f"决策用途：{request.decision_use}",
        f"写作范围：{request.scope}",
        f"时间范围：{request.time_range}",
        f"已知信号证据（公众号链接只作为检索入口）：\n{known}",
        f"内部注意事项（不可写进正文）：{request.evidence_gaps or '（未提供）'}",
        f"稿件要求：{request.output_requirements}",
        "",
        "正文写作规范（硬性）：",
        f"0. 定位：{profile.positioning}",
        "1. 内容边界：" + "；".join(profile.boundary) + "。",
        "2. 结构：" + "；".join(profile.structure) + "。",
        "3. 表达风格（风格调整不得改动任何事实、数据与来源）："
        + "；".join(profile.style) + "。",
        "4. 质量标准：" + "；".join(profile.quality) + "。",
        "5. 合规要求：" + "；".join(profile.compliance) + "。",
        "6. 必须是能直接作为微信公众号文章发布的完整文本；每条事实写成确定性行业表述，"
        "避免“可能”“据说”“未经证实”等含糊暗示；标题与正文都不允许出现"
        "“待补证”“公众号发现线索”“内部状态”“人工审阅”“证据状态”“草稿”“未核实”"
        "“本期”“期内”“监测窗口”“本周期”等内部标记或内部口径词。",
        "7. 必须严格按照下方 JSON 示例输出：字段名只能是 title、summary、article_body、"
        "editor_notes、sources，不得改用 sections、report_type、coverage、"
        "editorial_summary、methodology 等其他字段名；title 必须采用"
        "「营养保健行业周报|钩子式主标题」格式：以「营养保健行业周报|」开头，"
        "竖线后是一条 8—30 字的钩子式主标题；"
        "正文必须以“已知信号证据”中列出的信号为主线，公开检索只用于补充与核实这些信号。",
        "8. 不要在正文里写来源清单，系统会根据你返回的 sources 字段在文末统一附加"
        "“来源”一节。",
    ]
    if profile.exemplar:
        sections.extend(
            ("", "参考结构示例（仅示范文体结构，内容必须替换为真实信号）：", profile.exemplar)
        )
    prompt = "\n".join(sections)
    prompt += """

只输出一个 JSON 对象，不要用 Markdown 代码围栏，不要附加解释文字。结构如下：
{
  "title": "营养保健行业周报|监管换帅：特殊食品审批节奏会变吗",
  "summary": "一段可直接发布的导语，约 80—150 字",
  "article_body": "Markdown 正文，可直接作为公众号文章发布",
  "editor_notes": "仅供内部编辑使用：无法核实的主张、数据缺口、人工复核重点；不得出现在正文",
  "sources": [
    {
      "claim": "这条来源支持的事实",
      "title": "页面标题",
      "url": "https://具体页面",
      "publisher": "发布机构",
      "source_type": "regulator|research_paper|company_announcement|market_data|media|other"
    }
  ]
}
绝不在 title、summary、article_body 中出现内部标记；找不到可靠来源的细节直接在
editor_notes 中说明，绝不编造 URL。"""
    return prompt


def _weekly_article_quality_issues(
    parsed: Mapping[str, object],
    profile: WeeklyArticleProfile,
) -> list[str]:
    """Run the profile's quality bar on a parsed article without touching it.

    These checks are style/structure only.  They never modify facts, data or
    sources, and a failed check never overrides the human review gate.
    """

    body = str(parsed.get("article_body") or "")
    title = str(parsed.get("title") or "")
    issues: list[str] = []
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", body))
    if not profile.word_count_min <= chinese_chars <= profile.word_count_max:
        issues.append(
            f"正文字数 {chinese_chars}（中文字符），要求 {profile.word_count_min}—{profile.word_count_max}"
        )
    sections = len(re.findall(r"^##\s+", body, re.MULTILINE))
    if sections < 2:
        issues.append(f"正文只有 {sections} 个二级分节，要求至少 2 个")
    if not re.search(r"\d{1,2}月\d{1,2}日", body):
        issues.append('正文缺少具体日期（如“7月17日”）')
    themes_part = title.replace("｜", "|").split("|", 1)
    if len(themes_part) == 2 and themes_part[0] == "营养保健行业周报":
        headline = re.sub(r"\s+", "", themes_part[1])
        headline_length = len(headline)
        if not 6 <= headline_length <= 30:
            issues.append(f"主标题 {headline_length} 字，要求 6—30 字")
        if headline.count("、") >= 2 and "？" not in headline:
            issues.append("主标题疑似主题词罗列（生成时尽量避免）")
    elif title.strip():
        issues.append("标题必须以「营养保健行业周报|」开头")
    found_words = sorted(
        {word for word in profile.forbidden_style_words if word in body}
    )
    if found_words:
        issues.append("正文出现 AI 套话：" + "、".join(found_words))
    internal_words = sorted(
        {word for word in ("本期", "期内", "监测窗口", "本周期") if word in body}
    )
    if internal_words:
        issues.append("正文出现内部口径词（生成时尽量避免）：" + "、".join(internal_words))
    return issues


def _editor_notes_with_quality(
    parsed: Mapping[str, object],
    quality_issues: list[str],
) -> str:
    notes = _text(parsed.get("editor_notes"))
    if not quality_issues:
        return notes
    block = (
        "体检未消除的剩余问题（风格/结构；事实、数据与来源未改动）：\n"
        + "\n".join(f"- {item}" for item in quality_issues)
    )
    return "\n".join(value for value in (notes, block) if value)


def _build_weekly_article_revision_request(
    profile: WeeklyArticleProfile,
    parsed: Mapping[str, object],
    issues: list[str],
) -> PiAgentRequest:
    """Ask the agent for one style/structure revision, facts and sources frozen."""

    return PiAgentRequest(
        research_question=(
            "对上一轮生成的周报文章进行修订。以下体检问题必须逐条解决：\n"
            + "\n".join(f"- {item}" for item in issues)
            + "\n只修订写法、结构与字数：所有事实、数据与来源必须原样保留，"
            "不得新增事实、不得删除事实、不得改动 sources 中的 URL。"
            "\n\n上一轮完整 JSON：\n" + json.dumps(dict(parsed), ensure_ascii=False)
        ),
        audience=profile.audience,
        decision_use="作为独立的微信公众号文章对外发布；读者据此掌握近期行业关键动向与判断。",
        scope="按体检意见修订上一轮输出；保持主题、事实与来源不变。",
        time_range="保持上一轮输出中的具体日期不变。",
        output_requirements="输出修订后的完整 JSON，字段结构与上一轮一致。",
        profile_key=profile.key,
    )


def _parse_weekly_article_output(text: str) -> dict[str, object]:
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
        raise WeeklyArticleError("研究 Agent 未返回合法 JSON，文章未生成") from exc
    if not isinstance(parsed, dict):
        raise WeeklyArticleError("研究 Agent 返回的不是 JSON 对象，文章未生成")
    return _normalize_weekly_article_payload(parsed)


def _normalize_weekly_article_payload(
    parsed: Mapping[str, object],
) -> dict[str, object]:
    title = _article_title_from(parsed)
    if not title:
        raise WeeklyArticleError("研究 Agent 返回的 JSON 缺少可发布的文章标题")
    body, dropped_note = _article_body_from(parsed)
    if not body:
        raise WeeklyArticleError("研究 Agent 返回的 JSON 缺少正文")
    forbidden = _forbidden_markers(title, body)
    if forbidden:
        raise WeeklyArticleError(
            "生成内容仍包含不可发布的内部标记：" + "、".join(sorted(forbidden))
        )
    notes = _text(parsed.get("editor_notes")) or _text(parsed.get("methodology"))
    uncertain_lines: list[str] = []
    uncertain = parsed.get("uncertain_items")
    if isinstance(uncertain, list):
        for item in uncertain:
            if isinstance(item, Mapping):
                text = _first_key(item, ("item", "detail", "text", "title"))
                status = _first_key(item, ("status",))
                uncertain_lines.append(
                    f"- [{status}] {text}" if status and text else f"- {text or status}"
                )
            else:
                text = _text(item)
                if text:
                    uncertain_lines.append(f"- {text}")
    notes_parts = [notes, dropped_note]
    if uncertain_lines:
        notes_parts.append(
            "Agent 记录、待人工复核的不确定项：\n" + "\n".join(uncertain_lines)
        )
    notes = "\n".join(value for value in notes_parts if value)
    return {
        "title": title,
        "summary": _text(parsed.get("summary") or parsed.get("editorial_summary")),
        "article_body": body,
        "editor_notes": notes,
        "sources": _weekly_article_sources(parsed),
    }


def _article_title_from(parsed: Mapping[str, object]) -> str:
    candidate = _text(parsed.get("title")).strip("《》").strip()
    if re.fullmatch(r"营养(保健|健康)行业周报[|｜].+", candidate):
        return candidate
    themes = _theme_words_from_signals(parsed) or _theme_words_from_sections(parsed)
    if themes:
        return "营养保健行业周报|" + "、".join(themes[:3])
    return ""


def _theme_words_from_signals(parsed: Mapping[str, object]) -> list[str]:
    signals = parsed.get("signals_summary")
    if not isinstance(signals, list):
        return []
    themes: list[str] = []
    for signal in signals:
        word = (
            _text(signal.get("signal") or signal.get("title"))
            if isinstance(signal, Mapping)
            else _text(signal)
        )
        word = re.sub(r"[（(].*?[）)]", "", word).strip(" 　,，、|｜·")
        if word and word not in themes:
            themes.append(word)
        if len(themes) >= 3:
            break
    return themes


def _theme_words_from_sections(parsed: Mapping[str, object]) -> list[str]:
    themes: list[str] = []
    for section in parsed.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        heading = _first_key(section, ("section_title", "section", "title", "heading"))
        heading = re.sub(r"^[一二三四五六七八九十百]+[、.．]\s*", "", heading)
        heading = re.sub(r"[（(].*?[）)]", "", heading)
        heading = re.sub(r"(信号|动态|观察清单|观察|清单|周报)$", "", heading)
        heading = heading.strip(" 　,，、|｜·")
        if heading and heading not in themes:
            themes.append(heading)
        if len(themes) >= 3:
            break
    return themes


def _article_body_from(
    parsed: Mapping[str, object],
) -> tuple[str, str]:
    raw = str(parsed.get("article_body") or "").strip()
    if raw:
        if _forbidden_markers(raw):
            raise WeeklyArticleError(
                "研究 Agent 返回的正文包含不可发布的内部标记："
                + "、".join(sorted(_forbidden_markers(raw)))
            )
        return raw, ""
    sections = parsed.get("sections")
    if not isinstance(sections, list) or not sections:
        raise WeeklyArticleError(
            "研究 Agent 返回的 JSON 缺少正文（既无 article_body 也无 sections）"
        )
    lines: list[str] = []
    dropped: list[str] = []
    summary = _first_key(
        parsed, ("executive_summary", "editorial_summary", "summary")
    )
    if summary:
        cleaned, removed = _strip_internal_sentences(summary, "导语")
        if cleaned:
            lines.extend([cleaned, ""])
        dropped.extend(removed)
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        heading = _first_key(section, ("section_title", "section", "title", "heading"))
        if not heading:
            continue
        section_lines: list[str] = []
        interpretation = _first_key(
            section, ("interpretation", "summary", "overview")
        )
        if interpretation:
            cleaned, removed = _strip_internal_sentences(interpretation, heading)
            if cleaned:
                section_lines.extend([cleaned, ""])
            dropped.extend(removed)
        for item in section.get("items") or []:
            if isinstance(item, str):
                cleaned, removed = _strip_internal_sentences(item, heading)
                if cleaned:
                    section_lines.append(f"- {cleaned}")
                dropped.extend(removed)
                continue
            if not isinstance(item, Mapping):
                continue
            headline = _first_key(item, ("headline", "title", "topic"))
            content = _first_key(item, ("content", "detail", "body", "text"))
            if not headline and not content:
                continue
            if headline:
                section_lines.extend([f"### {headline}", ""])
            date = _first_key(item, ("date", "published_at"))
            if date and not _forbidden_markers(date):
                section_lines.extend([date, ""])
            if content:
                cleaned, removed = _strip_internal_sentences(
                    content, headline or heading
                )
                if cleaned:
                    section_lines.extend([cleaned, ""])
                dropped.extend(removed)
            impact = _first_key(item, ("impact", "judgment"))
            if impact:
                cleaned, removed = _strip_internal_sentences(
                    impact, headline or heading
                )
                if cleaned:
                    section_lines.extend([cleaned, ""])
                dropped.extend(removed)
        if not section_lines:
            continue
        lines.extend([f"## {heading}", "", *section_lines, ""])
    implications = parsed.get("decision_implications")
    if isinstance(implications, list):
        implication_lines: list[str] = []
        for line in implications:
            text = (
                _first_key(line, ("detail", "text", "title", "implication"))
                if isinstance(line, Mapping)
                else _text(line)
            )
            cleaned, removed = _strip_internal_sentences(text, "决策启示")
            if cleaned:
                implication_lines.append(f"- {cleaned}")
            dropped.extend(removed)
        if implication_lines:
            lines.extend(["## 决策启示", "", *implication_lines, ""])
    body = "\n".join(lines).strip()
    dropped_note = ""
    if dropped:
        dropped_note = (
            "以下内容含未核实表述，已从可发布正文中移除：\n"
            + "\n".join(f"- {line}" for line in dropped)
        )
    return body, dropped_note


def _strip_internal_sentences(text: str, label: str = "") -> tuple[str, list[str]]:
    value = str(text or "").strip()
    if not _forbidden_markers(value):
        return value, []
    parts = re.split(r"(?<=[。！？])", value)
    kept: list[str] = []
    removed: list[str] = []
    for part in parts:
        if _forbidden_markers(part):
            removed.append(part.strip())
        else:
            kept.append(part)
    label_prefix = f"「{label}」" if label else ""
    return "".join(kept).strip(), [
        f"{label_prefix}{sentence}" for sentence in removed if sentence
    ]


def _forbidden_markers(*texts: object) -> tuple[str, ...]:
    found: set[str] = set()
    for text in texts:
        value = str(text or "")
        for marker in PUBLISHABLE_FORBIDDEN_MARKERS:
            if marker in value:
                found.add(marker)
    return tuple(found)


def _weekly_article_sources(parsed: Mapping[str, object]) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(
        title: str,
        url: str,
        source_type: str,
        publisher: str = "未命名发布机构",
    ) -> None:
        normalized_url = str(url or "").strip()
        if not normalized_url or normalized_url in seen:
            return
        seen.add(normalized_url)
        cleaned.append(
            {
                "claim": str(title or "").strip()[:2000] or normalized_url,
                "title": str(title or "").strip()[:500] or "未命名来源页面",
                "url": normalized_url,
                "publisher": str(publisher or "未命名发布机构").strip()[:200],
                "source_type": normalize_source_type(source_type),
            }
        )

    raw = parsed.get("sources") or []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                add(item, item, "other")
            elif isinstance(item, Mapping):
                url = _first_key(item, ("url", "reference"))
                title = _first_key(item, ("title", "name"))
                source_type = _first_key(item, ("source_type", "type")) or "other"
                add(
                    title or url,
                    url,
                    source_type,
                    publisher=_first_key(item, ("publisher",)),
                )
    if not cleaned:
        for section in parsed.get("sections") or []:
            if not isinstance(section, Mapping):
                continue
            for item in section.get("items") or []:
                if not isinstance(item, Mapping):
                    continue
                verification = item.get("verification")
                urls = []
                if isinstance(verification, Mapping):
                    verification_urls = verification.get("urls") or []
                    if isinstance(verification_urls, list):
                        urls = verification_urls
                item_sources = item.get("sources")
                if isinstance(item_sources, list):
                    urls = item_sources or urls
                source_type = _first_key(
                    item, ("source_type",)
                ) or (
                    _first_key(verification, ("source_type",))
                    if isinstance(verification, Mapping)
                    else "other"
                )
                for url in urls:
                    add(
                        _first_key(item, ("headline", "title")),
                        str(url),
                        source_type,
                    )
    return cleaned[:80]


def _first_key(mapping: Mapping[str, object], keys: tuple[str, ...]) -> str:
    if not isinstance(mapping, Mapping):
        return ""
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _render_weekly_article_markdown(parsed: Mapping[str, object]) -> str:
    title = _text(parsed.get("title"))
    body = str(parsed.get("article_body") or "").strip()
    lines = [f"# {title}", "", body] if body else [f"# {title}"]
    numbered: list[str] = []
    seen: set[str] = set()
    for source in parsed.get("sources") or []:
        if not isinstance(source, Mapping):
            continue
        source_title = _text(source.get("title"))
        url = _text(source.get("url"))
        publisher = _text(source.get("publisher"))
        if source_title and publisher:
            label = f"{source_title}（{publisher}）"
        else:
            label = source_title or publisher or "未命名来源"
        key = url or label
        if key in seen:
            continue
        seen.add(key)
        entry = f"[{label}]({url})" if url else label
        numbered.append(f"{len(numbered) + 1}. {entry}")
    if numbered:
        lines.extend(["", "## 来源", ""])
        lines.extend(numbered)
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"
