"""Compare retrieval configurations on a labeled corpus.

Indexes the corpus through the real ingest pipeline (dense+sparse), then scores
each configuration on the query set. Measures retrieval only — generation is
excluded so the numbers reflect the retriever, not the LLM.

    python -m eval.run --corpus hard

Requires Postgres and a reachable embedding/rerank server (TEI_URL, RERANK_URL).
"""

import argparse
import asyncio
import uuid

from sqlalchemy import select

from eval import metrics, pdf
from eval.corpora import load

from app.db import SessionLocal, engine
from app.models import Chunk, Document
from app.services import embeddings, ingest, rerank, retrieve

CONFIGS = ("dense", "hybrid", "hybrid+rerank")


async def index_corpus(corpus, path: str, doc_name: str) -> uuid.UUID:
    """Build the fixture PDF and index it, replacing any prior run's copy."""
    pdf.build_pdf(path, corpus.CLAUSES)
    pdf.verify_pdf(path, corpus.CLAUSES)

    async with SessionLocal() as session:
        stale = (
            (await session.execute(select(Document).where(Document.filename == doc_name)))
            .scalars()
            .all()
        )
        for doc in stale:
            await session.delete(doc)
        await session.commit()

        doc = Document(filename=doc_name, mime_type="application/pdf", status="pending")
        session.add(doc)
        await session.commit()
        await session.refresh(doc)
        doc_id = doc.id

    await ingest.index_document(doc_id, path)

    async with SessionLocal() as session:
        doc = await session.get(Document, doc_id)
        chunks = (
            (await session.execute(select(Chunk).where(Chunk.document_id == doc_id)))
            .scalars()
            .all()
        )
        with_sparse = sum(1 for c in chunks if c.sparse_embedding is not None)
        print(f"[index] status={doc.status} pages={doc.num_pages} "
              f"chunks={len(chunks)} with_sparse={with_sparse}")
        if doc.status != "ready":
            raise RuntimeError(f"indexing failed: {doc.error}")
    return doc_id


async def dense_pages(session, question: str, doc_id: uuid.UUID, k: int) -> list[int]:
    embedding = await embeddings.embed_query(question)
    hits = await retrieve.search(session, embedding, document_id=doc_id, top_k=k)
    return [h.page_from for h in hits]


async def hybrid_pages(
    session, question: str, doc_id: uuid.UUID, k: int, do_rerank: bool
) -> list[int]:
    dense, sparse = await embeddings.embed_query_full(question)
    candidates = await retrieve.hybrid_search(session, dense, sparse, document_id=doc_id)
    if not do_rerank:
        return [c.page_from for c in candidates[:k]]
    ranked = await rerank.rerank(question, [c.content for c in candidates])
    return [candidates[i].page_from for i, _ in ranked[:k]]


def aggregate(rows: list[dict], config: str, k: int) -> dict[str, float]:
    keys = ["R@1", "R@3", f"R@{k}", "MRR", f"nDCG@{k}"]
    return {key: sum(r[config][key] for r in rows) / len(rows) for key in keys}


def print_table(title: str, rows: list[dict], k: int) -> None:
    if not rows:
        return
    keys = ["R@1", "R@3", f"R@{k}", "MRR", f"nDCG@{k}"]
    print(f"\n=== {title} (n={len(rows)}) ===")
    print(f"{'config':16}" + "".join(f"{key:>9}" for key in keys))
    for config in CONFIGS:
        agg = aggregate(rows, config, k)
        print(f"{config:16}" + "".join(f"{agg[key]:9.3f}" for key in keys))


def print_disagreements(rows: list[dict]) -> None:
    print("\n=== rank-1 불일치 (구성 간 top-1이 갈린 질의) ===")
    found = False
    for row in rows:
        tops = tuple(row["_top1"][c] for c in CONFIGS)
        if len(set(tops)) > 1:
            found = True
            detail = "  ".join(f"{c}=p{t}" for c, t in zip(CONFIGS, tops))
            print(f"  gold p{row['gold']:<3} {detail} | {row['q'][:40]}")
    if not found:
        print("  (없음 — 모든 질의에서 top-1이 동일)")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="hard", choices=("simple", "hard"))
    parser.add_argument("--k", type=int, default=5, help="cutoff for R@k / nDCG@k")
    args = parser.parse_args()

    corpus = load(args.corpus)
    path = f"/tmp/eval_corpus_{args.corpus}.pdf"
    doc_name = f"eval_corpus_{args.corpus}.pdf"
    k = args.k

    doc_id = await index_corpus(corpus, path, doc_name)

    rows: list[dict] = []
    async with SessionLocal() as session:
        for question, gold, qtype in corpus.QUERIES:
            pages = {
                "dense": await dense_pages(session, question, doc_id, k),
                "hybrid": await hybrid_pages(session, question, doc_id, k, False),
                "hybrid+rerank": await hybrid_pages(session, question, doc_id, k, True),
            }
            row = {"q": question, "gold": gold, "type": qtype,
                   "_top1": {c: (p[0] if p else None) for c, p in pages.items()}}
            for config, ranked in pages.items():
                row[config] = metrics.score_all(ranked, gold, k)
            rows.append(row)

            marks = " ".join(
                f"{c.split('+')[-1][:4]}={'✓' if row[c]['R@1'] else ('~' if row[c][f'R@{k}'] else '✗')}"
                for c in CONFIGS
            )
            print(f"[{qtype:8}] gold p{gold:<3} {marks} | {question[:36]}")

    print_table("ALL", rows, k)
    for qtype in sorted({r["type"] for r in rows}):
        print_table(qtype, [r for r in rows if r["type"] == qtype], k)
    print_disagreements(rows)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
