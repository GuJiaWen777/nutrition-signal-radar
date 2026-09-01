"""Auditable whitelist registry for the daily news line (Phase 4).

The news line fetches directly from a strict whitelist of primary publishers
(regulators, statutory disclosure platforms, brand official sites, expo
organizers, and research/industry bodies).  Every entry starts disabled; an
operator must explicitly enable each source after confirming the initial list
(ADR-0014).  This registry never performs network requests itself.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .database import connect_database, transaction
from .schema import initialize_schema


SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
_NEWS_CATEGORIES = {"policy", "disclosure", "brand", "expo", "research"}
_NEWS_FETCH_KINDS = {"html_list", "api", "rss"}
_NEWS_PRIORITIES = {"P0", "P1", "P2", "P3"}


class NewsRegistryError(ValueError):
    """Raised when an operator action would bypass a news-whitelist gate."""


@dataclass(frozen=True)
class NewsSource:
    id: int
    name: str
    domain: str
    category: str
    entry_url: str
    fetch_kind: str
    default_priority: str
    parser_version: str
    enabled: bool
    sync_health: str
    last_fetched_at: str | None
    last_item_at: str | None
    notes: str
    derived_from: str | None
    parser_config: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "domain": self.domain,
            "category": self.category,
            "entry_url": self.entry_url,
            "fetch_kind": self.fetch_kind,
            "default_priority": self.default_priority,
            "parser_version": self.parser_version,
            "enabled": self.enabled,
            "sync_health": self.sync_health,
            "last_fetched_at": self.last_fetched_at,
            "last_item_at": self.last_item_at,
            "notes": self.notes,
            "derived_from": self.derived_from,
            "parser_config": self.parser_config,
        }


@dataclass(frozen=True)
class NewsSourceSeed:
    name: str
    domain: str
    category: str
    entry_url: str
    fetch_kind: str
    default_priority: str
    notes: str = ""


@dataclass(frozen=True)
class NewsSourceSeedResult:
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


_NEWS_SOURCE_SEEDS: tuple[NewsSourceSeed, ...] = (
    # 政策监管（P0）
    NewsSourceSeed("国家市场监督管理总局", "samr.gov.cn", "policy", "https://www.samr.gov.cn/tssps/", "html_list", "P0", "特殊食品司公告、征求意见稿与监管通报。"),
    NewsSourceSeed("国家卫生健康委员会", "nhc.gov.cn", "policy", "https://www.nhc.gov.cn/", "html_list", "P0", "食品安全标准、新食品原料与公告。"),
    NewsSourceSeed("海关总署", "customs.gov.cn", "policy", "https://www.customs.gov.cn/", "html_list", "P0", "跨境营养品进口、通关与查缉动态。"),
    NewsSourceSeed("商务部", "mofcom.gov.cn", "policy", "https://www.mofcom.gov.cn/", "html_list", "P0", "行业相关政策发布。"),
    NewsSourceSeed("国家食品安全风险评估中心", "cfsa.net.cn", "policy", "https://www.cfsa.net.cn/", "html_list", "P0", "原料评估、标准跟踪与公开征求意见。"),
    NewsSourceSeed("北京市市场监督管理局", "scjgj.beijing.gov.cn", "policy", "https://scjgj.beijing.gov.cn/", "html_list", "P0", "重点省市市场监管与处罚通报。"),
    NewsSourceSeed("上海市市场监督管理局", "scjgj.sh.gov.cn", "policy", "https://scjgj.sh.gov.cn/", "html_list", "P0", "重点省市市场监管与处罚通报。"),
    # 法定披露（P1）
    NewsSourceSeed("巨潮资讯网", "cninfo.com.cn", "disclosure", "http://www.cninfo.com.cn/new/hisAnnouncement/query", "api", "P1", "深市上市公司公告查询接口；覆盖面与市场参数在启用后按实调优。"),
    NewsSourceSeed("港交所披露易", "hkexnews.hk", "disclosure", "https://www1.hkexnews.hk/search/titlesearch.xhtml", "api", "P1", "港股上市公司披露文件检索；解析器在启用后按实配置。"),
    NewsSourceSeed("SEC EDGAR", "sec.gov", "disclosure", "https://www.sec.gov/edgar/", "api", "P1", "在美上市公司披露与全文检索接口。"),
    # 品牌与公司动态（P1/P2）
    NewsSourceSeed("汤臣倍健", "by-health.com", "brand", "https://www.by-health.com/", "html_list", "P1", "上市公司官方新闻与投资者关系发布。"),
    NewsSourceSeed("仙乐健康", "sirio.com", "brand", "https://www.sirio.com/", "html_list", "P1", "上市公司官方新闻与投资者关系发布。"),
    NewsSourceSeed("东阿阿胶", "dongeejiao.com", "brand", "https://www.dongeejiao.com/", "html_list", "P1", "上市公司官方新闻与投资者关系发布。"),
    NewsSourceSeed("金达威", "kingdomway.com", "brand", "https://www.kingdomway.com/", "html_list", "P1", "上市公司官方新闻与投资者关系发布。"),
    NewsSourceSeed("康恩贝", "conbagroup.com", "brand", "https://www.conbagroup.com/", "html_list", "P1", "上市公司官方新闻与投资者关系发布。"),
    NewsSourceSeed("科拓生物", "scitop.cn", "brand", "http://www.scitop.cn/", "html_list", "P1", "上市公司官方新闻与投资者关系发布。"),
    NewsSourceSeed("WonderLab / 万益蓝", "wonderlab.com", "brand", "https://www.wonderlab.com/", "html_list", "P2", "未上市品牌官网新闻中心；不采集第三方传闻。"),
    NewsSourceSeed("森永乳业", "morinagamilk.co.jp", "brand", "https://www.morinagamilk.co.jp/", "html_list", "P2", "海外上市公司官方发布（日文）。"),
    NewsSourceSeed("ADM", "adm.com", "brand", "https://www.adm.com/", "html_list", "P2", "原料企业官方新闻室。"),
    NewsSourceSeed("Novonesis", "novonesis.com", "brand", "https://www.novonesis.com/", "html_list", "P2", "原料企业官方新闻室。"),
    NewsSourceSeed("Probi", "probi.com", "brand", "https://www.probi.com/", "html_list", "P2", "原料企业官方新闻室。"),
    # 展会与产业活动（P2）
    NewsSourceSeed("HNC 健康营养展", "hncexpo.com", "expo", "https://www.hncexpo.com/", "html_list", "P2", "主办方官网；区分展会预告、正在举办与会后回顾。"),
    NewsSourceSeed("NHNE 健康营养博览会", "nhnexpo.com", "expo", "https://www.nhnexpo.com/", "html_list", "P2", "主办方官网；区分展会预告、正在举办与会后回顾。"),
    NewsSourceSeed("Fi Global China", "figlobal.com", "expo", "https://www.figlobal.com/", "html_list", "P2", "健康天然原料展主办方官网。"),
    NewsSourceSeed("CPHI China", "cphi.com", "expo", "https://www.cphi.com/", "html_list", "P2", "制药原料展主办方官网；关注特医与营养相关板块。"),
    NewsSourceSeed("SupplySide West", "supplysideshow.com", "expo", "https://supplysideshow.com/", "html_list", "P2", "海外营养原料展主办方官网。"),
    # 科研与行业组织（P2）
    NewsSourceSeed("中国营养保健食品协会", "cnhfa.org.cn", "research", "https://www.cnhfa.org.cn/", "html_list", "P2", "全国性行业组织；政策法规与团体标准发布。"),
    NewsSourceSeed("中国营养学会", "cnsoc.org", "research", "https://www.cnsoc.org/", "html_list", "P2", "学术团体；膳食指南与科研发布。"),
    NewsSourceSeed("中国食品科学技术学会", "cifst.org.cn", "research", "https://www.cifst.org.cn/", "html_list", "P2", "学术团体；技术进展与团体标准发布。"),
)


def seed_news_sources(
    database_path: Path,
    *,
    actor: str = "operator",
) -> NewsSourceSeedResult:
    """Insert the documented news-whitelist seed once without enabling anything."""

    initialize_schema(database_path)
    connection = connect_database(database_path)
    inserted = 0
    already_present = 0
    try:
        with transaction(connection):
            for seed in _NEWS_SOURCE_SEEDS:
                existing = connection.execute(
                    "SELECT id FROM news_sources WHERE domain = ?", (seed.domain,)
                ).fetchone()
                if existing is not None:
                    already_present += 1
                    continue
                cursor = connection.execute(
                    """
                    INSERT INTO news_sources(
                        name, domain, category, entry_url, fetch_kind,
                        default_priority, parser_version, enabled, notes, derived_from
                    ) VALUES (?, ?, ?, ?, ?, ?, 'v1', 0, ?, ?)
                    """,
                    (
                        seed.name,
                        seed.domain,
                        seed.category,
                        seed.entry_url,
                        seed.fetch_kind,
                        seed.default_priority,
                        seed.notes,
                        "docs/DSA自动化文章需求.docx + ADR-0014",
                    ),
                )
                _record_event(
                    connection,
                    news_source_id=int(cursor.lastrowid),
                    event_type="seeded_disabled",
                    actor=actor,
                    payload={"domain": seed.domain, "default_priority": seed.default_priority},
                )
                inserted += 1
    finally:
        connection.close()
    return NewsSourceSeedResult(
        database_path=Path(database_path).resolve(),
        inserted=inserted,
        already_present=already_present,
    )


def configure_news_source(
    database_path: Path,
    *,
    name: str,
    domain: str,
    category: str,
    entry_url: str,
    fetch_kind: str = "html_list",
    default_priority: str = "P2",
    parser_version: str = "v1",
    parser_config: str = "",
    notes: str = "",
    enabled: bool = False,
    actor: str = "operator",
) -> NewsSource:
    """Create or edit one whitelist entry; it stays disabled unless explicitly enabled."""

    cleaned_name = _clean_required(name, "信源名称")
    cleaned_domain = _clean_domain(domain)
    cleaned_category = _clean_category(category)
    cleaned_entry_url = _clean_entry_url(entry_url)
    cleaned_fetch_kind = _clean_fetch_kind(fetch_kind)
    cleaned_priority = _clean_priority(default_priority)
    cleaned_parser_version = _clean_required(parser_version, "解析配置版本")
    cleaned_parser_config = _clean_parser_config(parser_config)
    cleaned_notes = str(notes or "").strip()

    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            existing = connection.execute(
                "SELECT id FROM news_sources WHERE domain = ? OR name = ?",
                (cleaned_domain, cleaned_name),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO news_sources(
                        name, domain, category, entry_url, fetch_kind,
                        default_priority, parser_version, parser_config, enabled, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cleaned_name,
                        cleaned_domain,
                        cleaned_category,
                        cleaned_entry_url,
                        cleaned_fetch_kind,
                        cleaned_priority,
                        cleaned_parser_version,
                        cleaned_parser_config,
                        int(enabled),
                        cleaned_notes,
                    ),
                )
                source_id = int(cursor.lastrowid)
                event_type = "configured"
            else:
                source_id = int(existing["id"])
                connection.execute(
                    f"""
                    UPDATE news_sources SET
                        name = ?, domain = ?, category = ?, entry_url = ?, fetch_kind = ?,
                        default_priority = ?, parser_version = ?, parser_config = ?,
                        enabled = ?, notes = ?,
                        updated_at = ({SQL_NOW})
                    WHERE id = ?
                    """,
                    (
                        cleaned_name,
                        cleaned_domain,
                        cleaned_category,
                        cleaned_entry_url,
                        cleaned_fetch_kind,
                        cleaned_priority,
                        cleaned_parser_version,
                        cleaned_parser_config,
                        int(enabled),
                        cleaned_notes,
                        source_id,
                    ),
                )
                event_type = "reconfigured"
            _record_event(
                connection,
                news_source_id=source_id,
                event_type=event_type,
                actor=actor,
                payload={
                    "domain": cleaned_domain,
                    "enabled": bool(enabled),
                    "default_priority": cleaned_priority,
                    "parser_version": cleaned_parser_version,
                },
            )
            row = _news_source_row(connection, source_id)
            if row is None:
                raise NewsRegistryError("保存资讯信源后无法读取记录")
    finally:
        connection.close()
    return _news_source(row)


def set_news_source_enabled(
    database_path: Path,
    *,
    name: str,
    enabled: bool,
    actor: str = "operator",
    reason: str | None = None,
) -> NewsSource:
    """Explicitly enable or pause one whitelist entry; enabling is the human gate."""

    cleaned_name = _clean_required(name, "信源名称")
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            row = connection.execute(
                "SELECT * FROM news_sources WHERE name = ?", (cleaned_name,)
            ).fetchone()
            if row is None:
                raise NewsRegistryError(f"资讯信源不存在：{cleaned_name}")
            source = _news_source(row)
            connection.execute(
                f"""
                UPDATE news_sources SET
                    enabled = ?,
                    sync_health = CASE WHEN ? = 1 THEN 'unknown' ELSE 'paused' END,
                    updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (int(enabled), int(enabled), source.id),
            )
            _record_event(
                connection,
                news_source_id=source.id,
                event_type="enabled" if enabled else "disabled",
                actor=actor,
                reason=(reason or "").strip() or None,
                payload={"domain": source.domain},
            )
            updated = _news_source_row(connection, source.id)
            if updated is None:
                raise NewsRegistryError("更新资讯信源状态后无法读取记录")
    finally:
        connection.close()
    return _news_source(updated)


def list_news_sources(database_path: Path) -> list[NewsSource]:
    """List whitelist entries, including disabled proposals, without network calls."""

    initialize_schema(database_path)
    connection = connect_database(database_path, readonly=True)
    try:
        return [
            _news_source(row)
            for row in connection.execute(
                """
                SELECT * FROM news_sources
                ORDER BY enabled DESC,
                    CASE default_priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                    name
                """
            )
        ]
    finally:
        connection.close()


def list_enabled_news_sources(database_path: Path) -> list[NewsSource]:
    """Return only operator-enabled whitelist entries; fetchers must use this gate."""

    return [source for source in list_news_sources(database_path) if source.enabled]


def record_news_source_health(
    database_path: Path,
    *,
    source_id: int,
    health: str,
    latest_item_at: str | None = None,
    actor: str = "news-fetch",
    reason: str | None = None,
) -> NewsSource:
    """Record one fetch outcome without letting a failed source block others."""

    if health not in {"healthy", "warning", "error", "paused"}:
        raise NewsRegistryError("资讯抓取健康状态无效")
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        with transaction(connection):
            row = _news_source_row(connection, source_id)
            if row is None:
                raise NewsRegistryError(f"资讯信源 ID 不存在：{source_id}")
            connection.execute(
                f"""
                UPDATE news_sources SET
                    sync_health = ?,
                    last_fetched_at = ({SQL_NOW}),
                    last_item_at = CASE
                        WHEN ? IS NULL THEN last_item_at
                        WHEN last_item_at IS NULL THEN ?
                        WHEN ? > last_item_at THEN ?
                        ELSE last_item_at
                    END,
                    updated_at = ({SQL_NOW})
                WHERE id = ?
                """,
                (health, latest_item_at, latest_item_at, latest_item_at, latest_item_at, source_id),
            )
            _record_event(
                connection,
                news_source_id=source_id,
                event_type="fetch_succeeded" if health == "healthy" else "fetch_failed",
                actor=actor,
                reason=(reason or "").strip()[:500] or None,
                payload={"health": health},
            )
            updated = _news_source_row(connection, source_id)
            if updated is None:
                raise NewsRegistryError("更新资讯抓取状态后无法读取记录")
    finally:
        connection.close()
    return _news_source(updated)


def news_source_seed_count() -> int:
    """Expose the documented seed size for tests and operator-facing status."""

    return len(_NEWS_SOURCE_SEEDS)


def _record_event(
    connection: sqlite3.Connection,
    *,
    news_source_id: int,
    event_type: str,
    actor: str,
    reason: str | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO news_source_events(
            news_source_id, event_type, actor, reason, payload_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            news_source_id,
            event_type,
            (actor or "operator").strip() or "operator",
            reason,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def _news_source_row(connection: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM news_sources WHERE id = ?", (source_id,)).fetchone()


def _news_source(row: sqlite3.Row) -> NewsSource:
    return NewsSource(
        id=int(row["id"]),
        name=str(row["name"]),
        domain=str(row["domain"]),
        category=str(row["category"]),
        entry_url=str(row["entry_url"]),
        fetch_kind=str(row["fetch_kind"]),
        default_priority=str(row["default_priority"]),
        parser_version=str(row["parser_version"]),
        enabled=bool(row["enabled"]),
        sync_health=str(row["sync_health"]),
        last_fetched_at=row["last_fetched_at"],
        last_item_at=row["last_item_at"],
        notes=str(row["notes"]),
        derived_from=str(row["derived_from"] or "").strip() or None,
        parser_config=str(row["parser_config"] or "").strip(),
    )


def _clean_required(value: str, label: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned or len(cleaned) > 120:
        raise NewsRegistryError(f"{label}必须为 1—120 个字符")
    return cleaned


def _clean_category(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    if cleaned not in _NEWS_CATEGORIES:
        raise NewsRegistryError("资讯信源类别必须是 policy、disclosure、brand、expo 或 research")
    return cleaned


def _clean_fetch_kind(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    if cleaned not in _NEWS_FETCH_KINDS:
        raise NewsRegistryError("抓取方式必须是 html_list、api 或 rss")
    return cleaned


def _clean_priority(value: str) -> str:
    cleaned = str(value or "").strip().upper()
    if cleaned not in _NEWS_PRIORITIES:
        raise NewsRegistryError("默认资讯优先级必须是 P0、P1、P2 或 P3")
    return cleaned


def _clean_parser_config(value: str) -> str:
    """Validate the optional per-source parser config as a flat JSON object."""

    cleaned = str(value or "").strip()
    if not cleaned:
        return ""
    try:
        decoded = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise NewsRegistryError(f"解析配置必须是合法 JSON：{exc}") from exc
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) for key in decoded
    ):
        raise NewsRegistryError("解析配置必须是 JSON 对象")
    if len(cleaned) > 2000:
        raise NewsRegistryError("解析配置过长（上限 2000 字符）")
    return json.dumps(decoded, ensure_ascii=False, sort_keys=True)


def _clean_domain(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    if cleaned.startswith(("https://", "http://")):
        cleaned = cleaned.split("://", 1)[1]
    cleaned = cleaned.strip("/")
    if not cleaned or "/" in cleaned or " " in cleaned or len(cleaned) > 255:
        raise NewsRegistryError("信源域名格式无效")
    return cleaned


def _clean_entry_url(value: str) -> str:
    cleaned = str(value or "").strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise NewsRegistryError("抓取入口必须是 http(s) 完整 URL")
    if len(cleaned) > 2048:
        raise NewsRegistryError("抓取入口 URL 过长")
    return cleaned
