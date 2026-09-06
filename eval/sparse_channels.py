"""D19: compare lexical/sparse retrieval channels on the same indexed corpus.

The M2 spec picked BGE-M3's lexical weights as the sparse channel, rejecting
Postgres FTS because Korean needs a morphological analyzer. This measures that
decision instead of assuming it, and adds a third option the spec did not
consider (pg_trgm).

Each channel is measured ALONE, since the decision is about channel quality —
RRF fusion and reranking sit downstream of whichever one we pick.

  fts   Postgres full-text search, 'simple' config (no Korean analyzer). Given
        OR semantics rather than the default AND so partial matches still rank,
        which is deliberately charitable to it.
  trgm  pg_trgm word_similarity: character trigrams, morphology-free, scores
        the best matching substring rather than the whole document.
  bge   BGE-M3 lexical weights (what we ship). Needs the embedding server.

    python -m eval.sparse_channels --channels fts,trgm,bge

Reads the corpus already indexed by `python -m eval.run --corpus hard`; it does
not re-index. `fts`/`trgm` need only Postgres; `bge` also needs TEI_URL.
Requires the pg_trgm extension: CREATE EXTENSION IF NOT EXISTS pg_trgm;
"""

import argparse
import asyncio
import re
import sys

from sqlalchemy import select, text

from eval import metrics
from eval.corpora import hard

from app.db import SessionLocal, engine
from app.models import Chunk, Document

K = 5
DOC_NAME = "eval_corpus_hard.pdf"
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")


async def _doc_id(session):
    doc = (
        await session.execute(select(Document).where(Document.filename == DOC_NAME))
    ).scalars().first()
    if doc is None:
        sys.exit(
            f"{DOC_NAME} is not indexed — run `python -m eval.run --corpus hard` first"
        )
    return doc.id


async def _require_pg_trgm(session) -> None:
    installed = (
        await session.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        )
    ).first()
    if not installed:
        sys.exit("pg_trgm is not installed — run: CREATE EXTENSION IF NOT EXISTS pg_trgm;")


async def fts_pages(session, question: str, doc_id, k: int) -> list[int]:
    """OR-ed tsquery over 'simple' tokens, ranked by ts_rank."""
    terms = _TOKEN_RE.findall(question)
    if not terms:
        return []
    tsquery = " | ".join(terms)
    rows = (await session.execute(
        text("""
            SELECT page_from,
                   ts_rank(to_tsvector('simple', content),
                           to_tsquery('simple', :q)) AS rank
            FROM chunks
            WHERE document_id = :doc
              AND to_tsvector('simple', content) @@ to_tsquery('simple', :q)
            ORDER BY rank DESC
            LIMIT :k
        """),
        {"q": tsquery, "doc": doc_id, "k": k},
    )).all()
    return [r[0] for r in rows]


async def trgm_pages(session, question: str, doc_id, k: int) -> list[int]:
    """pg_trgm word_similarity: best-substring trigram match."""
    rows = (await session.execute(
        text("""
            SELECT page_from, word_similarity(:q, content) AS sim
            FROM chunks
            WHERE document_id = :doc
            ORDER BY sim DESC
            LIMIT :k
        """),
        {"q": question, "doc": doc_id, "k": k},
    )).all()
    return [r[0] for r in rows]


async def bge_pages(session, question: str, doc_id, k: int) -> list[int]:
    """BGE-M3 sparse channel alone (sparsevec inner product)."""
    from app.services import embeddings

    _, sparse = await embeddings.embed_query_full(question)
    rows = (await session.execute(
        select(Chunk)
        .where(Chunk.document_id == doc_id, Chunk.sparse_embedding.isnot(None))
        .order_by(Chunk.sparse_embedding.max_inner_product(sparse))
        .limit(k)
    )).scalars().all()
    return [c.page_from for c in rows]


CHANNELS = {"fts": fts_pages, "trgm": trgm_pages, "bge": bge_pages}


def print_table(title: str, rows: list[dict], channels: list[str]) -> None:
    if not rows:
        return
    keys = ["R@1", "R@3", f"R@{K}", "MRR", f"nDCG@{K}"]
    print(f"\n=== {title} (n={len(rows)}) ===")
    print(f"{'channel':10}" + "".join(f"{key:>9}" for key in keys))
    for channel in channels:
        agg = {k: sum(r[channel][k] for r in rows) / len(rows) for k in keys}
        print(f"{channel:10}" + "".join(f"{agg[k]:9.3f}" for k in keys))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Compare lexical retrieval channels")
    parser.add_argument("--channels", default="fts,trgm,bge")
    args = parser.parse_args()

    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    unknown = [c for c in channels if c not in CHANNELS]
    if unknown:
        sys.exit(f"unknown channel(s) {unknown}; choose from {list(CHANNELS)}")

    rows: list[dict] = []
    async with SessionLocal() as session:
        doc_id = await _doc_id(session)
        if "trgm" in channels:
            await _require_pg_trgm(session)

        for question, gold, qtype in hard.QUERIES:
            row = {"q": question, "gold": gold, "type": qtype}
            for channel in channels:
                pages = await CHANNELS[channel](session, question, doc_id, K)
                row[channel] = metrics.score_all(pages, gold, K)
            rows.append(row)

            marks = " ".join(
                f"{c}={'✓' if row[c]['R@1'] else ('~' if row[c][f'R@{K}'] else '✗')}"
                for c in channels
            )
            print(f"[{qtype:8}] gold p{gold:<3} {marks} | {question[:34]}")

    print_table("ALL", rows, channels)
    for qtype in sorted({r["type"] for r in rows}):
        print_table(qtype, [r for r in rows if r["type"] == qtype], channels)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
