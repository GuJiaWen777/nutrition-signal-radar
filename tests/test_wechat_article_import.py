from types import SimpleNamespace

import pytest

from nutrition_signal_radar.cli import main
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.source_registry import SourceRegistryError, configure_source_account
from nutrition_signal_radar.wechat_article_import import (
    _parse_wechat_article_html,
    fetch_wechat_article,
    import_wechat_article,
)


FIXTURE_HTML = """
<html>
<head><meta property="og:title" content="忽略的标题"></head>
<script>var nickname = htmlDecode("Bonny营养领航");</script>
<h1 class="rich_media_title" id="activity-name">益生菌保健食品监管修订<span>解读</span></h1>
<script>var createTime = '2026-08-07 17:30';</script>
<div id="js_content">第一段正文<p>第二段正文</p></div>
<script>var x = 1;</script>
</html>
"""


class _FakeSession:
    def __init__(self, text: str, status_code: int | None = None) -> None:
        self._text = text
        self._status_code = status_code

    def get(self, url: str, *, headers: dict[str, str], timeout: float):
        if self._status_code is not None:
            return SimpleNamespace(text=self._text, status_code=self._status_code)
        return SimpleNamespace(text=self._text)


def test_parse_wechat_article_html_extracts_account_title_body_and_time() -> None:
    result = _parse_wechat_article_html(FIXTURE_HTML)

    assert result.nickname == "Bonny营养领航"
    assert result.title == "益生菌保健食品监管修订解读"
    assert result.published_at == "2026-08-07T17:30:00+08:00"
    assert "第一段正文" in result.body
    assert "第二段正文" in result.body


def test_import_wechat_article_requires_configured_source(tmp_path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    configure_source_account(
        database_path,
        name="Bonny营养领航",
        redfox_name="Bonny营养领航",
        redfox_account=None,
        redfox_library="wide",
        validation_status="matched",
        enabled=True,
    )

    result = import_wechat_article(
        database_path,
        "https://mp.weixin.qq.com/s/example",
        account_name="Bonny营养领航",
        session=_FakeSession(FIXTURE_HTML),
    )

    assert result["account"] == "Bonny营养领航"
    assert result["articles_inserted"] == 1
    assert "益生菌保健食品监管修订解读" in result["title"]
    connection = connect_database(database_path, readonly=True)
    try:
        row = connection.execute(
            """
            SELECT a.content_snapshot FROM source_articles a
            JOIN source_accounts sa ON sa.id = a.account_id
            WHERE sa.name = 'Bonny营养领航'
            """
        ).fetchone()
        assert row is not None
        assert "第一段正文" in row["content_snapshot"]
        event = connection.execute(
            """
            SELECT 1 FROM source_registry_events
            WHERE entity_kind = 'account' AND event_type = 'manual_article_imported'
            """
        ).fetchone()
        assert event is not None
    finally:
        connection.close()


def test_fetch_wechat_article_rejects_http_error_status() -> None:
    session = _FakeSession(FIXTURE_HTML, status_code=403)

    with pytest.raises(SourceRegistryError, match="403"):
        fetch_wechat_article(
            "https://mp.weixin.qq.com/s/example",
            session=session,
        )


def test_import_article_cli_requires_live_acknowledgement(tmp_path, capsys) -> None:
    result = main(
        [
            "--project-root",
            str(tmp_path),
            "sources",
            "import-article",
            "--account",
            "Bonny营养领航",
            "--url",
            "https://mp.weixin.qq.com/s/example",
        ]
    )

    assert result == 2
    captured = capsys.readouterr()
    assert "live" in captured.err
    assert not (tmp_path / "var" / "radar.sqlite3").exists()
