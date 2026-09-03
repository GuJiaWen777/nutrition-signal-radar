from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .database import connect_database, transaction


UTC_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


class SchemaVersionError(RuntimeError):
    """Raised when an existing database does not match the known migration chain."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


@dataclass(frozen=True)
class MigrationResult:
    starting_version: int
    ending_version: int
    applied_versions: tuple[int, ...]


@dataclass(frozen=True)
class SchemaStatus:
    state: str
    current_version: int
    latest_version: int
    detail: str


INITIAL_SCHEMA = Migration(
    version=1,
    name="initial_topic_radar_schema",
    statements=(
        f"""
        CREATE TABLE source_accounts (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            redfox_name TEXT,
            segment TEXT NOT NULL DEFAULT 'industry_media',
            priority INTEGER NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            validation_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (validation_status IN ('pending', 'matched', 'not_found', 'error', 'paused')),
            sync_health TEXT NOT NULL DEFAULT 'unknown'
                CHECK (sync_health IN ('unknown', 'healthy', 'warning', 'error', 'paused')),
            last_synced_at TEXT,
            last_article_at TEXT,
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            updated_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """,
        f"""
        CREATE TABLE priority_sites (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            domain TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            source_role TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            applicable_topics_json TEXT NOT NULL DEFAULT '[]',
            evidence_policy TEXT NOT NULL,
            derived_from TEXT,
            last_validated_at TEXT,
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            updated_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """,
        f"""
        CREATE TABLE source_articles (
            id INTEGER PRIMARY KEY,
            external_id TEXT,
            account_id INTEGER NOT NULL REFERENCES source_accounts(id) ON DELETE RESTRICT,
            title TEXT NOT NULL,
            published_at TEXT,
            url TEXT,
            content_snapshot TEXT NOT NULL DEFAULT '',
            content_hash TEXT,
            raw_snapshot_path TEXT,
            metrics_json TEXT NOT NULL DEFAULT '{{}}',
            read_count INTEGER NOT NULL DEFAULT 0 CHECK (read_count >= 0),
            like_count INTEGER NOT NULL DEFAULT 0 CHECK (like_count >= 0),
            share_count INTEGER NOT NULL DEFAULT 0 CHECK (share_count >= 0),
            recommend_count INTEGER NOT NULL DEFAULT 0 CHECK (recommend_count >= 0),
            fetched_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            updated_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            CHECK (external_id IS NOT NULL OR url IS NOT NULL OR content_hash IS NOT NULL)
        )
        """,
        f"""
        CREATE TABLE topic_clusters (
            id INTEGER PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            first_seen_at TEXT,
            last_seen_at TEXT,
            article_count INTEGER NOT NULL DEFAULT 0 CHECK (article_count >= 0),
            source_count INTEGER NOT NULL DEFAULT 0 CHECK (source_count >= 0),
            trend_state TEXT NOT NULL DEFAULT 'new'
                CHECK (trend_state IN ('new', 'rising', 'steady', 'falling', 'dormant')),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
            locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            updated_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """,
        f"""
        CREATE TABLE cluster_articles (
            cluster_id INTEGER NOT NULL REFERENCES topic_clusters(id) ON DELETE RESTRICT,
            article_id INTEGER NOT NULL REFERENCES source_articles(id) ON DELETE RESTRICT,
            similarity REAL CHECK (similarity IS NULL OR similarity BETWEEN 0.0 AND 1.0),
            is_primary_evidence INTEGER NOT NULL DEFAULT 0 CHECK (is_primary_evidence IN (0, 1)),
            assignment_source TEXT NOT NULL DEFAULT 'automatic'
                CHECK (assignment_source IN ('automatic', 'manual')),
            added_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            PRIMARY KEY (cluster_id, article_id)
        )
        """,
        f"""
        CREATE TABLE industry_signals (
            id INTEGER PRIMARY KEY,
            cluster_id INTEGER NOT NULL UNIQUE REFERENCES topic_clusters(id) ON DELETE RESTRICT,
            signal_title TEXT NOT NULL,
            underlying_event TEXT NOT NULL,
            first_seen_at TEXT,
            last_seen_at TEXT,
            trend_state TEXT NOT NULL DEFAULT 'new'
                CHECK (trend_state IN ('new', 'rising', 'steady', 'falling', 'dormant')),
            article_count INTEGER NOT NULL DEFAULT 0 CHECK (article_count >= 0),
            independent_source_count INTEGER NOT NULL DEFAULT 0 CHECK (independent_source_count >= 0),
            consensus TEXT NOT NULL DEFAULT '',
            disagreements TEXT NOT NULL DEFAULT '',
            evidence_status TEXT NOT NULL DEFAULT 'needs_evidence'
                CHECK (evidence_status IN ('verified', 'mixed', 'needs_evidence')),
            affected_actors_json TEXT NOT NULL DEFAULT '[]',
            business_implications TEXT NOT NULL DEFAULT '',
            data_gaps TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
            generated_by TEXT,
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            updated_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """,
        f"""
        CREATE TABLE topic_candidates (
            id INTEGER PRIMARY KEY,
            signal_id INTEGER NOT NULL REFERENCES industry_signals(id) ON DELETE RESTRICT,
            proposed_title TEXT NOT NULL,
            audience TEXT NOT NULL,
            core_question TEXT NOT NULL,
            why_now TEXT NOT NULL,
            angle TEXT NOT NULL,
            score REAL CHECK (score IS NULL OR score BETWEEN 0.0 AND 100.0),
            score_breakdown_json TEXT NOT NULL DEFAULT '{{}}',
            score_reason TEXT NOT NULL DEFAULT '',
            risk_flags_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'candidate_pool'
                CHECK (status IN (
                    'candidate_pool', 'pending_decision', 'package_drafting',
                    'pending_confirmation', 'selected', 'archived'
                )),
            archived_reason TEXT,
            generated_by TEXT,
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            updated_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            CHECK (status = 'archived' OR archived_reason IS NULL),
            CHECK (status != 'archived' OR archived_reason IS NOT NULL)
        )
        """,
        f"""
        CREATE TABLE topic_packages (
            id INTEGER PRIMARY KEY,
            candidate_id INTEGER NOT NULL UNIQUE REFERENCES topic_candidates(id) ON DELETE RESTRICT,
            title TEXT NOT NULL,
            why_now TEXT NOT NULL,
            industry_angle TEXT NOT NULL,
            selection_dimensions_json TEXT NOT NULL DEFAULT '[]',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            evidence_status TEXT NOT NULL DEFAULT 'needs_evidence'
                CHECK (evidence_status IN ('verified', 'mixed', 'needs_evidence')),
            evidence_gaps TEXT NOT NULL DEFAULT '',
            research_directions TEXT NOT NULL DEFAULT '',
            risk_notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'drafting'
                CHECK (status IN ('drafting', 'pending_confirmation', 'confirmed')),
            confirmed_at TEXT,
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            updated_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            CHECK (status = 'confirmed' OR confirmed_at IS NULL),
            CHECK (status != 'confirmed' OR confirmed_at IS NOT NULL)
        )
        """,
        f"""
        CREATE TABLE evidence_records (
            id INTEGER PRIMARY KEY,
            candidate_id INTEGER NOT NULL REFERENCES topic_candidates(id) ON DELETE RESTRICT,
            claim TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'verified', 'rejected')),
            source_type TEXT,
            source_name TEXT,
            title TEXT,
            url TEXT,
            published_at TEXT,
            attachment_path TEXT,
            note TEXT NOT NULL DEFAULT '',
            verified_at TEXT,
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            updated_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            CHECK (status = 'verified' OR verified_at IS NULL),
            CHECK (status != 'verified' OR verified_at IS NOT NULL),
            CHECK (status != 'verified' OR url IS NOT NULL OR attachment_path IS NOT NULL)
        )
        """,
        f"""
        CREATE TABLE topic_events (
            id INTEGER PRIMARY KEY,
            candidate_id INTEGER NOT NULL REFERENCES topic_candidates(id) ON DELETE RESTRICT,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'operator',
            from_status TEXT,
            to_status TEXT,
            reason TEXT,
            payload_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """,
        "CREATE UNIQUE INDEX uq_source_articles_external_id ON source_articles(external_id) WHERE external_id IS NOT NULL",
        "CREATE UNIQUE INDEX uq_source_articles_url ON source_articles(url) WHERE url IS NOT NULL AND url != ''",
        "CREATE INDEX ix_source_accounts_enabled_priority ON source_accounts(enabled, priority DESC)",
        "CREATE INDEX ix_priority_sites_enabled_priority ON priority_sites(enabled, priority DESC)",
        "CREATE INDEX ix_source_articles_account_published ON source_articles(account_id, published_at DESC)",
        "CREATE INDEX ix_source_articles_content_hash ON source_articles(content_hash)",
        "CREATE INDEX ix_cluster_articles_article ON cluster_articles(article_id)",
        "CREATE INDEX ix_topic_clusters_status_updated ON topic_clusters(status, updated_at DESC)",
        "CREATE INDEX ix_industry_signals_status_updated ON industry_signals(status, updated_at DESC)",
        "CREATE INDEX ix_topic_candidates_status_score ON topic_candidates(status, score DESC)",
        "CREATE INDEX ix_evidence_records_candidate_status ON evidence_records(candidate_id, status)",
        "CREATE INDEX ix_topic_events_candidate_created ON topic_events(candidate_id, created_at DESC)",
    ),
)


DEMO_CLUSTERING_SCHEMA = Migration(
    version=2,
    name="stable_demo_cluster_identity",
    statements=(
        "ALTER TABLE topic_clusters ADD COLUMN cluster_key TEXT",
        "ALTER TABLE topic_clusters ADD COLUMN generated_by TEXT",
        "CREATE UNIQUE INDEX uq_topic_clusters_cluster_key ON topic_clusters(cluster_key) WHERE cluster_key IS NOT NULL",
        "CREATE INDEX ix_topic_clusters_generated_by ON topic_clusters(generated_by, status)",
    ),
)


EDITORIAL_REVIEW_SCHEMA = Migration(
    version=3,
    name="dual_editorial_review_tracks",
    statements=(
        """
        ALTER TABLE topic_candidates ADD COLUMN review_track TEXT NOT NULL DEFAULT 'research'
            CHECK (review_track IN ('research', 'watch'))
        """,
        """
        ALTER TABLE topic_candidates ADD COLUMN track_source TEXT NOT NULL DEFAULT 'rule'
            CHECK (track_source IN ('rule', 'manual'))
        """,
        "ALTER TABLE topic_candidates ADD COLUMN track_reason TEXT NOT NULL DEFAULT ''",
        """
        ALTER TABLE topic_candidates ADD COLUMN editor_modified INTEGER NOT NULL DEFAULT 0
            CHECK (editor_modified IN (0, 1))
        """,
        """
        CREATE INDEX ix_topic_candidates_track_status_score
        ON topic_candidates(review_track, status, score DESC)
        """,
    ),
)


TOPIC_PACKAGE_EDITOR_SCHEMA = Migration(
    version=4,
    name="editable_topic_packages",
    statements=(
        "ALTER TABLE topic_packages ADD COLUMN audience TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE topic_packages ADD COLUMN core_question TEXT NOT NULL DEFAULT ''",
        """
        ALTER TABLE topic_packages ADD COLUMN editor_modified INTEGER NOT NULL DEFAULT 0
            CHECK (editor_modified IN (0, 1))
        """,
        "ALTER TABLE topic_packages ADD COLUMN submitted_at TEXT",
        """
        UPDATE topic_packages
        SET audience = COALESCE((
                SELECT c.audience FROM topic_candidates c
                WHERE c.id = topic_packages.candidate_id
            ), ''),
            core_question = COALESCE((
                SELECT c.core_question FROM topic_candidates c
                WHERE c.id = topic_packages.candidate_id
            ), '')
        """,
        """
        UPDATE topic_packages
        SET submitted_at = CASE
            WHEN status = 'confirmed' THEN confirmed_at
            WHEN status = 'pending_confirmation' THEN updated_at
            ELSE NULL
        END
        """,
        "CREATE INDEX ix_topic_packages_status_updated ON topic_packages(status, updated_at DESC)",
    ),
)


MONITORING_CYCLE_SCHEMA = Migration(
    version=5,
    name="monitoring_cycles_and_markdown_archives",
    statements=(
        f"""
        CREATE TABLE monitoring_cycles (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            data_cutoff TEXT,
            status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'closed')),
            closed_at TEXT,
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            updated_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            CHECK (start_date IS NULL OR end_date IS NULL OR start_date <= end_date),
            CHECK (status = 'closed' OR closed_at IS NULL),
            CHECK (status != 'closed' OR closed_at IS NOT NULL)
        )
        """,
        """
        CREATE UNIQUE INDEX uq_monitoring_cycles_single_open
        ON monitoring_cycles(status) WHERE status = 'open'
        """,
        """
        CREATE INDEX ix_monitoring_cycles_dates
        ON monitoring_cycles(end_date DESC, created_at DESC)
        """,
        f"""
        CREATE TABLE monitoring_cycle_articles (
            cycle_id INTEGER NOT NULL REFERENCES monitoring_cycles(id) ON DELETE RESTRICT,
            article_id INTEGER NOT NULL REFERENCES source_articles(id) ON DELETE RESTRICT,
            role TEXT NOT NULL DEFAULT 'in_period'
                CHECK (role IN ('in_period', 'context')),
            added_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            PRIMARY KEY (cycle_id, article_id)
        )
        """,
        """
        CREATE INDEX ix_monitoring_cycle_articles_article
        ON monitoring_cycle_articles(article_id, cycle_id)
        """,
        f"""
        CREATE TABLE monitoring_cycle_events (
            id INTEGER PRIMARY KEY,
            cycle_id INTEGER NOT NULL REFERENCES monitoring_cycles(id) ON DELETE RESTRICT,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'operator',
            reason TEXT,
            payload_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """,
        """
        CREATE INDEX ix_monitoring_cycle_events_cycle_created
        ON monitoring_cycle_events(cycle_id, created_at DESC)
        """,
        f"""
        CREATE TABLE cycle_exports (
            id INTEGER PRIMARY KEY,
            cycle_id INTEGER NOT NULL REFERENCES monitoring_cycles(id) ON DELETE RESTRICT,
            export_kind TEXT NOT NULL DEFAULT 'markdown_archive'
                CHECK (export_kind IN ('markdown_archive')),
            version INTEGER NOT NULL CHECK (version >= 1),
            status_snapshot TEXT NOT NULL CHECK (status_snapshot IN ('open', 'closed')),
            relative_path TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            file_count INTEGER NOT NULL CHECK (file_count >= 1),
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            UNIQUE (cycle_id, export_kind, version)
        )
        """,
        """
        CREATE INDEX ix_cycle_exports_cycle_created
        ON cycle_exports(cycle_id, created_at DESC)
        """,
        "ALTER TABLE topic_clusters ADD COLUMN cycle_id INTEGER REFERENCES monitoring_cycles(id) ON DELETE RESTRICT",
        """
        INSERT INTO monitoring_cycles(
            slug, label, start_date, end_date, data_cutoff, status
        )
        SELECT
            CASE
                WHEN MIN(published_at) IS NOT NULL
                    THEN 'demo-' || substr(MIN(published_at), 1, 7)
                ELSE 'demo-import'
            END,
            CASE
                WHEN MIN(published_at) IS NOT NULL
                    THEN 'Demo ' || substr(MIN(published_at), 1, 7)
                ELSE 'Demo 导入数据'
            END,
            substr(MIN(published_at), 1, 10),
            substr(MAX(published_at), 1, 10),
            MAX(published_at),
            'open'
        FROM source_articles
        HAVING COUNT(*) > 0
        """,
        """
        INSERT INTO monitoring_cycle_articles(cycle_id, article_id, role)
        SELECT mc.id, a.id, 'in_period'
        FROM source_articles a
        CROSS JOIN monitoring_cycles mc
        WHERE mc.status = 'open'
        """,
        """
        UPDATE topic_clusters
        SET cycle_id = (
            SELECT id FROM monitoring_cycles WHERE status = 'open' ORDER BY id LIMIT 1
        )
        WHERE cycle_id IS NULL
        """,
        "DROP INDEX uq_topic_clusters_cluster_key",
        """
        CREATE UNIQUE INDEX uq_topic_clusters_cycle_cluster_key
        ON topic_clusters(cycle_id, cluster_key)
        WHERE cycle_id IS NOT NULL AND cluster_key IS NOT NULL
        """,
        """
        CREATE INDEX ix_topic_clusters_cycle_status_updated
        ON topic_clusters(cycle_id, status, updated_at DESC)
        """,
    ),
)


RESEARCH_TASK_SCHEMA = Migration(
    version=6,
    name="research_task_briefs_and_human_gate",
    statements=(
        f"""
        CREATE TABLE research_tasks (
            id INTEGER PRIMARY KEY,
            topic_package_id INTEGER NOT NULL UNIQUE
                REFERENCES topic_packages(id) ON DELETE RESTRICT,
            parent_task_id INTEGER REFERENCES research_tasks(id) ON DELETE RESTRICT,
            new_task_reason TEXT,
            research_question TEXT NOT NULL,
            audience TEXT NOT NULL,
            decision_use TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT '',
            geography TEXT NOT NULL DEFAULT '',
            time_range TEXT NOT NULL DEFAULT '',
            exclusions TEXT NOT NULL DEFAULT '',
            selection_dimensions_json TEXT NOT NULL DEFAULT '[]',
            known_evidence_json TEXT NOT NULL DEFAULT '[]',
            evidence_gaps TEXT NOT NULL DEFAULT '',
            output_requirements TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'task_drafting'
                CHECK (status IN (
                    'task_drafting', 'pending_start', 'researching',
                    'draft_review', 'completed', 'cancelled'
                )),
            start_mode TEXT NOT NULL DEFAULT 'manual'
                CHECK (start_mode IN ('manual', 'automatic')),
            confirmed_at TEXT,
            start_requested_at TEXT,
            started_at TEXT,
            cancelled_at TEXT,
            cancellation_reason TEXT,
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            updated_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            CHECK (status IN ('task_drafting', 'cancelled') OR confirmed_at IS NOT NULL),
            CHECK (status = 'cancelled' OR cancelled_at IS NULL),
            CHECK (status != 'cancelled' OR cancelled_at IS NOT NULL),
            CHECK (status != 'cancelled' OR cancellation_reason IS NOT NULL)
        )
        """,
        """
        CREATE INDEX ix_research_tasks_status_updated
        ON research_tasks(status, updated_at DESC)
        """,
        """
        CREATE INDEX ix_research_tasks_parent
        ON research_tasks(parent_task_id)
        """,
        f"""
        CREATE TABLE research_events (
            id INTEGER PRIMARY KEY,
            research_task_id INTEGER NOT NULL
                REFERENCES research_tasks(id) ON DELETE RESTRICT,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'operator',
            from_status TEXT,
            to_status TEXT,
            reason TEXT,
            payload_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """,
        """
        CREATE INDEX ix_research_events_task_created
        ON research_events(research_task_id, created_at DESC)
        """,
    ),
)


ARTICLE_DRAFT_SCHEMA = Migration(
    version=7,
    name="versioned_article_drafts_and_sources",
    statements=(
        f"""
        CREATE TABLE research_drafts (
            id INTEGER PRIMARY KEY,
            research_task_id INTEGER NOT NULL
                REFERENCES research_tasks(id) ON DELETE RESTRICT,
            supersedes_draft_id INTEGER
                REFERENCES research_drafts(id) ON DELETE RESTRICT,
            title TEXT NOT NULL,
            article_body TEXT NOT NULL,
            key_judgment TEXT NOT NULL DEFAULT '',
            action_implications TEXT NOT NULL DEFAULT '',
            uncertainty_notes TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL CHECK (version >= 1),
            revision_reason TEXT,
            change_summary TEXT NOT NULL DEFAULT '',
            generated_by TEXT NOT NULL,
            markdown_relative_path TEXT,
            final_markdown_relative_path TEXT,
            status TEXT NOT NULL DEFAULT 'review'
                CHECK (status IN ('review', 'superseded', 'final')),
            generated_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            reviewed_at TEXT,
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            UNIQUE (research_task_id, version),
            CHECK (status != 'final' OR reviewed_at IS NOT NULL),
            CHECK (status = 'final' OR final_markdown_relative_path IS NULL)
        )
        """,
        """
        CREATE INDEX ix_research_drafts_task_version
        ON research_drafts(research_task_id, version DESC)
        """,
        f"""
        CREATE TABLE research_sources (
            id INTEGER PRIMARY KEY,
            draft_id INTEGER NOT NULL
                REFERENCES research_drafts(id) ON DELETE RESTRICT,
            claim TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT 'wechat_discovery'
                CHECK (source_type IN (
                    'wechat_discovery', 'regulator', 'research_paper',
                    'company_announcement', 'market_data', 'other'
                )),
            source_name TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            url_or_reference TEXT NOT NULL DEFAULT '',
            evidence_status TEXT NOT NULL DEFAULT 'needs_evidence'
                CHECK (evidence_status IN ('verified', 'needs_evidence')),
            accessed_at TEXT,
            source_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """,
        """
        CREATE INDEX ix_research_sources_draft
        ON research_sources(draft_id, id)
        """,
        """
        ALTER TABLE research_events ADD COLUMN draft_id INTEGER
            REFERENCES research_drafts(id) ON DELETE RESTRICT
        """,
        """
        CREATE INDEX ix_research_events_draft_created
        ON research_events(draft_id, created_at DESC)
        """,
    ),
)


SOURCE_SYNC_AND_WEEKLY_BRIEF_SCHEMA = Migration(
    version=8,
    name="source_registry_sync_and_weekly_signal_briefs",
    statements=(
        "ALTER TABLE source_accounts ADD COLUMN redfox_account TEXT",
        """
        ALTER TABLE source_accounts ADD COLUMN redfox_library TEXT NOT NULL DEFAULT 'quality'
        CHECK (redfox_library IN ('quality', 'wide'))
        """,
        f"""
        CREATE TABLE source_registry_events (
            id INTEGER PRIMARY KEY,
            entity_kind TEXT NOT NULL CHECK (entity_kind IN ('account', 'priority_site')),
            entity_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT,
            payload_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """,
        """
        CREATE INDEX ix_source_registry_events_entity
        ON source_registry_events(entity_kind, entity_id, id DESC)
        """,
        f"""
        CREATE TABLE weekly_signal_briefs (
            id INTEGER PRIMARY KEY,
            cycle_id INTEGER NOT NULL REFERENCES monitoring_cycles(id) ON DELETE RESTRICT,
            version INTEGER NOT NULL CHECK (version >= 1),
            relative_path TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            file_count INTEGER NOT NULL CHECK (file_count >= 1),
            generated_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            UNIQUE (cycle_id, version)
        )
        """,
        """
        CREATE INDEX ix_weekly_signal_briefs_cycle_version
        ON weekly_signal_briefs(cycle_id, version DESC)
        """,
    ),
)

WEEKLY_ARTICLE_SCHEMA = Migration(
    version=9,
    name="publishable_weekly_articles_and_confirmation",
    statements=(
        f"""
        CREATE TABLE weekly_articles (
            id INTEGER PRIMARY KEY,
            cycle_id INTEGER NOT NULL
                REFERENCES monitoring_cycles(id) ON DELETE RESTRICT,
            version INTEGER NOT NULL CHECK (version >= 1),
            title TEXT NOT NULL DEFAULT '',
            relative_path TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            file_count INTEGER NOT NULL CHECK (file_count >= 1),
            generated_by TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft_for_human_review'
                CHECK (status IN ('draft_for_human_review', 'final')),
            confirmed_at TEXT,
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            UNIQUE (cycle_id, version),
            CHECK (status = 'final' OR confirmed_at IS NULL),
            CHECK (status != 'final' OR confirmed_at IS NOT NULL)
        )
        """,
        """
        CREATE INDEX ix_weekly_articles_cycle_status_version
        ON weekly_articles(cycle_id, status, version DESC)
        """,
    ),
)

RESEARCH_SOURCE_MEDIA_TYPE_SCHEMA = Migration(
    version=10,
    name="research_sources_accept_media_type",
    statements=(
        f"""
        CREATE TABLE research_sources_new (
            id INTEGER PRIMARY KEY,
            draft_id INTEGER NOT NULL
                REFERENCES research_drafts(id) ON DELETE RESTRICT,
            claim TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT 'wechat_discovery'
                CHECK (source_type IN (
                    'wechat_discovery', 'regulator', 'research_paper',
                    'company_announcement', 'market_data', 'media', 'other'
                )),
            source_name TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            url_or_reference TEXT NOT NULL DEFAULT '',
            evidence_status TEXT NOT NULL DEFAULT 'needs_evidence'
                CHECK (evidence_status IN ('verified', 'needs_evidence')),
            accessed_at TEXT,
            source_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """,
        """
        INSERT INTO research_sources_new(
            id, draft_id, claim, source_type, source_name, title,
            url_or_reference, evidence_status, accessed_at, source_note, created_at
        )
        SELECT id, draft_id, claim, source_type, source_name, title,
               url_or_reference, evidence_status, accessed_at, source_note, created_at
        FROM research_sources
        """,
        "DROP TABLE research_sources",
        "ALTER TABLE research_sources_new RENAME TO research_sources",
        """
        CREATE INDEX ix_research_sources_draft
        ON research_sources(draft_id, id)
        """,
    ),
)


SIGNAL_PRIORITY_SCHEMA = Migration(
    version=11,
    name="signal_manual_priority",
    statements=(
        """
        ALTER TABLE industry_signals ADD COLUMN priority INTEGER
        """,
    ),
)


NEWS_SOURCE_WHITELIST_SCHEMA = Migration(
    version=12,
    name="news_line_whitelist_sources",
    statements=(
        f"""
        CREATE TABLE news_sources (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            domain TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL
                CHECK (category IN ('policy', 'disclosure', 'brand', 'expo', 'research')),
            entry_url TEXT NOT NULL
                CHECK (entry_url LIKE 'http://%' OR entry_url LIKE 'https://%'),
            fetch_kind TEXT NOT NULL DEFAULT 'html_list'
                CHECK (fetch_kind IN ('html_list', 'api', 'rss')),
            default_priority TEXT NOT NULL
                CHECK (default_priority IN ('P0', 'P1', 'P2', 'P3')),
            parser_version TEXT NOT NULL DEFAULT 'v1',
            enabled INTEGER NOT NULL DEFAULT 0,
            sync_health TEXT NOT NULL DEFAULT 'paused',
            last_fetched_at TEXT,
            last_item_at TEXT,
            notes TEXT NOT NULL DEFAULT '',
            derived_from TEXT,
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            updated_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """,
        """
        CREATE INDEX ix_news_sources_enabled_priority
        ON news_sources(enabled, default_priority, name)
        """,
        f"""
        CREATE TABLE news_source_events (
            id INTEGER PRIMARY KEY,
            news_source_id INTEGER NOT NULL
                REFERENCES news_sources(id) ON DELETE RESTRICT,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT,
            payload_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """,
        """
        CREATE INDEX ix_news_source_events_source
        ON news_source_events(news_source_id, id DESC)
        """,
    ),
)


NEWS_ITEM_SELECTION_SCHEMA = Migration(
    version=13,
    name="news_item_selection_decisions",
    statements=(
        f"""
        CREATE TABLE news_items (
            id INTEGER PRIMARY KEY,
            article_id INTEGER NOT NULL UNIQUE
                REFERENCES source_articles(id) ON DELETE RESTRICT,
            news_source_id INTEGER NOT NULL
                REFERENCES news_sources(id) ON DELETE RESTRICT,
            title TEXT NOT NULL,
            url TEXT,
            published_at TEXT,
            source_name TEXT NOT NULL,
            source_category TEXT NOT NULL,
            section TEXT NOT NULL
                CHECK (section IN ('policy', 'brand_finance', 'expo', 'research')),
            priority TEXT NOT NULL
                CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
            selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
            decision TEXT NOT NULL CHECK (decision IN ('selected', 'rejected')),
            rejection_reasons TEXT NOT NULL DEFAULT '[]',
            priority_reasons TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """,
        """
        CREATE INDEX ix_news_items_selected_order
        ON news_items(selected, priority, published_at DESC)
        """,
    ),
)


NEWS_ARTICLE_SCHEMA = Migration(
    version=14,
    name="news_article_builds",
    statements=(
        f"""
        CREATE TABLE news_articles (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version >= 1),
            mode TEXT NOT NULL CHECK (mode IN ('daily', 'weekly')),
            title TEXT NOT NULL,
            item_count INTEGER NOT NULL CHECK (item_count >= 0),
            llm_model TEXT NOT NULL DEFAULT '',
            relative_path TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            file_count INTEGER NOT NULL CHECK (file_count >= 1),
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            UNIQUE (slug, version)
        )
        """,
        """
        CREATE INDEX ix_news_articles_slug_version
        ON news_articles(slug, version DESC)
        """,
    ),
)


NEWS_WECHAT_PUSH_SCHEMA = Migration(
    version=15,
    name="news_wechat_push_records",
    statements=(
        """
        ALTER TABLE news_articles ADD COLUMN wechat_media_id TEXT
        """,
        """
        ALTER TABLE news_articles ADD COLUMN wechat_pushed_at TEXT
        """,
    ),
)


NEWS_SOURCE_PARSER_CONFIG_SCHEMA = Migration(
    version=16,
    name="news_source_parser_config",
    statements=(
        """
        ALTER TABLE news_sources ADD COLUMN parser_config TEXT NOT NULL DEFAULT ''
        """,
    ),
)


NEWS_ARTICLE_ITEMS_SCHEMA = Migration(
    version=17,
    name="news_article_item_links",
    statements=(
        f"""
        CREATE TABLE news_article_items (
            article_id INTEGER NOT NULL REFERENCES news_articles(id),
            news_item_id INTEGER NOT NULL REFERENCES news_items(id),
            created_at TEXT NOT NULL DEFAULT ({UTC_NOW}),
            PRIMARY KEY (article_id, news_item_id)
        )
        """,
        """
        CREATE INDEX ix_news_article_items_item
        ON news_article_items(news_item_id)
        """,
    ),
)


MIGRATIONS: tuple[Migration, ...] = (
    INITIAL_SCHEMA,
    DEMO_CLUSTERING_SCHEMA,
    EDITORIAL_REVIEW_SCHEMA,
    TOPIC_PACKAGE_EDITOR_SCHEMA,
    MONITORING_CYCLE_SCHEMA,
    RESEARCH_TASK_SCHEMA,
    ARTICLE_DRAFT_SCHEMA,
    SOURCE_SYNC_AND_WEEKLY_BRIEF_SCHEMA,
    WEEKLY_ARTICLE_SCHEMA,
    RESEARCH_SOURCE_MEDIA_TYPE_SCHEMA,
    SIGNAL_PRIORITY_SCHEMA,
    NEWS_SOURCE_WHITELIST_SCHEMA,
    NEWS_ITEM_SELECTION_SCHEMA,
    NEWS_ARTICLE_SCHEMA,
    NEWS_WECHAT_PUSH_SCHEMA,
    NEWS_SOURCE_PARSER_CONFIG_SCHEMA,
    NEWS_ARTICLE_ITEMS_SCHEMA,
)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


def initialize_schema(path: Path, *, migrations: Sequence[Migration] = MIGRATIONS) -> MigrationResult:
    connection = connect_database(path)
    try:
        return apply_migrations(connection, migrations=migrations)
    finally:
        connection.close()


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> MigrationResult:
    ordered = _validated_migrations(migrations)
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT ({UTC_NOW})
        )
        """
    )
    applied = tuple(
        int(row["version"])
        for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
    )
    _validate_applied_chain(applied, ordered)
    starting_version = applied[-1] if applied else 0
    applied_now: list[int] = []

    for migration in ordered[len(applied) :]:
        with transaction(connection):
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
        applied_now.append(migration.version)

    ending_version = ordered[-1].version if ordered else 0
    return MigrationResult(starting_version, ending_version, tuple(applied_now))


def inspect_schema(path: Path, *, migrations: Sequence[Migration] = MIGRATIONS) -> SchemaStatus:
    ordered = _validated_migrations(migrations)
    latest = ordered[-1].version if ordered else 0
    if not path.is_file():
        return SchemaStatus("not_initialized", 0, latest, "database is not initialized")

    try:
        connection = connect_database(path, readonly=True)
    except sqlite3.Error as exc:
        return SchemaStatus("invalid", 0, latest, f"database cannot be opened: {exc}")
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if table is None:
            return SchemaStatus("invalid", 0, latest, "schema_migrations table is missing")
        applied = tuple(
            int(row["version"])
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        )
        _validate_applied_chain(applied, ordered)
        current = applied[-1] if applied else 0
        if current == latest:
            return SchemaStatus("up_to_date", current, latest, f"schema version {current}")
        return SchemaStatus("outdated", current, latest, f"schema version {current}; latest is {latest}")
    except (sqlite3.Error, SchemaVersionError) as exc:
        return SchemaStatus("invalid", 0, latest, str(exc))
    finally:
        connection.close()


def _validated_migrations(migrations: Sequence[Migration]) -> tuple[Migration, ...]:
    ordered = tuple(sorted(migrations, key=lambda migration: migration.version))
    versions = tuple(migration.version for migration in ordered)
    expected = tuple(range(1, len(ordered) + 1))
    if versions != expected:
        raise SchemaVersionError(f"Migration versions must be contiguous from 1; got {versions}")
    if any(not migration.name.strip() for migration in ordered):
        raise SchemaVersionError("Migration names must not be empty")
    return ordered


def _validate_applied_chain(applied: tuple[int, ...], migrations: Sequence[Migration]) -> None:
    known = tuple(migration.version for migration in migrations)
    if applied != known[: len(applied)]:
        raise SchemaVersionError(f"Applied migration chain {applied} does not match known chain {known}")
