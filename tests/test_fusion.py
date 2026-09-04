"""Pure-logic tests for Reciprocal Rank Fusion (no infra required)."""

from app.services.fusion import reciprocal_rank_fusion


def test_empty_input():
    assert reciprocal_rank_fusion([], k=60) == []
    assert reciprocal_rank_fusion([[], []], k=60) == []


def test_single_list_preserves_order():
    fused = reciprocal_rank_fusion([["a", "b", "c"]], k=60)
    assert [key for key, _ in fused] == ["a", "b", "c"]


def test_rank1_in_one_list_beats_consistent_middle():
    # 'b' is #2 in both lists; 'a'/'c' are #1 in one list, #3 in the other.
    # By convexity of 1/(k+r), positions (1,3) outscore (2,2): a==c > b.
    dense = ["a", "b", "c"]
    sparse = ["c", "b", "a"]
    scores = dict(reciprocal_rank_fusion([dense, sparse], k=60))
    assert scores["a"] == scores["c"]
    assert scores["a"] > scores["b"]


def test_top_rank_in_both_lists_wins_overall():
    # 'b' is #1 in both lists -> must be the overall winner.
    dense = ["b", "a", "c"]
    sparse = ["b", "c", "a"]
    fused = reciprocal_rank_fusion([dense, sparse], k=60)
    assert fused[0][0] == "b"


def test_missing_from_a_list_contributes_nothing():
    fused = reciprocal_rank_fusion([["x", "y"], ["x"]], k=60)
    scores = dict(fused)
    # x appears in both -> higher than y which appears once at same rank.
    assert scores["x"] > scores["y"]
    assert fused[0][0] == "x"


def test_k_flattens_rank_advantage():
    lists = [["a", "b"]]
    small_k = dict(reciprocal_rank_fusion(lists, k=1))
    large_k = dict(reciprocal_rank_fusion(lists, k=1000))
    # gap between rank1 and rank2 shrinks as k grows
    assert (small_k["a"] - small_k["b"]) > (large_k["a"] - large_k["b"])
