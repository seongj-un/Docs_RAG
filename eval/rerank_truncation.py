"""Measure what truncating reranker input costs, by fact depth.

``RERANK_MAX_CHARS`` is the largest lever on query latency (reranking is linear
in input length), but it can only pay off if it does not throw away the text
that decides the ranking. This sweeps the setting over one corpus and reports
retrieval quality split by where the answering fact sits in the chunk.

    python -m eval.rerank_truncation --corpus longchunk

Read the per-depth rows, not the total: an average over depths hides the whole
effect, because truncation cannot touch a fact that sits in front of the cut.

Requires Postgres and a reachable embedding/rerank server.
"""

import argparse
import asyncio
import time

from eval import metrics
from eval.corpora import load
from eval.run import index_corpus

from app.config import settings
from app.constants import SEED_USER_ID
from app.db import SessionLocal, engine
from app.services import embeddings, rerank, retrieve

LIMITS = (0, 512, 256, 128)
KEYS = ("R@1", "R@3", "MRR")


def label(limit: int) -> str:
    return "자르기 없음" if limit == 0 else f"{limit}자"


async def measure(session, corpus, doc_id, k: int) -> list[dict]:
    rows = []
    for question, gold, depth in corpus.QUERIES:
        dense, sparse = await embeddings.embed_query_full(question)
        fused = await retrieve.hybrid_search(
            session, dense, sparse, user_id=SEED_USER_ID, document_id=doc_id
        )
        candidates = fused.candidates
        started = time.perf_counter()
        ranked = await rerank.rerank(question, [c.content for c in candidates])
        elapsed_ms = (time.perf_counter() - started) * 1000
        pages = [candidates[i].page_from for i, _ in ranked[:k]]
        row = {"q": question, "gold": gold, "depth": depth,
               "ms": elapsed_ms, "pairs": len(candidates),
               "top1": pages[0] if pages else None}
        row.update(metrics.score_all(pages, gold, k))
        rows.append(row)
    return rows


def report(results: dict[int, list[dict]], depths: list[str]) -> None:
    print("\n" + "=" * 74)
    print("깊이별 검색 품질 (하이브리드 + 리랭킹)")
    print("=" * 74)
    for depth in depths:
        print(f"\n--- {depth} ---")
        header = f"{'자르기':14}" + "".join(f"{key:>8}" for key in KEYS) + f"{'리랭킹ms':>10}"
        print(header)
        for limit in LIMITS:
            rows = [r for r in results[limit] if r["depth"] == depth]
            if not rows:
                continue
            cells = "".join(f"{sum(r[key] for r in rows) / len(rows):8.3f}" for key in KEYS)
            ms = sum(r["ms"] for r in rows) / len(rows)
            print(f"{label(limit):14}" + cells + f"{ms:10.0f}")

    print(f"\n--- 전체 (n={len(results[LIMITS[0]])}) ---")
    print(f"{'자르기':14}" + "".join(f"{key:>8}" for key in KEYS) + f"{'리랭킹ms':>10}")
    for limit in LIMITS:
        rows = results[limit]
        cells = "".join(f"{sum(r[key] for r in rows) / len(rows):8.3f}" for key in KEYS)
        ms = sum(r["ms"] for r in rows) / len(rows)
        print(f"{label(limit):14}" + cells + f"{ms:10.0f}")

    base = {r["q"]: r for r in results[LIMITS[0]]}
    print("\n=== 자르기로 top-1이 바뀐 질의 ===")
    changed = False
    for limit in LIMITS[1:]:
        for row in results[limit]:
            was = base[row["q"]]["top1"]
            if row["top1"] != was:
                changed = True
                mark = "✓" if row["top1"] == row["gold"] else "✗"
                print(f"  {label(limit):8} {row['depth']:6} gold p{row['gold']:<3} "
                      f"p{was} → p{row['top1']} {mark} | {row['q'][:34]}")
    if not changed:
        print("  (없음)")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="longchunk",
                        choices=("longchunk", "hard", "simple"))
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()

    corpus = load(args.corpus)
    lengths = sorted(len(c) for c in corpus.CLAUSES)
    print(f"코퍼스 {args.corpus}: {len(corpus.CLAUSES)}쪽, "
          f"조항 길이 최소 {lengths[0]}자 / 중앙 {lengths[len(lengths) // 2]}자 / 최대 {lengths[-1]}자")
    if lengths[-1] <= min(limit for limit in LIMITS if limit):
        print(f"  ⚠️  모든 조항이 최소 자르기 값({min(l for l in LIMITS if l)}자)보다 짧다 — "
              "이 코퍼스에서 자르기는 아무 일도 하지 않는다(음성 대조군).")

    doc_id = await index_corpus(
        corpus, f"/tmp/eval_corpus_{args.corpus}.pdf", f"eval_corpus_{args.corpus}.pdf"
    )

    original = settings.rerank_max_chars
    results: dict[int, list[dict]] = {}
    try:
        async with SessionLocal() as session:
            for limit in LIMITS:
                settings.rerank_max_chars = limit
                rows = await measure(session, corpus, doc_id, args.k)
                results[limit] = rows
                hits = sum(r["R@1"] for r in rows)
                print(f"[sweep] {label(limit):12} R@1 {hits:.0f}/{len(rows)}  "
                      f"후보 {rows[0]['pairs']}개")
    finally:
        settings.rerank_max_chars = original

    depths = sorted({r["depth"] for r in results[LIMITS[0]]},
                    key=lambda d: ["front", "mid", "back"].index(d)
                    if d in ("front", "mid", "back") else 99)
    report(results, depths)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
