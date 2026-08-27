from nutrition_signal_radar.minhash import (
    candidate_pairs,
    char_shingles,
    estimated_jaccard,
    jaccard,
    lsh_bands,
    minhash_signature,
    normalize_text,
)

_BASE = "市场监管部门发布保健食品虚假宣传典型案例通报涉及广告用语合规问题"
_EDITED = _BASE + "并提醒企业规范功效"
_OTHER = "研发团队完成新一代益生菌菌株临床试验研究数据显示改善肠道健康"


def test_normalize_text_strips_punctuation_and_case() -> None:
    assert normalize_text("Hello, 世界! 2026年。") == "hello世界2026年"


def test_char_shingles_sizes() -> None:
    assert char_shingles("") == set()
    assert char_shingles("ab") == {"ab"}
    assert char_shingles("abcd", size=3) == {"abc", "bcd"}


def test_jaccard_edge_cases() -> None:
    assert jaccard(set(), set()) == 1.0
    assert jaccard(set(), {"a"}) == 0.0
    assert jaccard({"a"}, {"b"}) == 0.0
    assert jaccard({"a", "b", "c"}, {"b", "c", "d"}) == 2 / 4


def test_minhash_signature_is_deterministic() -> None:
    shingles = char_shingles(normalize_text(_BASE))
    first = minhash_signature(shingles, num_hashes=64, seed=7)
    second = minhash_signature(shingles, num_hashes=64, seed=7)
    assert first == second
    assert len(first) == 64
    assert minhash_signature(shingles, num_hashes=64, seed=8) != first


def test_estimated_jaccard_tracks_exact_jaccard() -> None:
    base_shingles = char_shingles(normalize_text(_BASE))
    edited_shingles = char_shingles(normalize_text(_EDITED))
    exact = jaccard(base_shingles, edited_shingles)
    assert 0.7 <= exact <= 0.8
    estimate = estimated_jaccard(
        minhash_signature(base_shingles, seed=1),
        minhash_signature(edited_shingles, seed=1),
    )
    assert abs(estimate - exact) < 0.15
    unrelated = estimated_jaccard(
        minhash_signature(base_shingles, seed=1),
        minhash_signature(char_shingles(normalize_text(_OTHER)), seed=1),
    )
    assert unrelated < 0.2


def test_candidate_pairs_include_near_duplicates() -> None:
    signatures = [
        minhash_signature(char_shingles(normalize_text(_BASE))),
        minhash_signature(char_shingles(normalize_text(_EDITED))),
        minhash_signature(char_shingles(normalize_text(_OTHER))),
    ]
    candidates = candidate_pairs(signatures, band_size=4)
    assert (0, 1) in candidates


def test_lsh_bands_partition_signature() -> None:
    signature = list(range(16))
    assert lsh_bands(signature, band_size=4) == [
        (0, 1, 2, 3),
        (4, 5, 6, 7),
        (8, 9, 10, 11),
        (12, 13, 14, 15),
    ]
