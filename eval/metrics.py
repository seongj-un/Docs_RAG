"""Retrieval metrics, at two grains: page-level (M2-M4) and chunk-level (M7).

**Page level** — a retrieved chunk counts as relevant when its page equals the
query's gold page. ``ranked_pages`` is the pages of the retrieved chunks in
rank order (index 0 = rank 1). Each query has exactly one gold page, so
IDCG = 1 and nDCG reduces to the discount of the first hit.

**Chunk level** — M7's L1 grain. Relevance is a *gold span*: a snippet of
source text that answers the question, resolved at run time to the set of
chunks it overlaps (see ``eval/gold.py``). A question can carry several spans,
and one span can resolve to several chunks, so relevance is a list of sets
rather than a single id.

두 계층을 한 함수로 일반화하지 않고 나란히 둔다. 페이지 쪽은 "정답이 정확히
하나"를 전제로 IDCG=1 을 상수로 접어 넣은 식이고, 청크 쪽은 정답이 여러 개인
경우의 macro 평균과 진짜 IDCG 가 필요하다 — 같은 이름 아래 합치면 둘 중
하나는 옵션 인자로 뒤덮인 거짓말이 된다. 무엇보다 페이지 쪽은
``eval/run.py`` · ``candk_sweep.py`` · ``golden_run.py`` 가 이미 쓰고 있고,
그 숫자들은 README 와 config 주석에 근거로 박혀 있다. 의미가 조용히 바뀌면
그 근거가 전부 거짓이 된다.
"""

import math
import uuid

# 순위에 정답 청크가 없을 때의 표식. 0 이 아니라 None 인 이유는 "1위"와
# "못 찾음"이 둘 다 참인 정수로 표현되면 diff 가 구분을 못 하기 때문이다.
NO_RANK: int | None = None


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


# --- M7 L1: chunk-level, multi-span relevance ---

GoldSets = list[set[uuid.UUID]]
"""One set of chunk ids per gold span. Empty list = nothing to measure."""

DEFAULT_KS = (1, 3, 5, 10)
NDCG_K = 10


def span_recall_at_k(ranked_ids: list[uuid.UUID], gold: GoldSets, k: int) -> float:
    """Fraction of gold *spans* covered by the top-k chunks.

    Macro over spans, not over chunks: a question whose answer is spread over
    two spans scores 0.5 when retrieval found one of them. Counting chunks
    instead would let a span that happened to split into four chunks outweigh
    a span that fit in one — an artifact of chunking, which is precisely what
    W5 is going to change underneath this metric.
    """
    if not gold:
        return 0.0
    top = set(ranked_ids[:k])
    return sum(1 for span in gold if span & top) / len(gold)


def first_gold_rank(ranked_ids: list[uuid.UUID], gold: GoldSets) -> int | None:
    """1-based rank of the first chunk covering any gold span, else None."""
    if not gold:
        return NO_RANK
    wanted: set[uuid.UUID] = set().union(*gold)
    for i, chunk_id in enumerate(ranked_ids, start=1):
        if chunk_id in wanted:
            return i
    return NO_RANK


def chunk_reciprocal_rank(ranked_ids: list[uuid.UUID], gold: GoldSets) -> float:
    """1/rank of the first gold chunk, or 0.0 when none was retrieved."""
    rank = first_gold_rank(ranked_ids, gold)
    return 1.0 / rank if rank else 0.0


def chunk_ndcg_at_k(ranked_ids: list[uuid.UUID], gold: GoldSets, k: int) -> float:
    """nDCG@k where gain is 1 for the first chunk covering a *new* span.

    A second chunk of a span already covered scores 0. Without that rule a
    span that split into three chunks would pay out three times and the metric
    would reward chunk count rather than coverage — the same distortion
    ``span_recall_at_k`` avoids. IDCG is therefore the ideal ordering of
    ``min(len(gold), k)`` distinct spans.
    """
    if not gold:
        return 0.0
    covered: set[int] = set()
    dcg = 0.0
    for i, chunk_id in enumerate(ranked_ids[:k], start=1):
        for j, span in enumerate(gold):
            if j not in covered and chunk_id in span:
                covered.add(j)
                dcg += 1.0 / math.log2(i + 1)
                break
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(gold), k) + 1))
    return dcg / ideal if ideal else 0.0


def page_recall_at_k(ranked_pages: list, gold_pages: list, k: int) -> float:
    """Fraction of gold pages present in the top-k retrieved pages.

    A "page" is whatever identifies one for the corpus at hand — a page number
    inside one document, or a ``(document, page)`` pair when the dataset spans
    several. 문서가 여럿인데 쪽 번호만 비교하면 임대차 2쪽과 보험 2쪽이 같은
    쪽이 되어 recall 이 공짜로 올라간다.

    Reported alongside the chunk-level numbers because the W1 baseline —
    Notion's own MCP search — returns pages/blocks, not chunks. Comparing our
    chunk recall against its page recall would flatter us; the fair comparison
    needs both grains on both sides.
    """
    if not gold_pages:
        return 0.0
    top = set(ranked_pages[:k])
    return sum(1 for page in set(gold_pages) if page in top) / len(set(gold_pages))


def score_chunks(
    ranked_ids: list[uuid.UUID],
    gold: GoldSets,
    *,
    ranked_pages: list | None = None,
    gold_pages: list | None = None,
    ks: tuple[int, ...] = DEFAULT_KS,
    ndcg_k: int = NDCG_K,
) -> dict[str, float]:
    """All chunk-level L1 metrics for one question, keyed for aggregation."""
    scored = {f"R@{k}": span_recall_at_k(ranked_ids, gold, k) for k in ks}
    scored["MRR"] = chunk_reciprocal_rank(ranked_ids, gold)
    scored[f"nDCG@{ndcg_k}"] = chunk_ndcg_at_k(ranked_ids, gold, ndcg_k)
    if ranked_pages is not None and gold_pages is not None:
        for k in ks:
            scored[f"pageR@{k}"] = page_recall_at_k(ranked_pages, gold_pages, k)
    return scored


def metric_keys(ks: tuple[int, ...] = DEFAULT_KS, ndcg_k: int = NDCG_K) -> list[str]:
    """The key order reports and diffs print in."""
    return [f"R@{k}" for k in ks] + ["MRR", f"nDCG@{ndcg_k}"]
