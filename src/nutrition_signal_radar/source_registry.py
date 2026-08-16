"""Editable, auditable source configuration for the formal RedFox pipeline.

All monitored public accounts start disabled.  A validation result may be
recorded here, but the operator must explicitly enable a source before sync can
make billable RedFox calls.  Priority websites are only research preferences;
they are not an internet whitelist and are also seeded disabled.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .database import connect_database, transaction
from .schema import initialize_schema


SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
_VALIDATION_STATUSES = {"pending", "matched", "not_found", "error", "paused"}
_REDFOX_LIBRARIES = {"quality", "wide"}


class SourceRegistryError(ValueError):
    """Raised when an operator action would bypass a source-control gate."""


@dataclass(frozen=True)
class SourceAccount:
    id: int
    name: str
    redfox_name: str | None
    redfox_account: str | None
    redfox_library: str
    segment: str
    priority: int
    enabled: bool
    validation_status: str
    sync_health: str
    last_synced_at: str | None
    last_article_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "redfox_name": self.redfox_name,
            "redfox_account": self.redfox_account,
            "redfox_library": self.redfox_library,
            "segment": self.segment,
            "priority": self.priority,
            "enabled": self.enabled,
            "validation_status": self.validation_status,
            "sync_health": self.sync_health,
            "last_synced_at": self.last_synced_at,
            "last_article_at": self.last_article_at,
        }


@dataclass(frozen=True)
class PrioritySite:
    id: int
    name: str
    domain: str
    category: str
    source_role: str
    priority: int
    enabled: bool
    applicable_topics: tuple[str, ...]
    evidence_policy: str
    derived_from: str | None
    last_validated_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "domain": self.domain,
            "category": self.category,
            "source_role": self.source_role,
            "priority": self.priority,
            "enabled": self.enabled,
            "applicable_topics": list(self.applicable_topics),
            "evidence_policy": self.evidence_policy,
            "derived_from": self.derived_from,
            "last_validated_at": self.last_validated_at,
        }


@dataclass(frozen=True)
class PrioritySiteSeed:
    name: str
    domain: str
    category: str
    source_role: str
    priority: int
    evidence_policy: str


@dataclass(frozen=True)
class PrioritySiteSeedResult:
    database_path: Path
    inserted: int
    already_present: int

    def as_dict(self) -> dict[str, object]:
        return {
            "database_path": str(self.database_path),
            "inserted": self.inserted,
            "already_present": self.already_present,
            "total_seeded": self.inserted + self.already_present,
        }


_CORPORATE_POLICY = "仅作为企业自身公开动作或主张的一手候选；功效、科研和监管结论须补充适当一手来源。"
_MEDIA_POLICY = "仅作发现线索；重要主张必须回到监管、论文、信披或企业原始页面。"
_PRIORITY_SITE_SEEDS: tuple[PrioritySiteSeed, ...] = (
    PrioritySiteSeed("Novonesis", "novonesis.com", "company", "企业动作与公开材料的一手候选", 70, _CORPORATE_POLICY),
    PrioritySiteSeed("微康生物", "wecare-bio.com", "company", "企业动作与公开材料的一手候选", 70, _CORPORATE_POLICY),
    PrioritySiteSeed("Lallemand Health Solutions", "lallemand-health-solutions.com", "company", "企业动作与公开材料的一手候选", 70, _CORPORATE_POLICY),
    PrioritySiteSeed("森永乳业", "morinagamilk.co.jp", "company", "企业动作与公开材料的一手候选", 70, _CORPORATE_POLICY),
    PrioritySiteSeed("ADM", "adm.com", "company", "企业动作与公开材料的一手候选", 70, _CORPORATE_POLICY),
    PrioritySiteSeed("Sacco System", "saccosystem.com", "company", "企业动作与公开材料的一手候选", 70, _CORPORATE_POLICY),
    PrioritySiteSeed("Probi", "probi.com", "company", "企业动作与公开材料的一手候选", 70, _CORPORATE_POLICY),
    PrioritySiteSeed("AB-Biotics", "ab-biotics.com", "company", "企业动作与公开材料的一手候选", 70, _CORPORATE_POLICY),
    PrioritySiteSeed("科拓生物", "scitop.cn", "company", "企业动作与公开材料的一手候选", 70, _CORPORATE_POLICY),
    PrioritySiteSeed("WonderLab / 万益蓝", "wonderlab.com", "company", "品牌、产品和渠道动作观察", 60, _CORPORATE_POLICY),
    PrioritySiteSeed("IFF", "iff.com", "company", "企业动作与公开材料的一手候选", 70, _CORPORATE_POLICY),
    PrioritySiteSeed("Nutiani", "nutiani.com", "company", "企业和原料方案观察", 60, _CORPORATE_POLICY),
    PrioritySiteSeed("一然生物", "nature-bright.com", "company", "企业动作与公开材料的一手候选", 70, _CORPORATE_POLICY),
    PrioritySiteSeed("仙乐健康", "sirio.com", "company", "企业动作与公开材料的一手候选", 70, _CORPORATE_POLICY),
    PrioritySiteSeed("Kerry", "kerry.com", "company", "企业和原料方案观察", 60, _CORPORATE_POLICY),
    PrioritySiteSeed("DSM-Firmenich", "dsm-firmenich.com", "company", "企业和原料方案观察", 60, _CORPORATE_POLICY),
    PrioritySiteSeed("Danone", "danone.com", "company", "品牌、产品和营养业务观察", 60, _CORPORATE_POLICY),
    PrioritySiteSeed("Yakult", "yakult.co.jp", "company", "品牌、产品和科研叙事观察", 60, _CORPORATE_POLICY),
    PrioritySiteSeed("Nestlé Health Science", "nestlehealthscience.com", "company", "品牌、产品和营养业务观察", 60, _CORPORATE_POLICY),
    PrioritySiteSeed("润盈生物", "biothinking.com", "company", "企业动作与公开材料的一手候选", 70, _CORPORATE_POLICY),
    PrioritySiteSeed("Lesaffre", "lesaffre.com", "company", "企业和原料方案观察", 60, _CORPORATE_POLICY),
    PrioritySiteSeed("巨潮资讯", "cninfo.com.cn", "disclosure", "上市公司公告和信披的一手入口", 90, "具体公告页可作为信披事实的一手来源；首页不能直接作为证据。"),
    PrioritySiteSeed("Fi Global China", "figlobal.com", "event", "展会日程、参展及活动信息的一手候选", 60, "具体活动页可作为日程、参展或活动信息的一手候选；首页不能直接作为证据。"),
    PrioritySiteSeed("iHerb", "iherb.com", "retail", "跨境零售、产品和促销观察", 50, "仅作产品和促销观察；不得单独证明市场规模或功效。"),
    PrioritySiteSeed("中国网商业", "business.china.com.cn", "media", "新闻线索", 40, _MEDIA_POLICY),
    PrioritySiteSeed("今日头条", "toutiao.com", "media", "舆情和转载线索", 30, _MEDIA_POLICY),
    PrioritySiteSeed("东方财富财富号", "caifuhao.eastmoney.com", "media", "用户内容和财经线索", 30, _MEDIA_POLICY),
    PrioritySiteSeed("中国网", "china.com.cn", "media", "新闻线索", 40, _MEDIA_POLICY),
    PrioritySiteSeed("新浪财经", "money.finance.sina.com.cn", "media", "财经线索", 40, _MEDIA_POLICY),
    PrioritySiteSeed("凤凰网", "i.ifeng.com", "media", "新闻或转载线索", 30, _MEDIA_POLICY),
    PrioritySiteSeed("证券日报网", "zqrb.cn", "media", "财经新闻线索", 40, _MEDIA_POLICY),
)


def seed_priority_sites(
    database_path: Path,
    *,
    actor: str = "operator",
) -> PrioritySiteSeedResult:
    """Insert the documented 31-site seed once without overwriting human edits."""

    initialize_schema(database_path)
    connection = connect_database(database_path)
    inserted = 0
    already_present = 0
    try:
        with transaction(connection):
            for seed in _PRIORITY_SITE_SEEDS:
                existing = connection.execute(
                    "SELECT id FROM priority_sites WHERE domain = ?", (seed.domain,)
                ).fetchone()
                if existing is not None:
                    already_present += 1
                    continue
                cursor = connection.execute(
                    """
                    INSERT INTO priority_sites(
                        name, domain, category, source_role, priority, enabled,
                        applicable_topics_json, evidence_policy, derived_from
                    ) VALUES (?, ?, ?, ?, ?, 0, '[]', ?, 'SOURCE_REGISTRY_DRAFT.md')
                    """,
                    (
                        seed.name,
                        seed.domain,
                        seed.category,
                        seed.source_role,
                        seed.priority,
                        seed.evidence_policy,
                    ),
                )
                _record_event(
                    connection,
                    entity_kind="priority_site",
                    entity_id=int(cursor.lastrowid),
                    event_type="seeded_disabled",
                    actor=actor,
                    payload={"domain": seed.domain, "derived_from": "SOURCE_REGISTRY_DRAFT.md"},
                )
                inserted += 1
    finally:
        connection.close()
    return PrioritySiteSeedResult(
        database_path=Path(database_path).resolve(),
        inserted=inserted,
        already_present=already_present,
    )


def configure_source_account(
    database_path: Path,
    *,
    name: str,
    redfox_name: str | None,
    redfox_account: str | None,
    redfox_library: str = "quality",
    priority: int = 50,
    enabled: bool = False,
    validation_status: str = "pending",
    actor: str = "operator",
) -> SourceAccount:
    """Create or edit a source, enforcing confirmation before it may be enabled."""

    values = _validated_account_values(
        name=name,
        redfox_name=redfox_name,
        redfox_account=redfox_account,
        redfox_library=redfox_library,
        priority=priority,
        enabled=enabled,
        validation_status=validation_status,
    )
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            existing = connection.execute(
                "SELECT id FROM source_accounts WHERE name = ?", (values["name"],)
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO source_accounts(
                        name, redfox_name, redfox_account, redfox_library, segment,
                        priority, enabled, validation_status, sync_health
                    ) VALUES (?, ?, ?, ?, 'industry_media', ?, ?, ?, ?)
                    """,
                    (
                        values["name"],
                        values["redfox_name"],
                        values["redfox_account"],
                        values["redfox_library"],
                        values["priority"],
                        int(values["enabled"]),
                        values["validation_status"],
                        "unknown" if values["enabled"] else "paused",
                    ),
                )
                account_id = int(cursor.lastrowid)
                event_type = "configured"
            else:
                account_id = int(existing["id"])
                connection.execute(
                    f"""
                    UPDATE source_accounts SET
                        redfox_name = ?, redfox_account = ?, redfox_library = ?,
                        priority = ?, enabled = ?, validation_status = ?,
                        sync_health = CASE WHEN ? = 0 THEN 'paused' ELSE sync_health END,
                        updated_at = ({SQL_NOW})
                    WHERE id = ?
                    """,
                    (
                        values["redfox_name"],
                        values["redfox_account"],
                        values["redfox_library"],
                        values["priority"],
                        int(values["enabled"]),
                        values["validation_status"],
                        int(values["enabled"]),
                        account_id,
                    ),
                )
                event_type = "reconfigured"
            _record_event(
                connection,
                entity_kind="account",
                entity_id=account_id,
                event_type=event_type,
                actor=actor,
                payload={
                    "name": values["name"],
                    "redfox_library": values["redfox_library"],
                    "enabled": values["enabled"],
                    "validation_status": values["validation_status"],
                },
            )
            row = _source_account_row(connection, account_id)
            if row is None:
                raise SourceRegistryError("保存来源配置后无法读取记录")
    finally:
        connection.close()
    return _source_account(row)


def set_source_account_enabled(
    database_path: Path,
    *,
    name: str,
    enabled: bool,
    actor: str = "operator",
    reason: str | None = None,
) -> SourceAccount:
    """Explicitly enable or pause one configured source and leave an audit event."""

    cleaned_name = _clean_required(name, "来源名称")
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            row = connection.execute(
                "SELECT * FROM source_accounts WHERE name = ?", (cleaned_name,)
            ).fetchone()
            if row is None:
                raise SourceRegistryError(f"来源不存在：{cleaned_name}")
            account = _source_account(row)
            if enabled:
                _assert_enable_ready(account)
            connection.execute(
                f"""
                UPDATE source_accounts SET
                    enabled = ?,
                    sync_health = CASE WHEN ? = 1 THEN 'unknown' ELSE 'paused' END,
                    updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (int(enabled), int(enabled), account.id),
            )
            _record_event(
                connection,
                entity_kind="account",
                entity_id=account.id,
                event_type="enabled" if enabled else "disabled",
                actor=actor,
                reason=(reason or "").strip() or None,
                payload={"name": account.name, "redfox_library": account.redfox_library},
            )
            updated = _source_account_row(connection, account.id)
            if updated is None:
                raise SourceRegistryError("更新来源状态后无法读取记录")
    finally:
        connection.close()
    return _source_account(updated)


def list_source_accounts(database_path: Path) -> list[SourceAccount]:
    """Return source configuration, including disabled proposals, without exposing secrets."""

    initialize_schema(database_path)
    connection = connect_database(database_path, readonly=True)
    try:
        return [
            _source_account(row)
            for row in connection.execute(
                "SELECT * FROM source_accounts ORDER BY enabled DESC, priority DESC, name"
            )
        ]
    finally:
        connection.close()


def list_priority_sites(database_path: Path) -> list[PrioritySite]:
    """List public-research preferences; disabled entries remain visible."""

    initialize_schema(database_path)
    connection = connect_database(database_path, readonly=True)
    try:
        return [
            _priority_site(row)
            for row in connection.execute(
                "SELECT * FROM priority_sites ORDER BY enabled DESC, priority DESC, domain"
            )
        ]
    finally:
        connection.close()


def configure_priority_site(
    database_path: Path,
    *,
    name: str,
    domain: str,
    category: str,
    source_role: str,
    evidence_policy: str,
    priority: int = 50,
    enabled: bool = False,
    applicable_topics: Iterable[str] = (),
    derived_from: str | None = None,
    actor: str = "operator",
) -> PrioritySite:
    """Create or edit one research-priority site without treating it as a whitelist."""

    cleaned_name = _clean_required(name, "网站名称")
    cleaned_domain = _clean_domain(domain)
    cleaned_category = _clean_required(category, "网站类别")
    cleaned_role = _clean_required(source_role, "网站角色")
    cleaned_policy = _clean_required(evidence_policy, "证据规则")
    if not isinstance(priority, int) or not 0 <= priority <= 100:
        raise SourceRegistryError("网站优先级必须在 0—100 之间")
    topics = tuple(dict.fromkeys(str(topic).strip() for topic in applicable_topics if str(topic).strip()))
    cleaned_derived_from = str(derived_from or "").strip() or None

    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            existing = connection.execute(
                "SELECT id FROM priority_sites WHERE domain = ?", (cleaned_domain,)
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO priority_sites(
                        name, domain, category, source_role, priority, enabled,
                        applicable_topics_json, evidence_policy, derived_from
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cleaned_name,
                        cleaned_domain,
                        cleaned_category,
                        cleaned_role,
                        priority,
                        int(enabled),
                        json.dumps(topics, ensure_ascii=False),
                        cleaned_policy,
                        cleaned_derived_from,
                    ),
                )
                site_id = int(cursor.lastrowid)
                event_type = "configured"
            else:
                site_id = int(existing["id"])
                connection.execute(
                    f"""
                    UPDATE priority_sites SET
                        name = ?, category = ?, source_role = ?, priority = ?, enabled = ?,
                        applicable_topics_json = ?, evidence_policy = ?, derived_from = ?,
                        updated_at = ({SQL_NOW})
                    WHERE id = ?
                    """,
                    (
                        cleaned_name,
                        cleaned_category,
                        cleaned_role,
                        priority,
                        int(enabled),
                        json.dumps(topics, ensure_ascii=False),
                        cleaned_policy,
                        cleaned_derived_from,
                        site_id,
                    ),
                )
                event_type = "reconfigured"
            _record_event(
                connection,
                entity_kind="priority_site",
                entity_id=site_id,
                event_type=event_type,
                actor=actor,
                payload={"domain": cleaned_domain, "enabled": bool(enabled), "priority": priority},
            )
            row = _priority_site_row(connection, site_id)
            if row is None:
                raise SourceRegistryError("保存重点网站后无法读取记录")
    finally:
        connection.close()
    return _priority_site(row)


def set_priority_site_enabled(
    database_path: Path,
    *,
    domain: str,
    enabled: bool,
    actor: str = "operator",
    reason: str | None = None,
) -> PrioritySite:
    """Enable or pause one research preference without changing Agent scope."""

    cleaned_domain = _clean_domain(domain)
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            row = connection.execute(
                "SELECT * FROM priority_sites WHERE domain = ?", (cleaned_domain,)
            ).fetchone()
            if row is None:
                raise SourceRegistryError(f"重点网站不存在：{cleaned_domain}")
            site = _priority_site(row)
            connection.execute(
                f"UPDATE priority_sites SET enabled = ?, updated_at = ({SQL_NOW}) WHERE id = ?",
                (int(enabled), site.id),
            )
            _record_event(
                connection,
                entity_kind="priority_site",
                entity_id=site.id,
                event_type="enabled" if enabled else "disabled",
                actor=actor,
                reason=(reason or "").strip() or None,
                payload={"domain": site.domain},
            )
            updated = _priority_site_row(connection, site.id)
            if updated is None:
                raise SourceRegistryError("更新重点网站状态后无法读取记录")
    finally:
        connection.close()
    return _priority_site(updated)


def list_enabled_redfox_sources(database_path: Path) -> list[SourceAccount]:
    """Return only sources that have passed explicit validation and enablement gates."""

    return [
        account
        for account in list_source_accounts(database_path)
        if account.enabled and account.validation_status == "matched"
    ]


def record_source_sync_health(
    database_path: Path,
    *,
    account_id: int,
    health: str,
    actor: str = "sync",
    reason: str | None = None,
) -> SourceAccount:
    """Record one source's outcome without letting a failed source block others."""

    if health not in {"healthy", "warning", "error", "paused"}:
        raise SourceRegistryError("抓取健康状态无效")
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            row = _source_account_row(connection, account_id)
            if row is None:
                raise SourceRegistryError(f"来源 ID 不存在：{account_id}")
            connection.execute(
                f"""
                UPDATE source_accounts SET
                    sync_health = ?,
                    last_synced_at = CASE WHEN ? = 'healthy' THEN ({SQL_NOW}) ELSE last_synced_at END,
                    updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (health, health, account_id),
            )
            _record_event(
                connection,
                entity_kind="account",
                entity_id=account_id,
                event_type="sync_succeeded" if health == "healthy" else "sync_failed",
                actor=actor,
                reason=(reason or "").strip()[:500] or None,
                payload={"health": health},
            )
            updated = _source_account_row(connection, account_id)
            if updated is None:
                raise SourceRegistryError("更新抓取状态后无法读取记录")
    finally:
        connection.close()
    return _source_account(updated)


def priority_site_seed_count() -> int:
    """Expose the documented seed size for tests and operator-facing status."""

    return len(_PRIORITY_SITE_SEEDS)


def _validated_account_values(
    *,
    name: str,
    redfox_name: str | None,
    redfox_account: str | None,
    redfox_library: str,
    priority: int,
    enabled: bool,
    validation_status: str,
) -> dict[str, object]:
    cleaned_name = _clean_required(name, "来源名称")
    cleaned_redfox_name = str(redfox_name or "").strip() or None
    cleaned_redfox_account = str(redfox_account or "").strip() or None
    library = str(redfox_library).strip().lower()
    status = str(validation_status).strip().lower()
    if library not in _REDFOX_LIBRARIES:
        raise SourceRegistryError("RedFox 库类型必须是 quality 或 wide")
    if status not in _VALIDATION_STATUSES:
        raise SourceRegistryError("来源校验状态无效")
    if not isinstance(priority, int) or not 0 <= priority <= 100:
        raise SourceRegistryError("来源优先级必须在 0—100 之间")
    account = SourceAccount(
        id=0,
        name=cleaned_name,
        redfox_name=cleaned_redfox_name,
        redfox_account=cleaned_redfox_account,
        redfox_library=library,
        segment="industry_media",
        priority=priority,
        enabled=bool(enabled),
        validation_status=status,
        sync_health="unknown",
        last_synced_at=None,
        last_article_at=None,
    )
    if account.enabled:
        _assert_enable_ready(account)
    return {
        "name": account.name,
        "redfox_name": account.redfox_name,
        "redfox_account": account.redfox_account,
        "redfox_library": account.redfox_library,
        "priority": account.priority,
        "enabled": account.enabled,
        "validation_status": account.validation_status,
    }


def _assert_enable_ready(account: SourceAccount) -> None:
    if account.validation_status != "matched":
        raise SourceRegistryError("来源必须先经人工确认并标记为 matched，才可启用")
    if not account.redfox_name:
        raise SourceRegistryError("启用来源前必须填写 RedFox 精确展示名")
    if account.redfox_library == "quality" and not account.redfox_account:
        raise SourceRegistryError("优质库来源启用前必须填写 RedFox account 标识")


def _source_account_row(connection: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM source_accounts WHERE id = ?", (account_id,)).fetchone()


def _source_account(row: sqlite3.Row) -> SourceAccount:
    return SourceAccount(
        id=int(row["id"]),
        name=str(row["name"]),
        redfox_name=str(row["redfox_name"] or "").strip() or None,
        redfox_account=str(row["redfox_account"] or "").strip() or None,
        redfox_library=str(row["redfox_library"] or "quality"),
        segment=str(row["segment"]),
        priority=int(row["priority"]),
        enabled=bool(row["enabled"]),
        validation_status=str(row["validation_status"]),
        sync_health=str(row["sync_health"]),
        last_synced_at=row["last_synced_at"],
        last_article_at=row["last_article_at"],
    )


def _priority_site_row(connection: sqlite3.Connection, site_id: int) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM priority_sites WHERE id = ?", (site_id,)).fetchone()


def _priority_site(row: sqlite3.Row) -> PrioritySite:
    raw_topics = str(row["applicable_topics_json"] or "[]")
    try:
        decoded = json.loads(raw_topics)
    except json.JSONDecodeError:
        decoded = []
    topics = tuple(str(item) for item in decoded if isinstance(item, str)) if isinstance(decoded, list) else ()
    return PrioritySite(
        id=int(row["id"]),
        name=str(row["name"]),
        domain=str(row["domain"]),
        category=str(row["category"]),
        source_role=str(row["source_role"]),
        priority=int(row["priority"]),
        enabled=bool(row["enabled"]),
        applicable_topics=topics,
        evidence_policy=str(row["evidence_policy"]),
        derived_from=str(row["derived_from"] or "").strip() or None,
        last_validated_at=row["last_validated_at"],
    )


def _record_event(
    connection: sqlite3.Connection,
    *,
    entity_kind: str,
    entity_id: int,
    event_type: str,
    actor: str,
    reason: str | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO source_registry_events(
            entity_kind, entity_id, event_type, actor, reason, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            entity_kind,
            entity_id,
            event_type,
            (actor or "operator").strip() or "operator",
            reason,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def _clean_required(value: str, label: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned or len(cleaned) > 120:
        raise SourceRegistryError(f"{label}必须为 1—120 个字符")
    return cleaned


def _clean_domain(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    if cleaned.startswith(("https://", "http://")):
        cleaned = cleaned.split("://", 1)[1]
    cleaned = cleaned.strip("/")
    if not cleaned or "/" in cleaned or " " in cleaned or len(cleaned) > 255:
        raise SourceRegistryError("网站域名格式无效")
    return cleaned
