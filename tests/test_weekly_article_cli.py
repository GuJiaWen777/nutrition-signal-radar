import json
from pathlib import Path

from nutrition_signal_radar.agent_gateway import AgentRunResult
from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.cli import main
from nutrition_signal_radar.demo_pipeline import build_demo_topics
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.weekly_service import generate_weekly_article


def _article(external_id: str, account: str, title: str, url: str) -> NormalizedArticle:
    return NormalizedArticle(
        external_id=external_id,
        account_name=account,
        title=title,
        published_at="2026-08-14T09:30:00+08:00",
        url=url,
        content_snapshot=f"正文-{external_id}",
        content_hash=external_id,
        read_count=100,
    )


def _database(tmp_path: Path) -> Path:
    database_path = tmp_path / "var" / "radar.sqlite3"
    import_article_batch(
        database_path,
        ArticleBatch(
            source_kind="redfox",
            source_path="https://redfox.hk/example",
            source_fingerprint="weekly-article-cli-fixture",
            source_rows=2,
            articles=(
                _article("a1", "公众号甲", "益生菌监管政策发布", "https://example.com/a1"),
                _article("a2", "公众号乙", "益生菌监管政策正式发布", "https://example.com/a2"),
            ),
        ),
    )
    build_demo_topics(database_path)
    return database_path


class _Runner:
    def run(self, request: object) -> AgentRunResult:
        payload = {
            "title": "营养保健行业周报|监管趋严、原料创新",
            "summary": "本期聚焦监管整治与原料创新。",
            "article_body": "## 一、监管趋严\n\n7月7日，市场监管总局通报相关案件查办情况。",
            "editor_notes": "尚未核实的数据已从正文移除。",
            "sources": [],
        }
        return AgentRunResult(
            status="completed",
            final_text=json.dumps(payload, ensure_ascii=False),
            tool_calls=(),
            event_lines=(),
            exit_code=0,
            stderr="",
        )


def test_weekly_article_generate_requires_live_acknowledgement(
    tmp_path: Path, capsys
) -> None:
    result = main(
        [
            "--project-root",
            str(tmp_path),
            "weekly",
            "article",
            "generate",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload["status"] == "protected"


def test_weekly_article_can_be_confirmed_and_listed_via_cli(
    tmp_path: Path, capsys
) -> None:
    database_path = _database(tmp_path)
    generate_weekly_article(database_path, _Runner())

    result = main(
        [
            "--project-root",
            str(tmp_path),
            "weekly",
            "article",
            "confirm",
            "--version",
            "1",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "final"
    assert payload["version"] == 1

    result = main(
        [
            "--project-root",
            str(tmp_path),
            "weekly",
            "list",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert len(payload["articles"]) == 1
    assert payload["articles"][0]["status"] == "final"
    assert len(payload["briefs"]) == 0
