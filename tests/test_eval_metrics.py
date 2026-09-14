"""Tests for retrieval metrics, page-level and chunk-level (no infra required)."""

import math
import uuid

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


# --- M7 L1: chunk-level, multi-span relevance ---

A, B, C = (uuid.uuid4() for _ in range(3))


def test_span_recall_is_macro_over_spans():
    # 두 스팬 중 하나만 찾으면 0.5 — 청크 개수가 아니라 스팬 개수로 나눈다.
    assert metrics.span_recall_at_k([A], [{A}, {B}], k=5) == 0.5
    assert metrics.span_recall_at_k([A, B], [{A}, {B}], k=5) == 1.0
    assert metrics.span_recall_at_k([A, B], [{A}, {B}], k=1) == 0.5


def test_span_recall_counts_a_span_once_however_many_chunks_cover_it():
    # 한 스팬이 세 청크에 걸쳐도 그 스팬은 1개다. 청킹이 바뀌어도 값이 같아야 한다.
    assert metrics.span_recall_at_k([A, B, C], [{A, B, C}], k=5) == 1.0


def test_first_gold_rank_and_mrr():
    assert metrics.first_gold_rank([A, B], [{B}]) == 2
    assert metrics.first_gold_rank([A, B], [{C}]) is None
    assert metrics.chunk_reciprocal_rank([A, B], [{B}]) == 0.5
    assert metrics.chunk_reciprocal_rank([A, B], [{C}]) == 0.0


def test_chunk_ndcg_discounts_by_rank_and_ignores_repeat_coverage():
    assert metrics.chunk_ndcg_at_k([A], [{A}], k=10) == 1.0
    assert abs(metrics.chunk_ndcg_at_k([C, A], [{A}], k=10) - 1 / math.log2(3)) < 1e-12
    # 두 청크가 같은 스팬을 덮어도 이득은 한 번뿐 — IDCG 는 스팬 1개 기준.
    assert metrics.chunk_ndcg_at_k([A, B], [{A, B}], k=10) == 1.0
    # 스팬이 둘이고 1·2위에서 하나씩 덮으면 완벽한 순서다.
    assert abs(metrics.chunk_ndcg_at_k([A, B], [{A}, {B}], k=10) - 1.0) < 1e-12


def test_chunk_metrics_are_zero_without_gold():
    assert metrics.span_recall_at_k([A], [], k=5) == 0.0
    assert metrics.chunk_reciprocal_rank([A], []) == 0.0
    assert metrics.chunk_ndcg_at_k([A], [], k=5) == 0.0
    assert metrics.first_gold_rank([A], []) is None


def test_page_recall_separates_documents():
    # 문서가 다르면 같은 쪽 번호라도 다른 쪽이다.
    ranked = [("lease", 2), ("saas", 3)]
    assert metrics.page_recall_at_k(ranked, [("lease", 2)], k=5) == 1.0
    assert metrics.page_recall_at_k(ranked, [("insurance", 2)], k=5) == 0.0
    assert metrics.page_recall_at_k(ranked, [("lease", 2), ("saas", 3)], k=1) == 0.5


def test_score_chunks_keys_follow_cutoffs():
    scored = metrics.score_chunks(
        [B, A], [{A}], ranked_pages=[("d", 2), ("d", 1)],
        gold_pages=[("d", 1)], ks=(1, 3, 5), ndcg_k=5,
    )
    assert {"R@1", "R@3", "R@5", "MRR", "nDCG@5"} <= set(scored)
    assert {"pageR@1", "pageR@3", "pageR@5"} <= set(scored)
    assert scored["R@1"] == 0.0
    assert scored["R@3"] == 1.0
    assert scored["MRR"] == 0.5
    assert scored["pageR@1"] == 0.0
