import json
from pathlib import Path

from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.cli import main


def test_demo_cli_uses_isolated_database(tmp_path: Path, monkeypatch, capsys) -> None:
    batch = ArticleBatch(
        source_kind="xlsx",
        source_path="D:/read-only/source.xlsx",
        source_fingerprint="fingerprint",
        source_rows=1,
        articles=(
            NormalizedArticle(
                external_id="url:test",
                account_name="示例公众号",
                title="测试文章",
                published_at="2026-03-08T09:30:00+08:00",
                url="https://mp.weixin.qq.com/s/example",
                content_snapshot="正文",
                content_hash="hash",
            ),
        ),
    )
    monkeypatch.setattr("nutrition_signal_radar.cli.load_xlsx_batch", lambda *args, **kwargs: batch)

    result = main(
        [
            "--project-root",
            str(tmp_path),
            "demo",
            "import-xlsx",
            "--input",
            str(tmp_path / "ignored.xlsx"),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert Path(payload["database_path"]) == tmp_path / "var" / "demo" / "radar.sqlite3"
    assert payload["articles_inserted"] == 1
    assert not (tmp_path / "var" / "radar.sqlite3").exists()
