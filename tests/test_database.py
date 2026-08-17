import sqlite3
from pathlib import Path

import pytest

from nutrition_signal_radar.database import connect_database, transaction
from nutrition_signal_radar.schema import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    Migration,
    apply_migrations,
    initialize_schema,
    inspect_schema,
)


EXPECTED_TABLES = {
    "schema_migrations",
    "source_accounts",
    "priority_sites",
    "source_articles",
    "topic_clusters",
    "cluster_articles",
    "industry_signals",
    "topic_candidates",
    "topic_packages",
    "evidence_records",
    "topic_events",
    "monitoring_cycles",
    "monitoring_cycle_articles",
    "monitoring_cycle_events",
    "cycle_exports",
    "research_tasks",
    "research_events",
    "research_drafts",
    "research_sources",
    "source_registry_events",
    "weekly_signal_briefs",
}


def test_empty_database_initializes_all_phase_one_tables(tmp_path: Path) -> None:
    path = tmp_path / "var" / "radar.sqlite3"

    result = initialize_schema(path)

    assert result.starting_version == 0
    assert result.ending_version == LATEST_SCHEMA_VERSION
    assert result.applied_versions == (1, 2, 3, 4, 5, 6, 7, 8, 9)
    connection = connect_database(path, readonly=True)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert EXPECTED_TABLES <= tables
        assert "research_tasks" in tables
        assert "research_events" in tables
        assert "research_drafts" in tables
        assert "research_sources" in tables
        assert "research_reports" not in tables
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_repeated_initialization_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "radar.sqlite3"
    first = initialize_schema(path)
    second = initialize_schema(path)

    assert first.applied_versions == (1, 2, 3, 4, 5, 6, 7, 8, 9)
    assert second.starting_version == LATEST_SCHEMA_VERSION
    assert second.ending_version == LATEST_SCHEMA_VERSION
    assert second.applied_versions == ()
    connection = connect_database(path, readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 9
    finally:
        connection.close()


def test_transaction_rolls_back_all_writes(tmp_path: Path) -> None:
    path = tmp_path / "radar.sqlite3"
    initialize_schema(path)
    connection = connect_database(path)
    try:
        with pytest.raises(RuntimeError, match="force rollback"):
            with transaction(connection):
                connection.execute("INSERT INTO source_accounts(name) VALUES ('回滚测试')")
                raise RuntimeError("force rollback")
        assert connection.execute("SELECT COUNT(*) FROM source_accounts").fetchone()[0] == 0
    finally:
        connection.close()


def test_constraints_protect_identity_foreign_keys_and_scores(tmp_path: Path) -> None:
    path = tmp_path / "radar.sqlite3"
    initialize_schema(path)
    connection = connect_database(path)
    try:
        connection.execute("INSERT INTO source_accounts(name) VALUES ('营养健康评论')")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO source_accounts(name) VALUES ('营养健康评论')")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO source_articles(account_id, title, external_id) VALUES (999, '无效文章', 'invalid')"
            )

        account_id = connection.execute(
            "SELECT id FROM source_accounts WHERE name = '营养健康评论'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO source_articles(account_id, title, external_id) VALUES (?, '文章一', 'work-1')",
            (account_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO source_articles(account_id, title, external_id) VALUES (?, '文章二', 'work-1')",
                (account_id,),
            )

        connection.execute("INSERT INTO topic_clusters(canonical_name) VALUES ('益生菌新规')")
        cluster_id = connection.execute("SELECT id FROM topic_clusters").fetchone()[0]
        connection.execute(
            "INSERT INTO industry_signals(cluster_id, signal_title, underlying_event) VALUES (?, '新规信号', '监管变化')",
            (cluster_id,),
        )
        signal_id = connection.execute("SELECT id FROM industry_signals").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO topic_candidates(
                    signal_id, proposed_title, audience, core_question, why_now, angle, score
                ) VALUES (?, '题目', '品牌方', '问题', '现在', '角度', 101)
                """,
                (signal_id,),
            )
    finally:
        connection.close()


def test_failed_migration_rolls_back_its_schema_changes(tmp_path: Path) -> None:
    path = tmp_path / "radar.sqlite3"
    connection = connect_database(path)
    broken = Migration(
        version=1,
        name="broken",
        statements=(
            "CREATE TABLE should_rollback(id INTEGER PRIMARY KEY)",
            "INSERT INTO missing_table(id) VALUES (1)",
        ),
    )
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, migrations=(broken,))
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'should_rollback'"
        ).fetchone()
        assert table is None
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
    finally:
        connection.close()


def test_schema_status_is_read_only_and_reports_current_version(tmp_path: Path) -> None:
    path = tmp_path / "radar.sqlite3"

    missing = inspect_schema(path)
    assert missing.state == "not_initialized"
    assert not path.exists()

    initialize_schema(path)
    current = inspect_schema(path)
    assert current.state == "up_to_date"
    assert current.current_version == MIGRATIONS[-1].version


def test_v4_data_is_backfilled_into_one_open_monitoring_cycle(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v4.sqlite3"
    initialize_schema(path, migrations=MIGRATIONS[:4])
    connection = connect_database(path)
    try:
        connection.execute("INSERT INTO source_accounts(name) VALUES ('营养健康评论')")
        account_id = int(connection.execute("SELECT id FROM source_accounts").fetchone()[0])
        connection.execute(
            """
            INSERT INTO source_articles(
                account_id, title, external_id, published_at, url
            ) VALUES (?, '旧版文章', 'legacy-article', '2026-03-18T09:00:00+08:00', 'https://example.com/legacy')
            """,
            (account_id,),
        )
        connection.execute(
            """
            INSERT INTO topic_clusters(canonical_name, cluster_key, generated_by)
            VALUES ('旧版主题', 'legacy-topic', 'demo_rules_v2')
            """
        )
        connection.commit()
    finally:
        connection.close()

    result = initialize_schema(path)

    assert result.starting_version == 4
    assert result.applied_versions == (5, 6, 7, 8, 9)
    connection = connect_database(path, readonly=True)
    try:
        cycle = connection.execute("SELECT * FROM monitoring_cycles").fetchone()
        assert cycle["slug"] == "demo-2026-03"
        assert cycle["status"] == "open"
        assert connection.execute(
            "SELECT COUNT(*) FROM monitoring_cycle_articles WHERE role = 'in_period'"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT cycle_id FROM topic_clusters").fetchone()[0] == cycle["id"]
    finally:
        connection.close()
