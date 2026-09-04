"""Reciprocal Rank Fusion (RRF).

Combines several ranked candidate lists (dense, sparse, ...) into one ranking
without needing comparable scores across retrievers — only ranks matter, which
makes it robust to cosine-vs-inner-product scale differences.

RRF score for a document d: sum over lists of 1 / (k + rank_d), rank starting
at 1. A document missing from a list simply contributes nothing from it.
"""

from typing import TypeVar

K = TypeVar("K")


def reciprocal_rank_fusion(rankings: list[list[K]], k: int) -> list[tuple[K, float]]:
    """Fuse ranked lists into ``(key, score)`` pairs, best first.

    Args:
        rankings: each inner list is keys in rank order (index 0 = rank 1).
        k: RRF constant (larger flattens the contribution of top ranks).
    """
    scores: dict[K, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
