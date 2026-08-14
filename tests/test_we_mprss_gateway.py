import json
import sqlite3
from pathlib import Path

import pytest

from nutrition_signal_radar.cli import main
from nutrition_signal_radar.we_mprss_gateway import (
    WeMpRssImportError,
    load_we_mprss_batch,
    validate_we_mprss_source,
)


def _create_source_database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE feeds (
                id TEXT PRIMARY KEY,
                mp_name TEXT,
                status INTEGER
            );
            CREATE TABLE articles (
                id TEXT PRIMARY KEY,
                mp_id TEXT,
                title TEXT,
                url TEXT,
                description TEXT,
                status INTEGER,
                publish_time INTEGER,
                create_time INTEGER,
                content TEXT,
                content_html TEXT,
                publish_info TEXT,
                has_content INTEGER
            );
            """
        )
        connection.executemany(
            "INSERT INTO feeds(id, mp_name, status) VALUES (?, ?, ?)",
            (
                ("MP_WXS_A", "庶正康讯", 1),
                ("MP_WXS_B", "营养健康评论", 1),
                ("MP_WXS_C", "功能食品圈", 0),
            ),
        )
        connection.executemany(
            """
            INSERT INTO articles(
                id, mp_id, title, url, description, status, publish_time,
                create_time, content, content_html, publish_info, has_content
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    "article-a",
                    "MP_WXS_A",
                    "监管新规对营养健康产业意味着什么",
                    "http://mp.weixin.qq.com/s?__biz=abc==&mid=100&idx=1&sn=deadbeef&chksm=tracking",
                    "摘要 A",
                    1,
                    1786608000,
                    1786607000,
                    "这是完整正文 A。",
                    "<p>这是完整正文 A。</p>",
                    json.dumps({"read_num": 120, "like_num": 9}),
                    1,
                ),
                (
                    "article-b",
                    "MP_WXS_B",
                    "功能食品市场的下一阶段",
                    "https://mp.weixin.qq.com/s/short-link?from=tracking",
                    "摘要 B",
                    1,
                    1786521600,
                    1786521500,
                    "",
                    "",
                    json.dumps({"share_count": 4}),
                    0,
                ),
                (
                    "article-deleted",
                    "MP_WXS_A",
                    "已删除文章",
                    "https://mp.weixin.qq.com/s/deleted",
                    "",
                    1000,
                    1786400000,
                    1786400000,
                    "不应出现",
                    "",
                    "{}",
                    1,
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_we_mprss_validation_and_batch_are_read_only_and_source_neutral(tmp_path: Path) -> None:
    source = _create_source_database(tmp_path / "we_mp_rss.db")
    accounts = ("庶正康讯", "营养健康评论")

    report = validate_we_mprss_source(source, accounts=accounts)
    batch = load_we_mprss_batch(source, accounts=accounts)

    assert report.ready_for_import is True
    assert report.source_feed_count == 2
    assert report.source_article_count == 2
    assert report.accounts[0].status == "matched"
    assert report.accounts[0].body_coverage == 1.0
    assert report.accounts[1].body_coverage == 0.0
    assert batch.source_kind == "we-mp-rss"
    assert batch.source_rows == 2
    assert len(batch.articles) == 2

    first = batch.articles[0]
    assert first.external_id == "we-mp-rss:MP_WXS_A:article-a"
    assert first.url == "https://mp.weixin.qq.com/s?__biz=abc%3D%3D&mid=100&idx=1&sn=deadbeef"
    assert first.content_snapshot == "这是完整正文 A。"
    assert first.read_count == 120
    assert first.like_count == 9
    assert first.metadata["body_state"] == "full"
    assert first.published_at == "2026-08-13T16:00:00+08:00"

    second = batch.articles[1]
    assert second.account_name == "营养健康评论"
    assert second.content_snapshot == "摘要 B"
    assert second.metadata["body_state"] == "summary"
    assert second.share_count == 4

    connection = sqlite3.connect(source)
    try:
        assert connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 3
    finally:
        connection.close()


def test_validation_reports_disabled_and_missing_accounts(tmp_path: Path) -> None:
    source = _create_source_database(tmp_path / "we_mp_rss.db")
    report = validate_we_mprss_source(source, accounts=("功能食品圈", "营养品观察"))

    assert report.ready_for_import is False
    assert [item.status for item in report.accounts] == ["disabled", "not_found"]
    assert report.source_article_count == 0


def test_missing_we_mprss_source_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(WeMpRssImportError, match="does not exist"):
        validate_we_mprss_source(tmp_path / "missing.db")


def test_cli_import_uses_an_isolated_we_mprss_pilot_database(tmp_path: Path, capsys) -> None:
    source = _create_source_database(tmp_path / "we_mp_rss.db")

    result = main(
        [
            "--project-root",
            str(tmp_path),
            "we-mp-rss",
            "import",
            "--source-db",
            str(source),
            "--account",
            "庶正康讯",
            "--account",
            "营养健康评论",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert Path(payload["database_path"]) == tmp_path / "var" / "we-mp-rss-pilot" / "radar.sqlite3"
    assert payload["articles_inserted"] == 2
    assert not (tmp_path / "var" / "demo" / "radar.sqlite3").exists()
    assert not (tmp_path / "var" / "radar.sqlite3").exists()
