from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .cycle_service import MonitoringCycleError, get_monitoring_cycle
from .database import connect_database, transaction
from .schema import initialize_schema


SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


@dataclass(frozen=True)
class CycleArchiveResult:
    export_id: int
    cycle_id: int
    cycle_slug: str
    version: int
    directory: Path
    manifest_path: Path
    files: tuple[Path, ...]


def export_cycle_markdown_archive(
    database_path: Path,
    cycle_id: int,
    *,
    actor: str = "operator",
) -> CycleArchiveResult:
    """Write a versioned, local Markdown snapshot without mutating editorial state."""
    initialize_schema(database_path)
    database = database_path.resolve()
    connection = connect_database(database, readonly=True)
    try:
        cycle = get_monitoring_cycle(connection, cycle_id=cycle_id)
        if cycle is None:
            raise MonitoringCycleError(f"监测期次 {cycle_id} 不存在")
        snapshot = _cycle_snapshot(connection, cycle_id)
        current_version = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(version), 0)
                FROM cycle_exports
                WHERE cycle_id = ? AND export_kind = 'markdown_archive'
                """,
                (cycle_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()

    cycle_root = database.parent / "exports" / "cycles" / str(cycle["slug"])
    cycle_root.mkdir(parents=True, exist_ok=True)
    version = current_version + 1
    final_directory = cycle_root / f"v{version:03d}"
    while final_directory.exists():
        version += 1
        final_directory = cycle_root / f"v{version:03d}"

    temporary_directory = Path(
        tempfile.mkdtemp(prefix=f".v{version:03d}-", dir=cycle_root)
    )
    created_files: list[Path] = []
    exported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        selection_path = temporary_directory / "topic-selection.md"
        _write_text(
            selection_path,
            _render_topic_selection_markdown(cycle, snapshot, version, exported_at),
        )
        created_files.append(selection_path)

        package_directory = temporary_directory / "topic-packages"
        package_directory.mkdir(parents=True, exist_ok=True)
        for package in snapshot["packages"]:
            package_path = package_directory / f"TP-{cycle['slug']}-{int(package['candidate_id']):04d}.md"
            _write_text(
                package_path,
                _render_topic_package_markdown(cycle, package, version, exported_at),
            )
            created_files.append(package_path)

        file_entries = [
            {
                "path": path.relative_to(temporary_directory).as_posix(),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in sorted(created_files)
        ]
        manifest = {
            "archive_kind": "topic_selection_markdown",
            "archive_version": 2,
            "cycle": dict(cycle),
            "export": {
                "version": version,
                "status_snapshot": cycle["status"],
                "exported_at": exported_at,
            },
            "counts": snapshot["counts"],
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

    final_manifest = final_directory / "manifest.json"
    manifest_sha256 = _sha256(final_manifest)
    relative_path = final_directory.relative_to(database.parent).as_posix()
    write_connection = connect_database(database)
    try:
        with transaction(write_connection):
            cursor = write_connection.execute(
                f"""
                INSERT INTO cycle_exports(
                    cycle_id, export_kind, version, status_snapshot,
                    relative_path, manifest_sha256, file_count, created_at
                ) VALUES (?, 'markdown_archive', ?, ?, ?, ?, ?, ({SQL_NOW}))
                """,
                (
                    cycle_id,
                    version,
                    cycle["status"],
                    relative_path,
                    manifest_sha256,
                    len(created_files),
                ),
            )
            export_id = int(cursor.lastrowid)
            write_connection.execute(
                """
                INSERT INTO monitoring_cycle_events(
                    cycle_id, event_type, actor, payload_json
                ) VALUES (?, 'markdown_exported', ?, ?)
                """,
                (
                    cycle_id,
                    actor.strip() or "operator",
                    json.dumps(
                        {
                            "export_id": export_id,
                            "version": version,
                            "relative_path": relative_path,
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

    final_files = tuple(
        final_directory / path.relative_to(temporary_directory)
        for path in created_files
    )
    return CycleArchiveResult(
        export_id=export_id,
        cycle_id=cycle_id,
        cycle_slug=str(cycle["slug"]),
        version=version,
        directory=final_directory,
        manifest_path=final_manifest,
        files=final_files,
    )


def _cycle_snapshot(connection, cycle_id: int) -> dict[str, object]:
    signals = [
        dict(row)
        for row in connection.execute(
            """
            SELECT s.id, s.signal_title, s.underlying_event, s.trend_state,
                   s.article_count, s.independent_source_count, s.evidence_status,
                   s.consensus, s.disagreements, s.business_implications, s.data_gaps,
                   tc.source_count, tc.first_seen_at, tc.last_seen_at
            FROM industry_signals s
            JOIN topic_clusters tc ON tc.id = s.cluster_id
            WHERE tc.cycle_id = ?
            ORDER BY s.id
            """,
            (cycle_id,),
        )
    ]
    candidates = [
        dict(row)
        for row in connection.execute(
            """
            SELECT c.id, c.proposed_title, c.audience, c.core_question,
                   c.why_now, c.angle, c.score, c.review_track, c.track_source,
                   c.track_reason, c.status, c.archived_reason, c.risk_flags_json,
                   s.id AS signal_id, s.signal_title
            FROM topic_candidates c
            JOIN industry_signals s ON s.id = c.signal_id
            JOIN topic_clusters tc ON tc.id = s.cluster_id
            WHERE tc.cycle_id = ?
            ORDER BY c.score DESC, c.id
            """,
            (cycle_id,),
        )
    ]
    packages = [
        dict(row)
        for row in connection.execute(
            """
            SELECT p.*, c.status AS candidate_status, c.review_track,
                   s.signal_title
            FROM topic_packages p
            JOIN topic_candidates c ON c.id = p.candidate_id
            JOIN industry_signals s ON s.id = c.signal_id
            JOIN topic_clusters tc ON tc.id = s.cluster_id
            WHERE tc.cycle_id = ?
            ORDER BY p.id
            """,
            (cycle_id,),
        )
    ]
    article_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM monitoring_cycle_articles WHERE cycle_id = ?",
            (cycle_id,),
        ).fetchone()[0]
    )
    counts = {
        "articles": article_count,
        "signals": len(signals),
        "candidates": len(candidates),
        "topic_packages": len(packages),
        "selected": sum(1 for item in candidates if item["status"] == "selected"),
        "archived": sum(1 for item in candidates if item["status"] == "archived"),
    }
    return {
        "signals": signals,
        "candidates": candidates,
        "packages": packages,
        "counts": counts,
    }


def _render_topic_selection_markdown(
    cycle,
    snapshot: dict[str, object],
    version: int,
    exported_at: str,
) -> str:
    counts = snapshot["counts"]
    candidates = snapshot["candidates"]
    signals = snapshot["signals"]
    lines = [
        "---",
        "archive_kind: topic_selection_archive",
        f"cycle_slug: {_yaml(cycle['slug'])}",
        f"cycle_label: {_yaml(cycle['label'])}",
        f"cycle_status: {_yaml(cycle['status'])}",
        f"start_date: {_yaml(cycle['start_date'])}",
        f"end_date: {_yaml(cycle['end_date'])}",
        f"data_cutoff: {_yaml(cycle['data_cutoff'])}",
        f"export_version: {version}",
        f"exported_at: {_yaml(exported_at)}",
        "---",
        "",
        f"# {cycle['label']} · 本期选题归档",
        "",
        "> 本文件只记录选题阶段的人工审阅快照；不构成行业周报、深度研究稿或最终文章，也不会触发研究或发布。",
        "",
        "## 选题概览",
        "",
        f"- 来源文章：{counts['articles']}",
        f"- 行业信号：{counts['signals']}",
        f"- 候选选题：{counts['candidates']}",
        f"- 选题包：{counts['topic_packages']}",
        f"- 已确认选题：{counts['selected']}",
        f"- 已归档：{counts['archived']}",
        "",
        "## 选题看板快照",
        "",
        "| ID | 状态 | 队列 | 分数 | 题目 | 目标读者 |",
        "| ---: | --- | --- | ---: | --- | --- |",
    ]
    for candidate in candidates:
        lines.append(
            "| {id} | {status} | {track} | {score:.1f} | {title} | {audience} |".format(
                id=candidate["id"],
                status=_table(candidate["status"]),
                track="值得研究" if candidate["review_track"] == "research" else "传播观察",
                score=float(candidate["score"] or 0),
                title=_table(candidate["proposed_title"]),
                audience=_table(candidate["audience"]),
            )
        )
    if not candidates:
        lines.append("| — | — | — | — | 本期尚无候选选题 | — |")

    lines.extend(["", "## 候选依据摘要", ""])
    for signal in signals:
        lines.extend(
            [
                f"### {signal['signal_title']}",
                "",
                f"- 趋势：{signal['trend_state']}",
                f"- 文章 / 独立来源估算：{signal['article_count']} / {signal['independent_source_count']}",
                f"- 证据状态：{signal['evidence_status']}",
                f"- 底层事件：{signal['underlying_event']}",
                f"- 共识：{signal['consensus']}",
                f"- 分歧：{signal['disagreements']}",
                f"- 业务含义：{signal['business_implications']}",
                f"- 待补证：{signal['data_gaps']}",
                "",
            ]
        )
    if not signals:
        lines.append("本期尚无行业信号。\n")
    return "\n".join(lines).rstrip() + "\n"


def _render_topic_package_markdown(
    cycle,
    package: dict[str, object],
    version: int,
    exported_at: str,
) -> str:
    dimensions = _json_list(package.get("selection_dimensions_json"))
    evidence = _json_list(package.get("evidence_json"))
    lines = [
        "---",
        "archive_kind: topic_package",
        f"cycle_slug: {_yaml(cycle['slug'])}",
        f"candidate_id: {package['candidate_id']}",
        f"package_id: {package['id']}",
        f"package_status: {_yaml(package['status'])}",
        f"candidate_status: {_yaml(package['candidate_status'])}",
        f"export_version: {version}",
        f"exported_at: {_yaml(exported_at)}",
        "---",
        "",
        f"# {package['title']}",
        "",
        f"- 目标读者：{package['audience'] or '—'}",
        f"- 核心问题：{package['core_question'] or '—'}",
        f"- 为什么现在：{package['why_now'] or '—'}",
        f"- 行业判断角度：{package['industry_angle'] or '—'}",
        f"- 证据状态：{package['evidence_status']}",
        "",
        "## 选题判断维度",
        "",
    ]
    lines.extend(f"- {item}" for item in dimensions)
    if not dimensions:
        lines.append("- 尚未填写")
    lines.extend(
        [
            "",
            "## 待补证与研究方向",
            "",
            f"- 待补证事实：{package['evidence_gaps'] or '—'}",
            f"- 研究方向：{package['research_directions'] or '—'}",
            f"- 风险提示：{package['risk_notes'] or '—'}",
            "",
            "## 公众号发现线索",
            "",
        ]
    )
    for item in evidence:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or "未命名来源"
        url = item.get("url")
        account = item.get("account_name") or "未知公众号"
        published_at = str(item.get("published_at") or "—")[:10]
        link = f"[{title}]({url})" if url else str(title)
        lines.append(f"- {link}｜{account}｜{published_at}｜待补证")
    if not evidence:
        lines.append("- 尚无公众号发现线索")
    return "\n".join(lines).rstrip() + "\n"


def _json_list(raw: object) -> list[object]:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _yaml(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _table(value: object) -> str:
    return str(value or "—").replace("|", "\\|").replace("\n", " ")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
