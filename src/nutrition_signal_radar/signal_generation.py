"""DeepSeek-assisted interpretation of rule-generated industry signals.

Rules keep doing recall and initial ranking; the configured analysis backend
only fills the interpretive fields of an existing signal card (underlying
event, consensus, disagreements, business implications, data gaps, affected
actors).  Scores, workflow states and candidates never change here, and the
model is told that WeChat articles are discovery leads, not verified facts.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from .cycle_service import MonitoringCycleError, get_monitoring_cycle
from .database import connect_database, transaction
from .schema import initialize_schema


SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
GENERATOR = "deepseek_analysis_v1"
MAX_SIGNALS = 30
MAX_ARTICLES_PER_SIGNAL = 6
MAX_EXCERPT_CHARS = 600
MAX_PARSE_RETRIES = 2

MAX_FIELD_LENGTHS = {
    "underlying_event": 2_000,
    "consensus": 2_000,
    "disagreements": 2_000,
    "business_implications": 2_000,
    "data_gaps": 2_000,
}


class LlmAnalysisGateway(Protocol):
    def analyze(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_object: bool = True,
    ) -> object: ...  # returns an object with status/content/error


class SignalAnalysisError(ValueError):
    """Raised when a signal-analysis run cannot start."""


@dataclass(frozen=True)
class SignalAnalysisOutcome:
    signal_id: int
    status: str
    attempts: int
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "signal_id": self.signal_id,
            "status": self.status,
            "attempts": self.attempts,
            "message": self.message,
        }


@dataclass(frozen=True)
class SignalAnalysisResult:
    database_path: Path
    cycle_id: int
    cycle_slug: str
    generated_by: str
    outcomes: tuple[SignalAnalysisOutcome, ...]

    @property
    def updated(self) -> int:
        return sum(outcome.status == "updated" for outcome in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(outcome.status == "failed" for outcome in self.outcomes)

    def as_dict(self) -> dict[str, object]:
        return {
            "database_path": str(self.database_path),
            "cycle_id": self.cycle_id,
            "cycle_slug": self.cycle_slug,
            "generated_by": self.generated_by,
            "attempted": len(self.outcomes),
            "updated": self.updated,
            "failed": self.failed,
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
        }


SYSTEM_PROMPT = (
    "你是面向营养健康行业决策者的行业信号分析助手。"
    "只基于用户提供的公众号发现线索做解读：不联网、不新增外部事实、不编造数据或来源；"
    "公众号内容只是发现线索，事实性主张仍须回到一手来源补证。"
    "无法判断的内容如实写“需人工补充”或“需人工判断”，不要猜测。"
    "最终回复必须是单个合法 JSON 对象，不要使用 Markdown 代码围栏，不要附加解释文字。"
)


def build_signal_analysis_prompt(
    signal: Mapping[str, object],
    articles: list[Mapping[str, object]],
) -> str:
    """Render one signal card plus its discovery leads for the analysis model."""

    lines = [
        "请解读以下行业信号卡。字段说明：",
        "- underlying_event：用一两句话还原这批文章共同指向的底层事件或议题（不确定就写“需人工补充”）",
        "- consensus：来源之间的共识（没有就写“尚未形成稳定共识”）",
        "- disagreements：来源之间的分歧或不同侧重（没有就写“尚未记录明显分歧”）",
        "- business_implications：对品牌方、原料商或企业决策者的潜在影响（不确定就写“需人工判断”）",
        "- data_gaps：还缺哪些一手证据（监管文件、论文、企业公告、数据原文）",
        "- affected_actors：受影响角色，最多 5 个",
        "",
        f"信号标题：{_one_line(signal.get('signal_title')) or '未命名信号'}",
        f"趋势状态：{_one_line(signal.get('trend_state')) or '待判断'}",
        f"文章数：{int(signal.get('article_count') or 0)}；独立来源估算：{int(signal.get('independent_source_count') or 0)}",
        "",
        "公众号发现线索（待补证，仅作解读素材）：",
    ]
    for index, article in enumerate(articles, start=1):
        excerpt = _one_line(article.get("content_snapshot"))[:MAX_EXCERPT_CHARS]
        lines.append(
            f"{index}. 《{_one_line(article.get('title')) or '未命名文章'}》"
            f"｜{_one_line(article.get('account_name')) or '未知账号'}"
            f"｜{(_one_line(article.get('published_at')) or '无日期')[:10]}"
        )
        if excerpt:
            lines.append(f"   正文节选：{excerpt}")
    lines.extend(
        [
            "",
            "只输出如下 JSON 结构：",
            "{",
            '  "underlying_event": "…",',
            '  "consensus": "…",',
            '  "disagreements": "…",',
            '  "business_implications": "…",',
            '  "data_gaps": "…",',
            '  "affected_actors": ["…"]',
            "}",
        ]
    )
    return "\n".join(lines)


def analyze_cycle_signals(
    database_path: Path,
    gateway: LlmAnalysisGateway,
    *,
    cycle_slug: str | None = None,
    max_signals: int = MAX_SIGNALS,
    actor: str = "operator",
) -> SignalAnalysisResult:
    """Analyze the open cycle's active signals without changing states or scores."""

    if not 1 <= max_signals <= MAX_SIGNALS:
        raise ValueError(f"max_signals must be between 1 and {MAX_SIGNALS}")
    initialize_schema(database_path)
    database = Path(database_path).resolve()
    connection = connect_database(database, readonly=True)
    try:
        cycle = get_monitoring_cycle(connection, slug=cycle_slug)
        if cycle is None:
            raise SignalAnalysisError("没有可分析的监测期次")
        if cycle["status"] != "open":
            raise MonitoringCycleError("监测期次已关闭；历史信号只能查看，不能分析")
        cycle_id = int(cycle["id"])
        signals = [
            dict(row) for row in _active_signal_rows(connection, cycle_id, max_signals)
        ]
        articles_by_signal = {
            int(signal["id"]): _cluster_articles(connection, int(signal["cluster_id"]))
            for signal in signals
        }
    finally:
        connection.close()

    outcomes: list[SignalAnalysisOutcome] = []
    for signal in signals:
        signal_id = int(signal["id"])
        articles = articles_by_signal[signal_id]
        prompt = build_signal_analysis_prompt(signal, articles)
        outcome = _analyze_one_signal(
            database, gateway, cycle_id, signal, prompt, actor=actor
        )
        outcomes.append(outcome)

    return SignalAnalysisResult(
        database_path=database,
        cycle_id=cycle_id,
        cycle_slug=str(cycle["slug"]),
        generated_by=GENERATOR,
        outcomes=tuple(outcomes),
    )


def _analyze_one_signal(
    database: Path,
    gateway: LlmAnalysisGateway,
    cycle_id: int,
    signal: Mapping[str, object],
    prompt: str,
    *,
    actor: str,
) -> SignalAnalysisOutcome:
    parsed: dict[str, object] | None = None
    attempts = 0
    message = ""
    for _ in range(MAX_PARSE_RETRIES):
        attempts += 1
        try:
            result = gateway.analyze(SYSTEM_PROMPT, prompt, json_object=True)
        except Exception as exc:  # Provider failures must not abort the run.
            message = f"模型调用失败：{type(exc).__name__}: {exc}"[:500]
            continue
        if getattr(result, "status", None) != "completed":
            message = f"模型返回异常：{getattr(result, 'error', '') or getattr(result, 'status', 'unknown')}"[:500]
            continue
        try:
            parsed = _validate_analysis_payload(getattr(result, "content", ""))
            message = ""
            break
        except ValueError as exc:
            message = str(exc)
    if parsed is None:
        _record_failure(database, cycle_id, signal, actor, message)
        return SignalAnalysisOutcome(
            signal_id=int(signal["id"]),
            status="failed",
            attempts=attempts,
            message=message,
        )

    _persist_analysis(database, cycle_id, signal, parsed, actor=actor)
    return SignalAnalysisOutcome(
        signal_id=int(signal["id"]),
        status="updated",
        attempts=attempts,
        message="已写入解读字段",
    )


def _validate_analysis_payload(content: str) -> dict[str, object]:
    cleaned = str(content or "").strip()
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
        raise ValueError("模型未返回合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("模型返回的不是 JSON 对象")

    cleaned_fields: dict[str, object] = {}
    for field, maximum in MAX_FIELD_LENGTHS.items():
        value = str(parsed.get(field) or "").strip()
        if not value:
            raise ValueError(f"模型输出缺少字段 {field}")
        cleaned_fields[field] = value[:maximum]

    actors = parsed.get("affected_actors") or []
    if not isinstance(actors, list):
        raise ValueError("affected_actors 必须是数组")
    cleaned_actors: list[str] = []
    for item in actors:
        text = str(item or "").strip()
        if not text or text in cleaned_actors:
            continue
        if len(text) > 80:
            raise ValueError("单个受影响角色不能超过 80 个字符")
        cleaned_actors.append(text)
    if len(cleaned_actors) > 5:
        cleaned_actors = cleaned_actors[:5]
    cleaned_fields["affected_actors"] = cleaned_actors
    return cleaned_fields


def _persist_analysis(
    database: Path,
    cycle_id: int,
    signal: Mapping[str, object],
    parsed: Mapping[str, object],
    *,
    actor: str,
) -> None:
    connection = connect_database(database)
    try:
        with transaction(connection):
            connection.execute(
                f"""
                UPDATE industry_signals SET
                    underlying_event = ?, consensus = ?, disagreements = ?,
                    business_implications = ?, data_gaps = ?,
                    affected_actors_json = ?, generated_by = ?,
                    updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (
                    str(parsed["underlying_event"]),
                    str(parsed["consensus"]),
                    str(parsed["disagreements"]),
                    str(parsed["business_implications"]),
                    str(parsed["data_gaps"]),
                    json.dumps(parsed["affected_actors"], ensure_ascii=False),
                    GENERATOR,
                    int(signal["id"]),
                ),
            )
            connection.execute(
                """
                INSERT INTO monitoring_cycle_events(cycle_id, event_type, actor, payload_json)
                VALUES (?, 'signal_analyzed', ?, ?)
                """,
                (
                    cycle_id,
                    (actor or "operator").strip() or "operator",
                    json.dumps(
                        {"signal_id": int(signal["id"]), "generated_by": GENERATOR},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
    finally:
        connection.close()


def _record_failure(
    database: Path,
    cycle_id: int,
    signal: Mapping[str, object],
    actor: str,
    message: str,
) -> None:
    connection = connect_database(database)
    try:
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO monitoring_cycle_events(cycle_id, event_type, actor, reason, payload_json)
                VALUES (?, 'signal_analysis_failed', ?, ?, ?)
                """,
                (
                    cycle_id,
                    (actor or "operator").strip() or "operator",
                    (message or "模型分析失败")[:500],
                    json.dumps(
                        {"signal_id": int(signal["id"]), "generated_by": GENERATOR},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
    finally:
        connection.close()


def _active_signal_rows(
    connection: sqlite3.Connection,
    cycle_id: int,
    max_signals: int,
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT s.*
            FROM industry_signals s
            JOIN topic_clusters tc ON tc.id = s.cluster_id
            WHERE tc.cycle_id = ? AND s.status = 'active'
            ORDER BY s.article_count DESC, s.id
            LIMIT ?
            """,
            (cycle_id, max_signals),
        )
    )


def _cluster_articles(
    connection: sqlite3.Connection,
    cluster_id: int,
) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT a.title, a.published_at, a.content_snapshot,
                   sa.name AS account_name
            FROM cluster_articles ca
            JOIN source_articles a ON a.id = ca.article_id
            JOIN source_accounts sa ON sa.id = a.account_id
            WHERE ca.cluster_id = ?
            ORDER BY ca.is_primary_evidence DESC, a.published_at DESC, a.id
            LIMIT ?
            """,
            (cluster_id, MAX_ARTICLES_PER_SIGNAL),
        )
    ]


def _one_line(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
