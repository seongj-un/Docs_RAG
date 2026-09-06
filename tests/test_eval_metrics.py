"""Tests for retrieval metrics (no infra required)."""

import math

from eval import metrics


def test_recall_respects_cutoff():
    assert metrics.recall_at_k([3, 1, 2], gold_page=1, k=1) == 0.0
    assert metrics.recall_at_k([3, 1, 2], gold_page=1, k=2) == 1.0
    assert metrics.recall_at_k([3, 1, 2], gold_page=9, k=3) == 0.0


def test_reciprocal_rank_uses_first_hit():
    assert metrics.reciprocal_rank([1, 2, 3], gold_page=1) == 1.0
    assert metrics.reciprocal_rank([3, 1, 2], gold_page=1) == 0.5
    assert metrics.reciprocal_rank([3, 2], gold_page=9) == 0.0


def test_ndcg_discounts_by_rank():
    assert metrics.ndcg_at_k([1, 2], gold_page=1, k=5) == 1.0
    assert abs(metrics.ndcg_at_k([3, 1], gold_page=1, k=5) - 1 / math.log2(3)) < 1e-12
    # a hit beyond the cutoff scores zero
    assert metrics.ndcg_at_k([3, 4, 1], gold_page=1, k=2) == 0.0


def test_empty_ranking_scores_zero():
    assert metrics.recall_at_k([], gold_page=1, k=5) == 0.0
    assert metrics.reciprocal_rank([], gold_page=1) == 0.0
    assert metrics.ndcg_at_k([], gold_page=1, k=5) == 0.0


def test_score_all_keys_follow_cutoff():
    scored = metrics.score_all([2, 1], gold_page=1, k=5)
    assert set(scored) == {"R@1", "R@3", "R@5", "MRR", "nDCG@5"}
    assert scored["R@1"] == 0.0
    assert scored["R@3"] == 1.0
    assert scored["MRR"] == 0.5
