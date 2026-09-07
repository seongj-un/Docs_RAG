"""Run the RAG pipeline over the golden set and dump evaluation records.

Produces one record per question with everything a quality metric needs —
question, generated answer, retrieved contexts, and the hand-written reference
— so metric computation is a separate, offline step that never has to re-run
the pipeline (or hold the embedding server open).

Retrieval metrics and refusal accuracy are computed here, since both fall out
of the labels directly and need no LLM judge.

    python -m eval.golden_run --out /tmp/golden_records.json

Requires Postgres, the embedding/rerank server, and an LLM key.
"""

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

from sqlalchemy import select

from eval import metrics, pdf
from eval.throttle import Throttle, call_with_retry
from eval.corpora import golden

from app.config import settings
from app.constants import SEED_USER_ID
from app.db import SessionLocal, engine
from app.models import Chunk, Document
from app.services import embeddings, generate, ingest, rerank, retrieve

K = 5


async def _index_document(name: str, clauses: list[str]) -> uuid.UUID:
    path = f"/tmp/golden_{name}.pdf"
    doc_name = f"golden_{name}.pdf"
    pdf.build_pdf(path, clauses)
    pdf.verify_pdf(path, clauses)

    async with SessionLocal() as session:
        stale = (
            (await session.execute(select(Document).where(Document.filename == doc_name)))
            .scalars()
            .all()
        )
        for doc in stale:
            await session.delete(doc)
        await session.commit()

        doc = Document(
            user_id=SEED_USER_ID,
            filename=doc_name,
            mime_type="application/pdf",
            status="pending",
        )
        session.add(doc)
        await session.commit()
        await session.refresh(doc)
        doc_id = doc.id

    await ingest.index_document(doc_id, path)

    async with SessionLocal() as session:
        doc = await session.get(Document, doc_id)
        if doc.status != "ready":
            raise RuntimeError(f"{doc_name} failed to index: {doc.error}")
        n = len(
            (await session.execute(select(Chunk).where(Chunk.document_id == doc_id)))
            .scalars()
            .all()
        )
        print(f"[index] {doc_name}: {doc.num_pages}p, {n} chunks")
    return doc_id


async def _answer(session, question: str, throttle: Throttle):
    """Run the production retrieval+generation path across the whole corpus.

    Deliberately corpus-wide (no document_id): the golden set spans three
    documents, so this also measures whether retrieval picks the right document.
    """
    dense, sparse = await embeddings.embed_query_full(question)
    fused = await retrieve.hybrid_search(session, dense, sparse, user_id=SEED_USER_ID)
    chunks: list[retrieve.RetrievedChunk] = []
    if fused.candidates:
        ranked = await rerank.rerank(question, [c.content for c in fused.candidates])
        for idx, score in ranked[: settings.rerank_top]:
            chunk = fused.candidates[idx]
            chunk.score = score
            chunks.append(chunk)
    result = await call_with_retry(
        lambda: generate.answer_question(
            question,
            chunks,
            model=settings.eval_llm_model,
            min_score=settings.rerank_min_score,
        ),
        throttle,
    )
    return chunks, result


def _summarize(records: list[dict]) -> None:
    answerable = [r for r in records if r["type"] != "unanswerable"]
    unanswerable = [r for r in records if r["type"] == "unanswerable"]

    print("\n=== 검색 품질 (gold page 기준, 답변 가능 문항) ===")
    print(f"{'type':12}{'n':>4}{'R@1':>8}{'R@3':>8}{f'R@{K}':>8}{'MRR':>8}")
    groups: dict[str, list[dict]] = {"ALL": answerable}
    for record in answerable:
        groups.setdefault(record["type"], []).append(record)
    for name, rows in groups.items():
        n = len(rows)
        r1 = sum(r["retrieval"]["R@1"] for r in rows) / n
        r3 = sum(r["retrieval"]["R@3"] for r in rows) / n
        rk = sum(r["retrieval"][f"R@{K}"] for r in rows) / n
        mrr = sum(r["retrieval"]["MRR"] for r in rows) / n
        print(f"{name:12}{n:>4}{r1:>8.3f}{r3:>8.3f}{rk:>8.3f}{mrr:>8.3f}")

    print("\n=== 거부 가드레일 ===")
    wrong_refusals = [r for r in answerable if r["refused"]]
    correct_refusals = [r for r in unanswerable if r["refused"]]
    print(f"  답변 가능한데 거부한 경우: {len(wrong_refusals)}/{len(answerable)}")
    print(f"  문서에 없어 올바로 거부:   {len(correct_refusals)}/{len(unanswerable)}")
    for record in wrong_refusals:
        print(f"    ✗ 잘못 거부: {record['question'][:40]}")
    for record in unanswerable:
        if not record["refused"]:
            print(f"    ✗ 거부했어야: {record['question'][:40]}")

    latency = [r["latency_ms"] for r in records]
    print(f"\n지연: 중앙값 {sorted(latency)[len(latency) // 2]}ms, 최대 {max(latency)}ms")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/golden_records.json")
    parser.add_argument("--limit", type=int, default=0, help="only first N questions")
    parser.add_argument("--rpm", type=int, default=5,
                        help="LLM requests per minute (Gemini free tier: 5)")
    args = parser.parse_args()

    for name, clauses in golden.DOCUMENTS.items():
        await _index_document(name, clauses)

    questions = golden.QUESTIONS[: args.limit] if args.limit else golden.QUESTIONS
    throttle = Throttle(args.rpm)
    print(f'[pace] {args.rpm} req/min -> ~{len(questions) * 60 / max(args.rpm, 1) / 60:.1f}min\n')
    records: list[dict] = []

    async with SessionLocal() as session:
        for i, item in enumerate(questions, start=1):
            t0 = time.perf_counter()
            chunks, result = await _answer(session, item["q"], throttle)
            elapsed = int((time.perf_counter() - t0) * 1000)

            pages = [c.page_from for c in chunks]
            gold = item["gold_pages"]
            # A question is "retrieved correctly" if any gold page is present.
            retrieval = (
                {
                    "R@1": max((metrics.recall_at_k(pages, g, 1) for g in gold), default=0.0),
                    "R@3": max((metrics.recall_at_k(pages, g, 3) for g in gold), default=0.0),
                    f"R@{K}": max((metrics.recall_at_k(pages, g, K) for g in gold), default=0.0),
                    "MRR": max((metrics.reciprocal_rank(pages, g) for g in gold), default=0.0),
                }
                if gold
                else {}
            )

            records.append({
                "question": item["q"],
                "document": item["doc"],
                "type": item["type"],
                "reference": item["reference"],
                "gold_pages": gold,
                "answer": result.answer,
                "refused": result.refused,
                "contexts": [c.content for c in chunks],
                "context_chunk_ids": [str(c.chunk_id) for c in chunks],
                "retrieved_pages": pages,
                "rerank_scores": [round(c.score, 4) for c in chunks],
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "latency_ms": elapsed,
                "retrieval": retrieval,
            })
            mark = "✓" if (not gold and result.refused) or (
                gold and retrieval.get("R@1")
            ) else ("~" if gold and retrieval.get(f"R@{K}") else "✗")
            print(f"[{i:2}/{len(questions)}] {mark} {item['type']:12} {item['q'][:38]}")
            # Save after every record: a rate-limit crash mid-sweep must not
            # discard the questions already paid for.
            Path(args.out).write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    Path(args.out).write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n{len(records)} records -> {args.out}")
    _summarize(records)

    tokens = sum(r["tokens_in"] + r["tokens_out"] for r in records)
    print(f"생성 토큰 합계: {tokens}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
