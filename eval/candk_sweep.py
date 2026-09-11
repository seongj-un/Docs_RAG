"""Measure what shrinking CAND_K costs, separating the two ways it can fail.

CAND_K caps how many chunks each retriever hands to fusion, and fusion hands to
the reranker. Shrinking it is the other lever on rerank cost — and unlike input
truncation it removes whole candidates rather than the text inside them, so the
failure has a hard floor worth measuring directly:

    후보 적중 (candidate recall)  gold chunk survived into the candidate set
    R@1                          gold chunk came first after reranking

**후보 적중 is the ceiling on R@1.** Once the gold chunk is not in the set the
reranker cannot recover it, no matter how good it is. Reporting the two side by
side says whether a drop is "retrieval never found it" or "the reranker ranked
it low" — the same distinction M4 built tracing for.

    python -m eval.candk_sweep --corpus wide

Requires Postgres and a reachable embedding/rerank server.
"""

import argparse
import asyncio
import time

from sqlalchemy import select

from eval import metrics
from eval.corpora import load
from eval.run import index_corpus

from app.config import settings
from app.constants import SEED_USER_ID
from app.db import SessionLocal, engine
from app.models import Chunk
from app.services import embeddings, rerank, retrieve

CAND_KS = (100, 50, 30, 20, 10, 5)


async def page_to_chunk(session, doc_id) -> dict[int, object]:
    rows = (
        (await session.execute(select(Chunk).where(Chunk.document_id == doc_id)))
        .scalars()
        .all()
    )
    mapping: dict[int, object] = {}
    for chunk in rows:
        mapping.setdefault(chunk.page_from, chunk.id)
    return mapping


async def measure(session, corpus, doc_id, pages, cand_k: int, k: int) -> list[dict]:
    rows = []
    for question, gold, qtype in corpus.QUERIES:
        gold_id = pages[gold]
        dense, sparse = await embeddings.embed_query_full(question)
        fused = await retrieve.hybrid_search(
            session, dense, sparse, user_id=SEED_USER_ID,
            document_id=doc_id, cand_k=cand_k,
        )
        candidates = fused.candidates
        started = time.perf_counter()
        ranked = await rerank.rerank(question, [c.content for c in candidates])
        elapsed_ms = (time.perf_counter() - started) * 1000
        ranked_pages = [candidates[i].page_from for i, _ in ranked[:k]]

        row = {
            "q": question, "gold": gold, "type": qtype,
            "ms": elapsed_ms, "pairs": len(candidates),
            "cand_hit": float(gold_id in {c.chunk_id for c in candidates}),
            "dense_hit": float(gold_id in set(fused.dense_ids)),
            "sparse_hit": float(gold_id in set(fused.sparse_ids)),
        }
        row.update(metrics.score_all(ranked_pages, gold, k))
        rows.append(row)
    return rows


def _avg(rows, key):
    return sum(r[key] for r in rows) / len(rows) if rows else 0.0


def table(title: str, results: dict[int, list[dict]], select_type=None) -> None:
    print(f"\n--- {title} ---")
    cols = ["후보적중", "dense", "sparse", "R@1", "R@3", "MRR"]
    print(f"{'CAND_K':>7}" + "".join(f"{c:>9}" for c in cols)
          + f"{'후보수':>7}{'리랭킹ms':>9}")
    for cand_k in CAND_KS:
        rows = results[cand_k]
        if select_type:
            rows = [r for r in rows if r["type"] == select_type]
        if not rows:
            continue
        cells = [
            _avg(rows, "cand_hit"), _avg(rows, "dense_hit"), _avg(rows, "sparse_hit"),
            _avg(rows, "R@1"), _avg(rows, "R@3"), _avg(rows, "MRR"),
        ]
        print(f"{cand_k:>7}" + "".join(f"{v:9.3f}" for v in cells)
              + f"{_avg(rows, 'pairs'):7.0f}{_avg(rows, 'ms'):9.0f}")


def losses(results: dict[int, list[dict]]) -> None:
    """Separate 'never a candidate' from 'candidate but not ranked first'."""
    print("\n=== R@1 실패의 원인 분해 ===")
    print(f"{'CAND_K':>7}{'실패':>6}{'후보에 없음':>12}{'후보엔 있었음':>14}")
    for cand_k in CAND_KS:
        rows = results[cand_k]
        missed = [r for r in rows if not r["R@1"]]
        absent = sum(1 for r in missed if not r["cand_hit"])
        print(f"{cand_k:>7}{len(missed):>6}{absent:>12}{len(missed) - absent:>14}")


async def margins(session, corpus, doc_id, pages, probe: int) -> None:
    """Where does the gold chunk actually sit in each first-stage ranking?

    A sweep that never fails says only "CAND_K was never the binding
    constraint on this corpus". The rank of the gold chunk says *how much
    room was left* — and therefore which CAND_K would start cutting.
    """
    print("\n=== 1스테이지에서 정답 청크의 순위 (여유 측정) ===")
    print(f"{'질의':38}{'dense':>7}{'sparse':>8}{'RRF':>6}")
    ranks = {"dense": [], "sparse": [], "rrf": []}
    for question, gold, _ in corpus.QUERIES:
        gold_id = pages[gold]
        dense, sparse = await embeddings.embed_query_full(question)
        fused = await retrieve.hybrid_search(
            session, dense, sparse, user_id=SEED_USER_ID,
            document_id=doc_id, cand_k=probe,
        )

        def rank_in(ids) -> int | None:
            return ids.index(gold_id) + 1 if gold_id in ids else None

        got = {
            "dense": rank_in(fused.dense_ids),
            "sparse": rank_in(fused.sparse_ids),
            "rrf": rank_in([c.chunk_id for c in fused.candidates]),
        }
        for key, value in got.items():
            ranks[key].append(value)
        cells = "".join(f"{('-' if v is None else v):>7}" for v in
                        (got["dense"], got["sparse"], got["rrf"]))
        print(f"{question[:36]:38}{cells}")

    print(f"\n{'채널':8}{'중앙':>6}{'최악':>6}{'>5위':>7}{'>10위':>7}{'미포함':>8}")
    for key in ("dense", "sparse", "rrf"):
        found = sorted(r for r in ranks[key] if r is not None)
        missing = sum(1 for r in ranks[key] if r is None)
        median = found[len(found) // 2] if found else 0
        print(f"{key:8}{median:>6}{(found[-1] if found else 0):>6}"
              f"{sum(1 for r in found if r > 5):>7}{sum(1 for r in found if r > 10):>7}"
              f"{missing:>8}")
    print(f"(상위 {probe}개까지만 조회 — '미포함'은 그보다 아래라는 뜻)")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="wide")
    parser.add_argument("--k", type=int, default=5)
    # 구간을 좁혀 다시 돌 수 있게 한다. 실제 크기 코퍼스에서는 한 설정이
    # 질의당 수 초라 전 구간(100~5)이 한 시간을 넘는데, 답이 필요한 곳은
    # 대개 무릎 근처 몇 점뿐이다.
    parser.add_argument(
        "--ks", default=None,
        help="쉼표로 구분한 CAND_K 목록 (예: 50,45,40,35,30). 생략하면 기본 전 구간.",
    )
    args = parser.parse_args()

    global CAND_KS
    if args.ks:
        CAND_KS = tuple(sorted((int(x) for x in args.ks.split(",")), reverse=True))
        print(f"CAND_K 구간을 좁혀서 돈다: {CAND_KS}")

    corpus = load(args.corpus)
    print(f"코퍼스 {args.corpus}: {len(corpus.CLAUSES)}쪽, 질의 {len(corpus.QUERIES)}개")
    if len(corpus.CLAUSES) <= max(CAND_KS):
        print(f"  ⚠️  코퍼스({len(corpus.CLAUSES)})가 최대 CAND_K({max(CAND_KS)})보다 작다 — "
              "그 지점에선 전 청크가 후보라 자르는 효과가 없다.")

    doc_id = await index_corpus(
        corpus, f"/tmp/eval_corpus_{args.corpus}.pdf", f"eval_corpus_{args.corpus}.pdf"
    )

    results: dict[int, list[dict]] = {}
    async with SessionLocal() as session:
        pages = await page_to_chunk(session, doc_id)
        missing = [g for _, g, _ in corpus.QUERIES if g not in pages]
        if missing:
            raise RuntimeError(f"gold pages without a chunk: {missing}")
        for cand_k in CAND_KS:
            rows = await measure(session, corpus, doc_id, pages, cand_k, args.k)
            results[cand_k] = rows
            print(f"[sweep] CAND_K={cand_k:<4} 후보적중 {_avg(rows, 'cand_hit'):.3f}  "
                  f"R@1 {_avg(rows, 'R@1'):.3f}  후보 {_avg(rows, 'pairs'):.0f}개  "
                  f"리랭킹 {_avg(rows, 'ms'):.0f}ms")

    print("\n" + "=" * 74)
    print(f"CAND_K 스윕 (기본값 {settings.cand_k})  · 후보적중 = R@1의 천장")
    print("=" * 74)
    table(f"전체 (n={len(results[CAND_KS[0]])})", results)
    for qtype in sorted({r["type"] for r in results[CAND_KS[0]]}):
        table(qtype, results, qtype)
    losses(results)

    async with SessionLocal() as session:
        await margins(session, corpus, doc_id, pages, max(CAND_KS))

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
