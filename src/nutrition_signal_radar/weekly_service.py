"""Versioned, local weekly signal-brief exports.

The weekly brief is a horizontal scan of already generated industry signals. It
does not confirm facts, change topic status, create research tasks, call an
LLM, or publish anywhere.  A separate human-confirmed topic still starts the
vertical deep-research branch.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .cycle_service import MonitoringCycleError, get_monitoring_cycle
from .database import connect_database, transaction
from .schema import initialize_schema


SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


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
            ORDER BY c.score DESC NULLS LAST, s.article_count DESC, s.id
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
        "## 使用边界",
        "",
        "- 这是每周横向扫描，不是深度研究报告，也不是最终文章。",
        "- 信号卡中的文章数和独立来源估算用于判断讨论度；它们不等于独立事实验证。",
        "- 进入深度研究必须由操作者人工确认选题包、创建并确认研究任务书；本快报不会改变任何状态。",
        "- 本地导出不触发任何微信公众号写入或发布动作。",
    ]
    if not signals:
        lines.extend(("", "## 信号清单", "", "本期尚无可展示的行业信号。请先完成受控同步和规则信号构建。", ""))
        return "\n".join(lines)

    lines.extend(("", "## 信号清单", ""))
    for index, signal in enumerate(signals, start=1):
        evidence_label = {
            "verified": "已核实（仍需人工复核）",
            "mixed": "混合 / 部分待补证",
            "needs_evidence": "待补证",
        }.get(str(signal["evidence_status"]), str(signal["evidence_status"]))
        lines.extend(
            (
                f"### {index}. {_text(signal['signal_title'])}",
                "",
                f"- 议题状态：{_trend_label(signal['trend_state'])}",
                f"- 讨论度：{signal['article_count']} 篇文章；独立来源估算 {signal['independent_source_count']}。",
                f"- 时间：首次 {_date(signal['first_seen_at'])}；最近 {_date(signal['last_seen_at'])}。",
                f"- 证据状态：{evidence_label}。",
                f"- 底层事件：{_text(signal['underlying_event']) or '待人工补充'}",
                f"- 来源共识：{_text(signal['consensus']) or '尚未形成稳定共识'}",
                f"- 来源分歧：{_text(signal['disagreements']) or '尚未记录明显分歧'}",
                f"- 对行业决策者的潜在含义：{_text(signal['business_implications']) or '待人工判断'}",
                f"- 待补证：{_text(signal['data_gaps']) or '需回到监管、论文、公告或数据原文核验'}",
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
