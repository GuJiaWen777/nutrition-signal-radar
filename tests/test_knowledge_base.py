import json
from pathlib import Path

from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.cli import main
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.knowledge_base import (
    export_knowledge_base,
    knowledge_base_stats,
)


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "var" / "radar.sqlite3"
    articles = (
        NormalizedArticle(
            external_id="a1",
            account_name="公众号甲",
            title="有正文的文章",
            published_at="2026-08-13T09:00:00+08:00",
            url="https://example.com/a1",
            content_snapshot="完整正文快照",
            content_hash="body-a1",
        ),
        NormalizedArticle(
            external_id="a2",
            account_name="公众号甲",
            title="无正文的文章",
            published_at="2026-08-14T09:00:00+08:00",
            url="https://example.com/a2",
            content_snapshot="",
            content_hash="body-a2",
        ),
    )
    import_article_batch(
        path,
        ArticleBatch(
            source_kind="redfox",
            source_path="https://redfox.hk/example",
            source_fingerprint="kb-fixture",
            source_rows=2,
            articles=articles,
        ),
    )
    return path


def test_knowledge_base_stats_reports_body_coverage(tmp_path: Path) -> None:
    path = _database(tmp_path)

    stats = knowledge_base_stats(path)

    assert stats.articles == 2
    assert stats.articles_with_body == 1
    assert stats.accounts == 1
    assert stats.body_coverage == 0.5


def test_knowledge_base_export_is_versioned_and_immutable(tmp_path: Path) -> None:
    path = _database(tmp_path)

    first = export_knowledge_base(path)
    second = export_knowledge_base(path, max_articles=1)
    lines = first.articles_path.read_text(encoding="utf-8").splitlines()
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))

    assert first.version == 1
    assert second.version == 2
    assert first.directory.name == "export-v001"
    assert second.directory.name == "export-v002"
    assert first.exported_articles == 2
    assert first.with_body == 1
    assert len(lines) == 2
    first_line = json.loads(lines[0])
    assert first_line["account_name"] == "公众号甲"
    assert first_line["has_body"] is True
    assert manifest["counts"] == {"articles": 2, "with_body": 1, "body_coverage": 0.5}
    assert manifest["kind"] == "local_article_knowledge_base"


def test_knowledge_base_cli_reports_and_exports(tmp_path: Path, capsys) -> None:
    path = _database(tmp_path)
    root = path.parent.parent

    stats_result = main(["--project-root", str(root), "knowledge-base", "stats", "--json"])
    stats_payload = json.loads(capsys.readouterr().out)
    export_result = main(["--project-root", str(root), "knowledge-base", "export", "--json"])
    export_payload = json.loads(capsys.readouterr().out)

    assert stats_result == 0
    assert stats_payload["articles"] == 2
    assert stats_payload["body_coverage"] == 0.5
    assert export_result == 0
    assert export_payload["version"] == 1
    assert Path(export_payload["articles_path"]).is_file()
