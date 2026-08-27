"""Deterministic MinHash / locality-sensitive-hashing helpers for near-duplicate text.

The rule pipeline estimates how many of the articles inside a cluster are
independent sources.  Exact content hashes catch byte-identical reprints;
MinHash over character shingles catches reprints with local edits without the
pairwise sequence alignment used by ``difflib.SequenceMatcher``.  Every hash in
this module is seeded, so results are reproducible across runs and machines.
"""

from __future__ import annotations

import re
import unicodedata
import zlib

DEFAULT_NUM_HASHES = 128
DEFAULT_SHINGLE_SIZE = 4
DEFAULT_BAND_SIZE = 16

_MASK64 = (1 << 64) - 1
_U64_MAX = _MASK64


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def char_shingles(text: str, size: int = DEFAULT_SHINGLE_SIZE) -> set[str]:
    if len(text) < size:
        return {text} if text else set()
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 1.0 if left == right else 0.0
    return len(left & right) / len(left | right)


def _mix64(value: int) -> int:
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK64
    return value ^ (value >> 31)


def minhash_signature(
    shingles: set[str],
    *,
    num_hashes: int = DEFAULT_NUM_HASHES,
    seed: int = 0,
) -> list[int]:
    """Estimate the shingle set with ``num_hashes`` seeded 64-bit minima."""
    signature = [_U64_MAX] * num_hashes
    for shingle in shingles:
        base = zlib.crc32(shingle.encode("utf-8"))
        for index in range(num_hashes):
            value = _mix64(
                (base << 32) | ((index * 0x9E3779B97F4A7C15 + seed) & 0xFFFFFFFF)
            )
            if value < signature[index]:
                signature[index] = value
    return signature


def estimated_jaccard(
    left_signature: list[int], right_signature: list[int]
) -> float:
    matches = sum(a == b for a, b in zip(left_signature, right_signature))
    return matches / len(left_signature)


def lsh_bands(signature: list[int], *, band_size: int = DEFAULT_BAND_SIZE) -> list[tuple[int, ...]]:
    return [
        tuple(signature[index : index + band_size])
        for index in range(0, len(signature) - band_size + 1, band_size)
    ]


def candidate_pairs(
    signatures: list[list[int]],
    *,
    band_size: int = DEFAULT_BAND_SIZE,
) -> set[tuple[int, int]]:
    """Pairs hashed into at least one identical band, as ``(left, right)`` with ``left < right``."""
    buckets: dict[tuple[int, ...], list[int]] = {}
    for index, signature in enumerate(signatures):
        for band in lsh_bands(signature, band_size=band_size):
            buckets.setdefault(band, []).append(index)
    candidates: set[tuple[int, int]] = set()
    for indexes in buckets.values():
        for position, left in enumerate(indexes):
            for right in indexes[position + 1 :]:
                candidates.add((left, right) if left < right else (right, left))
    return candidates
