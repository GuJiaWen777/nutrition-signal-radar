"""Compare near-duplicate detection methods inside rule-built topic clusters.

Reference implementation for the independent-source estimator in
``demo_pipeline._estimated_independent_sources``.  For every multi-article
cluster of one monitoring cycle it compares:

  A. the current rule: content hash equality, then ``SequenceMatcher`` ratio
     >= 0.82 (quick + full) with a 0.65 length guard, on normalized bodies;
  B. exact Jaccard similarity on 4-gram character shingles at several
     thresholds;
  C. MinHash(128) signatures with LSH banding as a candidate generator for B.

Ground-truth anchors are byte-identical snapshots (equal ``content_hash``) and
identical normalized titles.

Usage:  python scripts/compare_near_dup.py [--db var/radar.sqlite3] [--cycle SLUG]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nutrition_signal_radar.minhash import (  # noqa: E402
    char_shingles,
    candidate_pairs,
    estimated_jaccard,
    jaccard,
    minhash_signature,
    normalize_text,
)

SM_THRESHOLD = 0.82
LENGTH_RATIO_GUARD = 0.65
JACCARD_THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9)
BAND_SIZES = (4, 8, 16)


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def _pick_cycle(connection: sqlite3.Connection, cycle_slug: str | None) -> sqlite3.Row:
    if cycle_slug:
        row = connection.execute(
            "SELECT * FROM monitoring_cycles WHERE slug = ?", (cycle_slug,)
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM monitoring_cycles WHERE status = 'open' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise SystemExit("no open monitoring cycle found")
    return row


def load_clusters(connection: sqlite3.Connection, cycle_id: int) -> dict[int, list[sqlite3.Row]]:
    clusters: dict[int, list[sqlite3.Row]] = defaultdict(list)
    rows = connection.execute(
        """
        SELECT tc.id AS cluster_id, a.id AS article_id, a.account_id,
               sa.name AS account_name, a.title, a.content_snapshot,
               a.content_hash, a.published_at
        FROM cluster_articles ca
        JOIN topic_clusters tc ON tc.id = ca.cluster_id
        JOIN source_articles a ON a.id = ca.article_id
        LEFT JOIN source_accounts sa ON sa.id = a.account_id
        WHERE tc.cycle_id = ?
        ORDER BY tc.id, a.id
        """,
        (cycle_id,),
    ).fetchall()
    for row in rows:
        clusters[int(row["cluster_id"])].append(row)
    return clusters


def _pairs(count: int) -> list[tuple[int, int]]:
    return [(left, right) for left in range(count) for right in range(left + 1, count)]


def current_rule_duplicate(left: sqlite3.Row, right: sqlite3.Row) -> bool:
    if left["content_hash"] and left["content_hash"] == right["content_hash"]:
        return True
    left_text = normalize_text(left["content_snapshot"] or "")
    right_text = normalize_text(right["content_snapshot"] or "")
    if not left_text or not right_text:
        return False
    length_ratio = min(len(left_text), len(right_text)) / max(len(left_text), len(right_text))
    if length_ratio < LENGTH_RATIO_GUARD:
        return False
    matcher = SequenceMatcher(None, left_text, right_text, autojunk=False)
    return matcher.quick_ratio() >= SM_THRESHOLD and matcher.ratio() >= SM_THRESHOLD


def _union_find(member_count: int, merges: list[tuple[int, int]]) -> list[int]:
    parent = list(range(member_count))
    for left, right in merges:
        roots = [left, right]
        while parent[roots[0]] != roots[0]:
            roots[0] = parent[roots[0]]
        while parent[roots[1]] != roots[1]:
            roots[1] = parent[roots[1]]
        if roots[0] != roots[1]:
            parent[roots[1]] = roots[0]
    for index in range(member_count):
        while parent[index] != parent[parent[index]]:
            parent[index] = parent[parent[index]]
    return parent


def text_group_count(member_count: int, merges: list[tuple[int, int]]) -> int:
    return len(set(_union_find(member_count, merges)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="var/radar.sqlite3")
    parser.add_argument("--cycle", default=None)
    args = parser.parse_args()

    connection = _connect(Path(args.db))
    try:
        cycle = _pick_cycle(connection, args.cycle)
        clusters = load_clusters(connection, int(cycle["id"]))
    finally:
        connection.close()

    multi = {key: members for key, members in clusters.items() if len(members) >= 2}
    total_pairs = sum(len(_pairs(len(m))) for m in multi.values())
    hash_duplicate_pairs = 0
    identical_title_pairs = 0
    for members in multi.values():
        for left, right in _pairs(len(members)):
            if members[left]["content_hash"] and members[left]["content_hash"] == members[right]["content_hash"]:
                hash_duplicate_pairs += 1
            if normalize_text(members[left]["title"]) == normalize_text(members[right]["title"]):
                identical_title_pairs += 1

    print(f"cycle: {cycle['slug']} ({cycle['label']})")
    print(
        f"clusters: {len(clusters)} total, {len(multi)} with >=2 articles, "
        f"{total_pairs} comparable pairs, {hash_duplicate_pairs} hash-equal pairs, "
        f"{identical_title_pairs} identical-title pairs"
    )

    # ---- per-pair measurements (methods A + B), timed ----
    started = time.perf_counter()
    exact: dict[tuple[int, int], float] = {}
    sequence_flags: set[tuple[int, int]] = set()
    shingles_by_cluster: dict[int, list[set[str]]] = {}
    for cluster_id, members in multi.items():
        shingles = [char_shingles(normalize_text(m["content_snapshot"] or "")) for m in members]
        shingles_by_cluster[cluster_id] = shingles
        for left, right in _pairs(len(members)):
            pair = (cluster_id, left, right)
            exact[pair] = jaccard(shingles[left], shingles[right])
            if current_rule_duplicate(members[left], members[right]):
                sequence_flags.add(pair)
    elapsed_exact = time.perf_counter() - started

    # ---- method C: MinHash signatures + LSH candidate recall ----
    signatures: dict[int, list[list[int]]] = {}
    for cluster_id, shingles_list in shingles_by_cluster.items():
        signatures[cluster_id] = [minhash_signature(s, num_hashes=128) for s in shingles_list]
    estimate_errors: list[float] = []
    for cluster_id, members in multi.items():
        cluster_signatures = signatures[cluster_id]
        for left, right in _pairs(len(members)):
            error = abs(
                estimated_jaccard(cluster_signatures[left], cluster_signatures[right])
                - exact[(cluster_id, left, right)]
            )
            estimate_errors.append(error)

    print("\n=== MinHash(128) estimation error vs exact Jaccard ===")
    print(
        f"pairs: {len(estimate_errors)} | mean abs error: {sum(estimate_errors)/len(estimate_errors):.4f} "
        f"| max: {max(estimate_errors):.4f}"
    )

    print("\n=== LSH candidate recall (pairs with exact Jaccard >= t) ===")
    for band_size in BAND_SIZES:
        line = f"band_size={band_size:>2}:"
        for threshold in JACCARD_THRESHOLDS:
            positive = [pair for pair, value in exact.items() if value >= threshold]
            candidates: set[tuple[int, int]] = set()
            for cluster_id in multi:
                for pair in candidate_pairs(signatures[cluster_id], band_size=band_size):
                    candidates.add((cluster_id, pair[0], pair[1]))
            recall = (
                sum(1 for pair in positive if pair in candidates) / len(positive)
                if positive
                else float("nan")
            )
            line += f"  t={threshold} recall={recall:.2f}({len(positive)})"
        print(line)

    # ---- contingency: current rule vs exact Jaccard thresholds ----
    print("\n=== current rule (SM>=0.82) vs exact Jaccard >= t ===")
    for threshold in JACCARD_THRESHOLDS:
        jaccard_flags = {pair for pair, value in exact.items() if value >= threshold}
        both = sequence_flags & jaccard_flags
        sequence_only = sequence_flags - jaccard_flags
        jaccard_only = jaccard_flags - sequence_flags
        print(
            f"t={threshold}: both={len(both)}  SM-only={len(sequence_only)}  "
            f"Jaccard-only={len(jaccard_only)}"
        )

    # ---- disagreement examples for the candidate production threshold ----
    threshold = 0.7
    jaccard_flags = {pair for pair, value in exact.items() if value >= threshold}
    jaccard_only = sorted(jaccard_flags - sequence_flags, key=lambda p: -exact[p])[:12]
    sequence_only = sorted(
        sequence_flags - jaccard_flags, key=lambda p: exact[p]
    )[:12]
    print(f"\n=== Jaccard-only pairs at t={threshold} (SM missed), top by Jaccard ===")
    for cluster_id, left, right in jaccard_only:
        members = multi[cluster_id]
        print(
            f"  cluster {cluster_id} | J={exact[(cluster_id, left, right)]:.3f} | "
            f"{members[left]['title'][:30]!r} <-> {members[right]['title'][:30]!r}"
        )
    print(f"\n=== SM-only pairs at t={threshold} (Jaccard below), lowest Jaccard first ===")
    for cluster_id, left, right in sequence_only:
        members = multi[cluster_id]
        print(
            f"  cluster {cluster_id} | J={exact[(cluster_id, left, right)]:.3f} | "
            f"{members[left]['title'][:30]!r} <-> {members[right]['title'][:30]!r}"
        )

    # ---- independent-source estimate impact ----
    print("\n=== per-cluster independent-source estimate ===")
    header = f"{'cluster':>8} {'n':>3} {'acct':>4} {'current':>8}"
    for threshold in (0.6, 0.7, 0.8):
        header += f" {'J>=' + str(threshold):>8}"
    print(header)
    for cluster_id in sorted(multi):
        members = multi[cluster_id]
        account_count = len({m["account_id"] for m in members})
        cluster_pairs = _pairs(len(members))
        current_merges = [
            (left, right)
            for left, right in cluster_pairs
            if (cluster_id, left, right) in sequence_flags
        ]
        current_estimate = max(1, min(account_count, text_group_count(len(members), current_merges)))
        line = (
            f"{cluster_id:>8} {len(members):>3} {account_count:>4} {current_estimate:>8}"
        )
        for threshold in (0.6, 0.7, 0.8):
            merges = [
                (left, right)
                for left, right in cluster_pairs
                if exact[(cluster_id, left, right)] >= threshold
            ]
            estimate = max(1, min(account_count, text_group_count(len(members), merges)))
            line += f" {estimate:>8}"
        print(line)

    print(f"\nmeasurements: exact Jaccard + SM over all pairs took {elapsed_exact:.3f}s")


if __name__ == "__main__":
    main()
