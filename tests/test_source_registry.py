from pathlib import Path

import pytest

from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.source_registry import (
    SourceRegistryError,
    configure_source_account,
    configure_priority_site,
    list_enabled_redfox_sources,
    list_priority_sites,
    list_source_accounts,
    priority_site_seed_count,
    seed_priority_sites,
    set_priority_site_enabled,
    set_source_account_enabled,
)


def test_priority_site_seed_is_idempotent_and_keeps_sites_disabled(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"

    first = seed_priority_sites(database_path)
    second = seed_priority_sites(database_path)

    assert first.inserted == priority_site_seed_count() == 31
    assert first.already_present == 0
    assert second.inserted == 0
    assert second.already_present == 31
    connection = connect_database(database_path, readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM priority_sites").fetchone()[0] == 31
        assert connection.execute("SELECT COUNT(*) FROM priority_sites WHERE enabled = 1").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM source_registry_events WHERE entity_kind = 'priority_site'"
        ).fetchone()[0] == 31
    finally:
        connection.close()


def test_source_accounts_require_a_confirmed_exact_configuration_before_enablement(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"

    pending = configure_source_account(
        database_path,
        name="Bonny营养领航",
        redfox_name="Bonny营养领航",
        redfox_account=None,
        redfox_library="wide",
    )

    assert pending.enabled is False
    assert pending.validation_status == "pending"
    assert list_enabled_redfox_sources(database_path) == []
    with pytest.raises(SourceRegistryError, match="matched"):
        set_source_account_enabled(database_path, name="Bonny营养领航", enabled=True)

    confirmed = configure_source_account(
        database_path,
        name="Bonny营养领航",
        redfox_name="Bonny营养领航",
        redfox_account=None,
        redfox_library="wide",
        validation_status="matched",
        enabled=False,
    )
    enabled = set_source_account_enabled(
        database_path,
        name="Bonny营养领航",
        enabled=True,
        reason="operator reviewed bounded broad-library results",
    )

    assert confirmed.enabled is False
    assert enabled.enabled is True
    assert [account.name for account in list_enabled_redfox_sources(database_path)] == ["Bonny营养领航"]
    assert list_source_accounts(database_path)[0].sync_health == "unknown"


def test_quality_source_cannot_be_enabled_without_provider_account_identifier(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"

    with pytest.raises(SourceRegistryError, match="account"):
        configure_source_account(
            database_path,
            name="营养健康评论",
            redfox_name="营养健康评论",
            redfox_account=None,
            redfox_library="quality",
            validation_status="matched",
            enabled=True,
        )


def test_priority_site_can_be_reconfigured_and_enabled_without_becoming_a_whitelist(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    site = configure_priority_site(
        database_path,
        name="监管示例",
        domain="https://example.gov.cn/",
        category="regulator",
        source_role="监管原文优先来源",
        evidence_policy="具体公告页可作为监管事实来源；首页仅用于发现。",
        priority=90,
        applicable_topics=("法规", "合规"),
        enabled=False,
    )
    enabled = set_priority_site_enabled(database_path, domain="example.gov.cn", enabled=True)

    assert site.domain == "example.gov.cn"
    assert site.enabled is False
    assert enabled.enabled is True
    assert enabled.applicable_topics == ("法规", "合规")
    assert list_priority_sites(database_path)[0].domain == "example.gov.cn"
