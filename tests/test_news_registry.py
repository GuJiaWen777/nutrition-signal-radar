import json
from pathlib import Path

import pytest

from nutrition_signal_radar.cli import main
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.news_registry import (
    NewsRegistryError,
    configure_news_source,
    list_enabled_news_sources,
    list_news_sources,
    news_source_seed_count,
    seed_news_sources,
    set_news_source_enabled,
)
from nutrition_signal_radar.schema import MIGRATIONS, initialize_schema, inspect_schema


def test_news_source_seed_is_idempotent_and_keeps_sources_disabled(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"

    first = seed_news_sources(database_path)
    second = seed_news_sources(database_path)

    assert first.inserted == news_source_seed_count() == 29
    assert first.already_present == 0
    assert second.inserted == 0
    assert second.already_present == 29
    connection = connect_database(database_path, readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM news_sources").fetchone()[0] == 29
        assert connection.execute("SELECT COUNT(*) FROM news_sources WHERE enabled = 1").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM news_source_events").fetchone()[0] == 29
        assert connection.execute(
            "SELECT COUNT(DISTINCT category) FROM news_sources"
        ).fetchone()[0] == 5
    finally:
        connection.close()


def test_news_source_enable_gate_requires_explicit_operator_action(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    seed_news_sources(database_path)

    configured = configure_news_source(
        database_path,
        name="示例监管源",
        domain="https://example.gov.cn/",
        category="policy",
        entry_url="https://example.gov.cn/list",
        default_priority="P0",
    )
    assert configured.enabled is False
    assert list_enabled_news_sources(database_path) == []

    enabled = set_news_source_enabled(
        database_path,
        name="示例监管源",
        enabled=True,
        reason="operator confirmed the initial whitelist entry",
    )
    assert enabled.enabled is True
    assert [source.name for source in list_enabled_news_sources(database_path)] == ["示例监管源"]

    paused = set_news_source_enabled(database_path, name="示例监管源", enabled=False)
    assert paused.enabled is False
    assert list_enabled_news_sources(database_path) == []

    with pytest.raises(NewsRegistryError, match="不存在"):
        set_news_source_enabled(database_path, name="未配置信源", enabled=True)

    connection = connect_database(database_path, readonly=True)
    try:
        events = connection.execute(
            """
            SELECT event_type FROM news_source_events
            WHERE news_source_id = ? ORDER BY id
            """,
            (configured.id,),
        ).fetchall()
        assert [event["event_type"] for event in events] == [
            "configured",
            "enabled",
            "disabled",
        ]
    finally:
        connection.close()


def test_configure_news_source_validates_fields_and_updates_in_place(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"

    with pytest.raises(NewsRegistryError, match="类别"):
        configure_news_source(
            database_path,
            name="坏类别",
            domain="example.com",
            category="media",
            entry_url="https://example.com/",
        )
    with pytest.raises(NewsRegistryError, match="优先级"):
        configure_news_source(
            database_path,
            name="坏优先级",
            domain="example.com",
            category="brand",
            entry_url="https://example.com/",
            default_priority="P9",
        )
    with pytest.raises(NewsRegistryError, match="抓取方式"):
        configure_news_source(
            database_path,
            name="坏抓取方式",
            domain="example.com",
            category="brand",
            entry_url="https://example.com/",
            fetch_kind="scrape",
        )
    with pytest.raises(NewsRegistryError, match="入口"):
        configure_news_source(
            database_path,
            name="坏入口",
            domain="example.com",
            category="brand",
            entry_url="example.com/news",
        )

    created = configure_news_source(
        database_path,
        name="示例品牌",
        domain="https://Example.com/",
        category="brand",
        entry_url="https://example.com/news",
        fetch_kind="rss",
        default_priority="P1",
        notes="测试条目",
    )
    assert created.domain == "example.com"
    assert created.parser_version == "v1"

    updated = configure_news_source(
        database_path,
        name="示例品牌（更新）",
        domain="example.com",
        category="brand",
        entry_url="https://example.com/feed.xml",
        fetch_kind="rss",
        default_priority="P2",
        parser_version="v2",
    )
    assert updated.id == created.id
    assert updated.name == "示例品牌（更新）"
    assert updated.entry_url == "https://example.com/feed.xml"
    assert updated.default_priority == "P2"
    assert updated.parser_version == "v2"
    assert len(list_news_sources(database_path)) == 1


def test_seed_skips_domains_already_configured_by_the_operator(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"

    configured = configure_news_source(
        database_path,
        name="国家市场监督管理总局",
        domain="samr.gov.cn",
        category="policy",
        entry_url="https://www.samr.gov.cn/",
        default_priority="P1",
        notes="operator-tuned entry",
    )
    result = seed_news_sources(database_path)

    assert result.inserted == news_source_seed_count() - 1
    assert result.already_present == 1
    stored = [source for source in list_news_sources(database_path) if source.domain == "samr.gov.cn"]
    assert len(stored) == 1
    assert stored[0].id == configured.id
    assert stored[0].default_priority == "P1"
    assert stored[0].notes == "operator-tuned entry"


def test_news_whitelist_schema_upgrades_from_v11_without_losing_state(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"

    initialize_schema(database_path, migrations=MIGRATIONS[:11])
    seeded = seed_news_sources(database_path)
    status = inspect_schema(database_path)

    assert seeded.inserted == news_source_seed_count()
    assert status.state == "up_to_date"
    assert status.current_version == 17
    assert len(list_news_sources(database_path)) == news_source_seed_count()


def test_news_registry_module_makes_no_external_requests() -> None:
    source_path = Path(__file__).resolve().parents[1] / "src" / "nutrition_signal_radar" / "news_registry.py"
    source_code = source_path.read_text(encoding="utf-8")

    assert "import requests" not in source_code
    assert "api.weixin.qq.com" not in source_code


def test_news_cli_seeds_lists_configures_and_enables_sources(tmp_path: Path, capsys) -> None:
    root = tmp_path

    seeded_code = main(["--project-root", str(root), "news", "seed-sources", "--json"])
    seeded = json.loads(capsys.readouterr().out)
    assert seeded_code == 0
    assert seeded["inserted"] == 29

    listed_code = main(["--project-root", str(root), "news", "list-sources", "--json"])
    listed = json.loads(capsys.readouterr().out)
    assert listed_code == 0
    assert listed["source_count"] == 29
    assert listed["enabled_count"] == 0

    configured_code = main(
        [
            "--project-root",
            str(root),
            "news",
            "configure-source",
            "--name",
            "测试展会",
            "--domain",
            "example-expo.com",
            "--category",
            "expo",
            "--entry-url",
            "https://example-expo.com/news",
            "--priority",
            "P2",
            "--json",
        ]
    )
    configured = json.loads(capsys.readouterr().out)
    assert configured_code == 0
    assert configured["enabled"] is False

    enabled_code = main(
        [
            "--project-root",
            str(root),
            "news",
            "set-source-enabled",
            "--name",
            "测试展会",
            "--enable",
            "--reason",
            "whitelist confirmed",
            "--json",
        ]
    )
    enabled = json.loads(capsys.readouterr().out)
    assert enabled_code == 0
    assert enabled["enabled"] is True

    main(["--project-root", str(root), "news", "list-sources", "--json"])
    listed_again = json.loads(capsys.readouterr().out)
    assert listed_again["source_count"] == 30
    assert listed_again["enabled_count"] == 1

    human_output_code = main(["--project-root", str(root), "news", "list-sources"])
    human_output = capsys.readouterr().out
    assert human_output_code == 0
    assert "News whitelist: 30 total; 1 enabled." in human_output
