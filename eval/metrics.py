"""Retrieval metrics with binary, page-level relevance.

A retrieved chunk counts as relevant when its page equals the query's gold
page. ``ranked_pages`` is the pages of the retrieved chunks in rank order
(index 0 = rank 1). Each query has exactly one gold page, so IDCG = 1 and
nDCG reduces to the discount of the first hit.
"""

import math


def recall_at_k(ranked_pages: list[int], gold_page: int, k: int) -> float:
    """1.0 if the gold page appears in the top-k, else 0.0."""
    return 1.0 if gold_page in ranked_pages[:k] else 0.0


def reciprocal_rank(ranked_pages: list[int], gold_page: int) -> float:
    """1/rank of the first gold hit, or 0.0 if the gold page is absent."""
    for i, page in enumerate(ranked_pages, start=1):
        if page == gold_page:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked_pages: list[int], gold_page: int, k: int) -> float:
    """nDCG@k for a single relevant item (IDCG = 1)."""
    for i, page in enumerate(ranked_pages[:k], start=1):
        if page == gold_page:
            return 1.0 / math.log2(i + 1)
    return 0.0


def score_all(ranked_pages: list[int], gold_page: int, k: int) -> dict[str, float]:
    """All metrics for one query, keyed for aggregation."""
    return {
        "R@1": recall_at_k(ranked_pages, gold_page, 1),
        "R@3": recall_at_k(ranked_pages, gold_page, 3),
        f"R@{k}": recall_at_k(ranked_pages, gold_page, k),
        "MRR": reciprocal_rank(ranked_pages, gold_page),
        f"nDCG@{k}": ndcg_at_k(ranked_pages, gold_page, k),
    }
