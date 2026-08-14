from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from .cycle_service import get_monitoring_cycle
from .database import connect_database, transaction
from .schema import initialize_schema


DEMO_GENERATOR = "demo_rules_v3"
DEMO_GENERATOR_PATTERN = "demo_rules_v%"
TITLE_SIMILARITY_THRESHOLD = 0.54
SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

# These terms are deliberately narrower than generic words such as “健康”、
# “营养” or “食品”.  Sharing one of them is strong enough evidence that two
# headlines belong to the same editorial topic, even when their wording and
# publication angle differ substantially.
STRONG_INDUSTRY_TOPIC_TERMS = (
    "益生菌",
    "后生元",
    "特医食品",
    "特殊医学用途配方食品",
    "跨境保健品",
    "药食同源",
    "体重管理",
    "情绪健康",
    "肠道健康",
    "银发营养",
    "口服美容",
)


@dataclass(frozen=True)
class DemoBuildResult:
    database_path: Path
    source_articles: int
    clusters: int
    multi_article_clusters: int
    signals: int
    candidates: int
    locked_clusters_preserved: int
    stale_clusters_removed: int
    cycle_id: int
    cycle_slug: str
    generator: str = DEMO_GENERATOR


@dataclass(frozen=True)
class _Article:
    id: int
    external_id: str
    account_id: int
    account_name: str
    title: str
    published_at: str | None
    url: str | None
    content_snapshot: str
    content_hash: str | None
    read_count: int
    like_count: int
    share_count: int
    recommend_count: int

    @property
    def engagement(self) -> int:
        return self.read_count + 4 * self.like_count + 3 * self.share_count + 2 * self.recommend_count


def build_demo_topics(
    database_path: Path, *, cycle_slug: str | None = None
) -> DemoBuildResult:
    """Build explainable title-based clusters and clearly labelled demo candidates."""
    initialize_schema(database_path)
    connection = connect_database(database_path)
    try:
        cycle = get_monitoring_cycle(connection, slug=cycle_slug)
        if cycle is None:
            raise ValueError("Demo database has no monitoring cycle; import articles first")
        if cycle["status"] != "open":
            raise ValueError("Cannot rebuild a closed monitoring cycle")
        cycle_id = int(cycle["id"])
        articles = _load_articles(connection, cycle_id)
        if not articles:
            raise ValueError(
                f"Monitoring cycle {cycle['slug']} has no in-period source articles"
            )

        groups, vectors, norms = _cluster_articles(articles)
        global_latest = max(
            (_parse_datetime(article.published_at) for article in articles if article.published_at),
            default=None,
        )
        locked_preserved = 0
        stale_removed = 0
        signal_count = 0
        candidate_count = 0
        active_cluster_keys = {
            _cluster_key([articles[index] for index in member_indexes])
            for member_indexes in groups
        }

        with transaction(connection):
            stale_removed = _remove_stale_generated_clusters(
                connection, cycle_id, active_cluster_keys
            )
            for member_indexes in groups:
                members = [articles[index] for index in member_indexes]
                representative_index = _choose_representative(member_indexes, articles)
                representative = articles[representative_index]
                cluster_key = _cluster_key(members)
                first_seen = min(
                    (article.published_at for article in members if article.published_at), default=None
                )
                last_seen = max(
                    (article.published_at for article in members if article.published_at), default=None
                )
                account_count = len({article.account_id for article in members})
                trend_state = _trend_state(first_seen, last_seen, len(members), global_latest)

                existing = connection.execute(
                    """
                    SELECT id, locked FROM topic_clusters
                    WHERE cycle_id = ? AND cluster_key = ?
                    """,
                    (cycle_id, cluster_key),
                ).fetchone()
                if existing is None:
                    cursor = connection.execute(
                        f"""
                        INSERT INTO topic_clusters(
                            canonical_name, first_seen_at, last_seen_at, article_count,
                            source_count, trend_state, cluster_key, generated_by, cycle_id,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ({SQL_NOW}), ({SQL_NOW}))
                        """,
                        (
                            representative.title,
                            first_seen,
                            last_seen,
                            len(members),
                            account_count,
                            trend_state,
                            cluster_key,
                            DEMO_GENERATOR,
                            cycle_id,
                        ),
                    )
                    cluster_id = int(cursor.lastrowid)
                else:
                    cluster_id = int(existing["id"])
                    if int(existing["locked"]):
                        locked_preserved += 1
                        continue
                    connection.execute(
                        f"""
                        UPDATE topic_clusters SET
                            canonical_name = ?, first_seen_at = ?, last_seen_at = ?,
                            article_count = ?, source_count = ?, trend_state = ?,
                            generated_by = ?, updated_at = ({SQL_NOW})
                        WHERE id = ?
                        """,
                        (
                            representative.title,
                            first_seen,
                            last_seen,
                            len(members),
                            account_count,
                            trend_state,
                            DEMO_GENERATOR,
                            cluster_id,
                        ),
                    )
                    connection.execute(
                        "DELETE FROM cluster_articles WHERE cluster_id = ? AND assignment_source = 'automatic'",
                        (cluster_id,),
                    )

                for index in member_indexes:
                    similarity = _cosine(vectors[index], vectors[representative_index], norms[index], norms[representative_index])
                    connection.execute(
                        """
                        INSERT INTO cluster_articles(
                            cluster_id, article_id, similarity, is_primary_evidence, assignment_source
                        ) VALUES (?, ?, ?, ?, 'automatic')
                        ON CONFLICT(cluster_id, article_id) DO UPDATE SET
                            similarity = excluded.similarity,
                            is_primary_evidence = excluded.is_primary_evidence
                        """,
                        (
                            cluster_id,
                            articles[index].id,
                            round(similarity, 4),
                            1 if index == representative_index else 0,
                        ),
                    )

                if len(members) < 2:
                    continue
                independent_count = _estimated_independent_sources(members)
                signal_id = _upsert_signal(
                    connection,
                    cluster_id=cluster_id,
                    canonical_name=representative.title,
                    members=members,
                    first_seen=first_seen,
                    last_seen=last_seen,
                    trend_state=trend_state,
                    independent_count=independent_count,
                )
                signal_count += 1
                _upsert_candidate(
                    connection,
                    signal_id=signal_id,
                    canonical_name=representative.title,
                    members=members,
                    global_latest=global_latest,
                    independent_count=independent_count,
                    cycle_label=str(cycle["label"]),
                )
                candidate_count += 1

        return DemoBuildResult(
            database_path=database_path.resolve(),
            source_articles=len(articles),
            clusters=len(groups),
            multi_article_clusters=sum(1 for group in groups if len(group) >= 2),
            signals=signal_count,
            candidates=candidate_count,
            locked_clusters_preserved=locked_preserved,
            stale_clusters_removed=stale_removed,
            cycle_id=cycle_id,
            cycle_slug=str(cycle["slug"]),
        )
    finally:
        connection.close()


def _load_articles(connection: sqlite3.Connection, cycle_id: int) -> list[_Article]:
    rows = connection.execute(
        """
        SELECT s.id, s.external_id, s.account_id, a.name AS account_name,
               s.title, s.published_at, s.url, s.content_snapshot, s.content_hash,
               s.read_count, s.like_count, s.share_count, s.recommend_count
        FROM source_articles s
        JOIN source_accounts a ON a.id = s.account_id
        JOIN monitoring_cycle_articles mca ON mca.article_id = s.id
        WHERE mca.cycle_id = ? AND mca.role = 'in_period'
        ORDER BY s.id
        """,
        (cycle_id,),
    ).fetchall()
    return [
        _Article(
            id=int(row["id"]),
            external_id=str(row["external_id"] or f"article:{row['id']}"),
            account_id=int(row["account_id"]),
            account_name=str(row["account_name"]),
            title=str(row["title"]),
            published_at=row["published_at"],
            url=row["url"],
            content_snapshot=str(row["content_snapshot"]),
            content_hash=row["content_hash"],
            read_count=int(row["read_count"]),
            like_count=int(row["like_count"]),
            share_count=int(row["share_count"]),
            recommend_count=int(row["recommend_count"]),
        )
        for row in rows
    ]


def _cluster_articles(
    articles: list[_Article],
) -> tuple[list[list[int]], list[dict[str, float]], list[float]]:
    feature_sets = [_title_features(article.title) for article in articles]
    document_frequency = Counter(feature for features in feature_sets for feature in features)
    document_count = len(articles)
    max_feature_documents = max(24, min(80, math.ceil(document_count * 0.05)))

    vectors: list[dict[str, float]] = []
    norms: list[float] = []
    for features in feature_sets:
        vector = {
            feature: math.log((document_count + 1) / (document_frequency[feature] + 1)) + 1
            for feature in features
            if document_frequency[feature] <= max_feature_documents
        }
        vectors.append(vector)
        norms.append(math.sqrt(sum(weight * weight for weight in vector.values())))

    parent = list(range(document_count))
    sizes = [1] * document_count

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if sizes[left_root] < sizes[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        sizes[left_root] += sizes[right_root]

    content_groups: dict[str, list[int]] = defaultdict(list)
    for index, article in enumerate(articles):
        if article.content_hash:
            content_groups[article.content_hash].append(index)
    for indexes in content_groups.values():
        for index in indexes[1:]:
            union(indexes[0], index)

    # A title-similarity threshold is useful for near-duplicate headlines, but
    # misses cross-media angles such as “情绪益生菌” versus “益生菌品牌玩法”.
    # Merge only on a small, auditable vocabulary of specific industry topics;
    # broad category words are intentionally excluded to avoid one giant cluster.
    strong_topic_groups: dict[str, list[int]] = defaultdict(list)
    for index, article in enumerate(articles):
        for topic in _strong_industry_topics(article.title):
            strong_topic_groups[topic].append(index)
    for indexes in strong_topic_groups.values():
        for index in indexes[1:]:
            if not _different_recurring_issues(
                articles[indexes[0]].title, articles[index].title
            ):
                union(indexes[0], index)

    postings: dict[str, list[int]] = defaultdict(list)
    for index, vector in enumerate(vectors):
        for feature in vector:
            postings[feature].append(index)
    dot_products: dict[tuple[int, int], float] = defaultdict(float)
    for feature, indexes in postings.items():
        for position, left in enumerate(indexes):
            left_weight = vectors[left][feature]
            for right in indexes[position + 1 :]:
                dot_products[(left, right)] += left_weight * vectors[right][feature]
    for (left, right), dot_product in dot_products.items():
        denominator = norms[left] * norms[right]
        if _different_recurring_issues(articles[left].title, articles[right].title):
            continue
        if denominator and dot_product / denominator >= TITLE_SIMILARITY_THRESHOLD:
            union(left, right)

    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(document_count):
        groups[find(index)].append(index)
    return sorted(groups.values(), key=lambda group: min(group)), vectors, norms


def _title_features(title: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", title).upper()
    features = set(re.findall(r"[A-Z][A-Z0-9+.-]{1,}", normalized))
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        for size in (2, 3, 4):
            features.update(
                segment[index : index + size]
                for index in range(0, len(segment) - size + 1)
            )
    return features


def _normalized_title(title: str) -> str:
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", title)).casefold()


def _strong_industry_topics(title: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    return {
        topic
        for topic in STRONG_INDUSTRY_TOPIC_TERMS
        if topic.casefold() in normalized
    }


def _different_recurring_issues(left: str, right: str) -> bool:
    recurring = re.compile(r"一周新闻|周新闻|周报|月报|日报|早报|晚报|第\s*\d+\s*期", re.I)
    if not recurring.search(left) or not recurring.search(right):
        return False
    left_numbers = re.findall(r"\d+", unicodedata.normalize("NFKC", left))
    right_numbers = re.findall(r"\d+", unicodedata.normalize("NFKC", right))
    return left_numbers != right_numbers


def _choose_representative(indexes: list[int], articles: list[_Article]) -> int:
    title_frequency = Counter(_normalized_title(articles[index].title) for index in indexes)
    return max(
        indexes,
        key=lambda index: (
            not _is_event_promotion(articles[index].title),
            not _is_recurring_digest(articles[index].title),
            _decision_relevance(articles[index].title),
            title_frequency[_normalized_title(articles[index].title)],
            articles[index].engagement,
            -len(articles[index].title),
            -articles[index].id,
        ),
    )


def _cluster_key(members: list[_Article]) -> str:
    material = "\n".join(sorted(article.external_id for article in members))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _remove_stale_generated_clusters(
    connection: sqlite3.Connection,
    cycle_id: int,
    active_cluster_keys: set[str],
) -> int:
    stale_rows = connection.execute(
        """
        SELECT tc.id
        FROM topic_clusters tc
        WHERE tc.cycle_id = ? AND tc.generated_by LIKE ? AND tc.locked = 0
          AND NOT EXISTS (
              SELECT 1 FROM industry_signals s
              JOIN topic_candidates c ON c.signal_id = s.id
              WHERE s.cluster_id = tc.id
                AND (c.status != 'candidate_pool' OR EXISTS (
                    SELECT 1 FROM topic_events e WHERE e.candidate_id = c.id
                ))
          )
        """,
        (cycle_id, DEMO_GENERATOR_PATTERN),
    ).fetchall()
    removed = 0
    for row in stale_rows:
        cluster_id = int(row["id"])
        key_row = connection.execute(
            "SELECT cluster_key FROM topic_clusters WHERE id = ?", (cluster_id,)
        ).fetchone()
        if key_row is not None and key_row["cluster_key"] in active_cluster_keys:
            continue
        candidate_ids = [
            int(candidate["id"])
            for candidate in connection.execute(
                """
                SELECT c.id FROM topic_candidates c
                JOIN industry_signals s ON s.id = c.signal_id
                WHERE s.cluster_id = ?
                """,
                (cluster_id,),
            )
        ]
        for candidate_id in candidate_ids:
            connection.execute("DELETE FROM evidence_records WHERE candidate_id = ?", (candidate_id,))
            connection.execute("DELETE FROM topic_packages WHERE candidate_id = ?", (candidate_id,))
            connection.execute("DELETE FROM topic_events WHERE candidate_id = ?", (candidate_id,))
            connection.execute("DELETE FROM topic_candidates WHERE id = ?", (candidate_id,))
        connection.execute("DELETE FROM industry_signals WHERE cluster_id = ?", (cluster_id,))
        connection.execute("DELETE FROM cluster_articles WHERE cluster_id = ?", (cluster_id,))
        connection.execute("DELETE FROM topic_clusters WHERE id = ?", (cluster_id,))
        removed += 1
    return removed


def _cosine(
    left: dict[str, float], right: dict[str, float], left_norm: float, right_norm: float
) -> float:
    if not left_norm or not right_norm:
        return 1.0 if left == right else 0.0
    if len(left) > len(right):
        left, right = right, left
    dot_product = sum(weight * right.get(feature, 0.0) for feature, weight in left.items())
    return dot_product / (left_norm * right_norm)


def _trend_state(
    first_seen: str | None,
    last_seen: str | None,
    article_count: int,
    global_latest: datetime | None,
) -> str:
    if article_count <= 1 or not first_seen or not last_seen or global_latest is None:
        return "new"
    first = _parse_datetime(first_seen)
    last = _parse_datetime(last_seen)
    age_days = max(0, (global_latest - last).days)
    span_days = max(0, (last - first).days)
    if age_days <= 3 and span_days <= 7:
        return "rising"
    if age_days <= 7 or span_days >= 7:
        return "steady"
    if age_days <= 21:
        return "falling"
    return "dormant"


def _upsert_signal(
    connection: sqlite3.Connection,
    *,
    cluster_id: int,
    canonical_name: str,
    members: list[_Article],
    first_seen: str | None,
    last_seen: str | None,
    trend_state: str,
    independent_count: int,
) -> int:
    account_count = len({article.account_id for article in members})
    actors = _affected_actors(canonical_name)
    consensus = (
        f"规则聚类检测到 {len(members)} 篇文章、{account_count} 个公众号；"
        f"按账号覆盖和正文近似去重，估算为 {independent_count} 个相对独立来源。"
    )
    disagreements = "尚未调用模型识别观点分歧；需要人工阅读来源文章后确认。"
    data_gaps = (
        "需核对底层事件、共同稿源及监管文件、论文、企业原始公告或数据原文；"
        "当前公众号材料仅作为发现线索。"
    )
    business_implications = (
        "先判断该议题是否影响品牌、研发、供应、合规或资本决策，再决定是否进入深度研究。"
    )
    underlying_event = (
        f"多篇文章围绕“{_truncate(_plain_subject(canonical_name), 64)}”形成相似主题。"
        "这是确定性规则的 Demo 聚类结果，底层事件尚待人工确认。"
    )
    existing = connection.execute(
        "SELECT id FROM industry_signals WHERE cluster_id = ?", (cluster_id,)
    ).fetchone()
    if existing is None:
        cursor = connection.execute(
            f"""
            INSERT INTO industry_signals(
                cluster_id, signal_title, underlying_event, first_seen_at,
                last_seen_at, trend_state, article_count,
                independent_source_count, consensus, disagreements,
                evidence_status, affected_actors_json, business_implications,
                data_gaps, generated_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'needs_evidence', ?, ?, ?, ?, ({SQL_NOW}), ({SQL_NOW}))
            """,
            (
                cluster_id,
                canonical_name,
                underlying_event,
                first_seen,
                last_seen,
                trend_state,
                len(members),
                independent_count,
                consensus,
                disagreements,
                json.dumps(actors, ensure_ascii=False),
                business_implications,
                data_gaps,
                DEMO_GENERATOR,
            ),
        )
        return int(cursor.lastrowid)

    signal_id = int(existing["id"])
    connection.execute(
        f"""
        UPDATE industry_signals SET
            signal_title = ?, underlying_event = ?, first_seen_at = ?,
            last_seen_at = ?, trend_state = ?, article_count = ?,
            independent_source_count = ?, consensus = ?, disagreements = ?,
            evidence_status = 'needs_evidence', affected_actors_json = ?,
            business_implications = ?, data_gaps = ?, generated_by = ?,
            updated_at = ({SQL_NOW})
        WHERE id = ?
        """,
        (
            canonical_name,
            underlying_event,
            first_seen,
            last_seen,
            trend_state,
            len(members),
            independent_count,
            consensus,
            disagreements,
            json.dumps(actors, ensure_ascii=False),
            business_implications,
            data_gaps,
            DEMO_GENERATOR,
            signal_id,
        ),
    )
    return signal_id


def _upsert_candidate(
    connection: sqlite3.Connection,
    *,
    signal_id: int,
    canonical_name: str,
    members: list[_Article],
    global_latest: datetime | None,
    independent_count: int,
    cycle_label: str,
) -> int:
    account_count = len({article.account_id for article in members})
    latest = max(
        (_parse_datetime(article.published_at) for article in members if article.published_at),
        default=None,
    )
    raw_total_reads = sum(article.read_count for article in members)
    adjusted_reads = _adjusted_attention_reads(members, independent_count)
    score, breakdown = _candidate_score(
        title=canonical_name,
        account_count=account_count,
        independent_count=independent_count,
        adjusted_reads=adjusted_reads,
        latest=latest,
        global_latest=global_latest,
    )
    review_track, track_reason = _review_track(
        canonical_name,
        account_count=account_count,
        independent_count=independent_count,
    )
    audience = _audience(canonical_name)
    proposed_title = _candidate_title(canonical_name, account_count, independent_count)
    why_now = (
        f"“{cycle_label}”样本中有 {len(members)} 篇文章、{account_count} 个公众号集中讨论；"
        f"经正文近似去重后估算为 {independent_count} 个相对独立来源。该热度仅代表 Demo 样本。"
    )
    angle = (
        "区分传播热度与独立证据，先还原底层事件，再判断其对目标企业的决策影响。"
    )
    risks = ["当前为试运行样本，公开索引覆盖与完整性待人工确认", "事实性主张待补一手来源"]
    if account_count > independent_count:
        risks.append("存在跨账号同稿传播，账号数不等于独立证据数")
    if _is_event_promotion(canonical_name):
        risks.append("会议或活动推广内容，行业判断价值需人工确认")
    if _is_recurring_digest(canonical_name):
        risks.append("资讯汇总不是单一研究主题，需要人工拆出具体事件")
    read_explanation = f"去重折算阅读 {adjusted_reads}"
    if adjusted_reads != raw_total_reads:
        read_explanation += f"（原始累计 {raw_total_reads}）"
    score_reason = (
        f"规则评分：账号覆盖 {account_count}、独立来源估算 {independent_count}、"
        f"{read_explanation}；只在“{'值得研究' if review_track == 'research' else '传播观察'}”"
        "队列内排序，不自动立项。"
    )
    core_question = (
        "这些文章共同指向的底层事件是什么，哪些信息是独立证据，"
        "它可能怎样影响品牌方、原料商或企业决策者？"
    )
    existing = connection.execute(
        """
        SELECT id, track_source, editor_modified
        FROM topic_candidates
        WHERE signal_id = ? AND generated_by LIKE ?
        ORDER BY id LIMIT 1
        """,
        (signal_id, DEMO_GENERATOR_PATTERN),
    ).fetchone()
    score_breakdown_json = json.dumps(breakdown, ensure_ascii=False, sort_keys=True)
    risk_flags_json = json.dumps(risks, ensure_ascii=False)
    if existing is None:
        cursor = connection.execute(
            f"""
            INSERT INTO topic_candidates(
                signal_id, proposed_title, audience, core_question,
                why_now, angle, score, score_breakdown_json, score_reason,
                risk_flags_json, status, generated_by, review_track,
                track_source, track_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate_pool', ?, ?, 'rule', ?, ({SQL_NOW}), ({SQL_NOW}))
            """,
            (
                signal_id,
                proposed_title,
                audience,
                core_question,
                why_now,
                angle,
                score,
                score_breakdown_json,
                score_reason,
                risk_flags_json,
                DEMO_GENERATOR,
                review_track,
                track_reason,
            ),
        )
        return int(cursor.lastrowid)

    candidate_id = int(existing["id"])
    connection.execute(
        f"""
        UPDATE topic_candidates SET
            score = ?, score_breakdown_json = ?, score_reason = ?,
            risk_flags_json = ?, generated_by = ?, updated_at = ({SQL_NOW})
        WHERE id = ?
        """,
        (
            score,
            score_breakdown_json,
            score_reason,
            risk_flags_json,
            DEMO_GENERATOR,
            candidate_id,
        ),
    )
    if not int(existing["editor_modified"]):
        connection.execute(
            f"""
            UPDATE topic_candidates SET
                proposed_title = ?, audience = ?, core_question = ?,
                why_now = ?, angle = ?, updated_at = ({SQL_NOW})
            WHERE id = ?
            """,
            (proposed_title, audience, core_question, why_now, angle, candidate_id),
        )
    if existing["track_source"] != "manual":
        connection.execute(
            f"""
            UPDATE topic_candidates SET
                review_track = ?, track_source = 'rule', track_reason = ?,
                updated_at = ({SQL_NOW})
            WHERE id = ?
            """,
            (review_track, track_reason, candidate_id),
        )
    return candidate_id


def _candidate_score(
    *,
    title: str,
    account_count: int,
    independent_count: int,
    adjusted_reads: int,
    latest: datetime | None,
    global_latest: datetime | None,
) -> tuple[float, dict[str, float | int | str]]:
    # A second account can still be useful corroboration, but large same-copy
    # syndication must not receive the same breadth credit as independent sources.
    effective_source_count = min(account_count, independent_count + 1)
    source_breadth = min(20.0, effective_source_count * 5.0)
    evidence_independence = min(25.0, independent_count * 7.0)
    # Interaction is deliberately a weak signal. Reads are already conservatively
    # reduced to at most one high-performing article per estimated independent source.
    engagement = min(10.0, math.log10(max(1, adjusted_reads) + 1) * 2.5)
    age_days = 30
    if latest is not None and global_latest is not None:
        age_days = max(0, (global_latest - latest).days)
    recency = max(0.0, 20.0 - age_days * 0.8)
    decision_relevance = _decision_relevance(title)
    total = round(
        source_breadth + evidence_independence + engagement + recency + decision_relevance,
        1,
    )
    return total, {
        "source_breadth": round(source_breadth, 1),
        "evidence_independence": round(evidence_independence, 1),
        "engagement": round(engagement, 1),
        "recency": round(recency, 1),
        "decision_relevance": round(decision_relevance, 1),
        "effective_source_count": effective_source_count,
        "adjusted_reads": adjusted_reads,
        "method": DEMO_GENERATOR,
    }


def _adjusted_attention_reads(members: list[_Article], independent_count: int) -> int:
    """Keep attention weak by counting at most one strong article per source estimate."""
    if not members:
        return 0
    limit = max(1, independent_count)
    return sum(sorted((article.read_count for article in members), reverse=True)[:limit])


def _estimated_independent_sources(members: list[_Article]) -> int:
    """Estimate source independence by combining account count and near-duplicate bodies."""
    if not members:
        return 0
    account_count = len({article.account_id for article in members})
    parent = list(range(len(members)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    normalized = [_normalized_content(article.content_snapshot) for article in members]
    for left in range(len(members)):
        for right in range(left + 1, len(members)):
            if members[left].content_hash and members[left].content_hash == members[right].content_hash:
                union(left, right)
                continue
            left_text = normalized[left]
            right_text = normalized[right]
            if not left_text or not right_text:
                continue
            length_ratio = min(len(left_text), len(right_text)) / max(len(left_text), len(right_text))
            if length_ratio < 0.65:
                continue
            matcher = SequenceMatcher(None, left_text, right_text, autojunk=False)
            if matcher.quick_ratio() >= 0.82 and matcher.ratio() >= 0.82:
                union(left, right)

    text_groups = len({find(index) for index in range(len(members))})
    return max(1, min(account_count, text_groups))


def _normalized_content(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def _decision_relevance(title: str) -> float:
    category = _decision_category(title)
    if category == "event":
        return 4.0
    if category == "digest":
        return 6.0
    if category == "regulatory":
        return 25.0
    if category == "market":
        return 22.0
    if category == "science_product":
        return 18.0
    return 10.0


def _decision_category(title: str) -> str:
    if _is_event_promotion(title):
        return "event"
    if _is_recurring_digest(title):
        return "digest"
    if re.search(
        r"监管|法规|新规|蓝帽子|药监|CDE|FDA|审批|审评|合规|罚|召回|不合格|"
        r"注册检验|说明书|药典|中检院|对照品|GMP|飞检|质量标准",
        title,
        re.I,
    ):
        return "regulatory"
    if re.search(
        r"融资|IPO|港股|并购|收购|授权|BD|出海|市场|销售|营收|"
        r"裁员|关厂|关闭.{0,4}基地|扩产|产能|战略合作|签约|换帅|任命|"
        r"人事变动|破产|停产|裁撤|创业|高管|专利|投资|募资|估值",
        title,
        re.I,
    ):
        return "market"
    if re.search(
        r"临床|获批|研发|科研|证据|药物|疗法|原料|产品|技术|"
        r"ADC|抗体|疫苗|靶点|管线|创新药|生物医药",
        title,
        re.I,
    ) or _strong_industry_topics(title):
        return "science_product"
    return "general"


def _review_track(
    title: str,
    *,
    account_count: int,
    independent_count: int,
) -> tuple[str, str]:
    category = _decision_category(title)
    if category == "event":
        return "watch", "识别为会议、年会、招募或直播等活动内容，先观察传播和议程热度。"
    if category == "digest":
        return "watch", "识别为周报、月报或资讯汇总，适合作为发现线索，不直接作为深度研究主题。"
    if category in {"regulatory", "market", "science_product"}:
        label = {
            "regulatory": "监管与合规",
            "market": "市场与企业动作",
            "science_product": "科研、原料与产品",
        }[category]
        return "research", f"标题包含{label}决策信号，建议人工判断是否进入深度研究。"
    if account_count > independent_count:
        return "watch", "多号传播但独立来源估算较低，先作为共同稿源传播现象观察。"
    return "watch", "规则尚未识别明确的行业决策信号，保留在线索观察队列等待人工改类。"


def _audience(title: str) -> str:
    if re.search(
        r"监管|法规|新规|蓝帽子|药监|CDE|FDA|审批|审评|合规|质量|召回",
        title,
        re.I,
    ):
        return "企业决策者与合规负责人"
    if re.search(r"原料|供应|生产|工艺|制剂|CDMO|CRO", title, re.I):
        return "原料商、供应链与企业决策者"
    if re.search(r"品牌|产品|消费|市场|销售|渠道", title, re.I):
        return "品牌方与企业决策者"
    return "品牌方、原料商及企业决策者"


def _affected_actors(title: str) -> list[str]:
    audience = _audience(title)
    actors = [part.strip() for part in re.split(r"[、及与]", audience) if part.strip()]
    return list(dict.fromkeys(actors))


def _candidate_title(title: str, account_count: int, independent_count: int) -> str:
    subject = _truncate(_plain_subject(title), 42)
    if _is_event_promotion(title):
        return f"从“{subject}”看行业会议热度：哪些议题值得继续跟踪？"
    if _is_recurring_digest(title):
        return f"拆解“{subject}”：哪些具体变化值得单独立项？"
    if account_count > 1 and independent_count <= 1:
        return f"多号同步传播“{subject}”：事实增量在哪里，决策者该如何判断？"
    return f"“{subject}”背后的行业信号：证据、影响与下一步判断"


def _plain_subject(title: str) -> str:
    return re.sub(r"[“”\"「」]", "", title).strip()


def _is_event_promotion(title: str) -> bool:
    return bool(
        re.search(
            r"报名|大会|年会|论坛|峰会|会议|培训|直播|倒计时|邀您|邀请|开幕|"
            r"招募|路演|课程|解读课|公开课|线上课|开课|开讲|讲堂|研修|"
            r"训练营|实训营|研习班|培训班|研修班|工作坊|实战班|沙龙|展会|"
            r"嘉宾|早鸟|抢票|新书发布|有奖调查|文末下载|推荐关注|不可错过的行业公众号|"
            r"秘籍|全掌握|(?:北京|上海|广州|深圳|杭州|成都|苏州|南京|武汉|西安)站\s*[|｜]",
            title,
            re.I,
        )
    )


def _is_recurring_digest(title: str) -> bool:
    return bool(
        re.search(
            r"一周新闻|周新闻|一周头条|一周动态|周报|月报|日报|早报|晚报|"
            r"每周盘点|政策盘点|行业盘点|资讯汇总|新闻汇总|本周要闻",
            title,
            re.I,
        )
    )


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _truncate(value: str, length: int) -> str:
    return value if len(value) <= length else value[: length - 1] + "…"
