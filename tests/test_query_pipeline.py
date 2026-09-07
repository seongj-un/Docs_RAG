"""Orchestration tests for the query retrieval pipeline (no infra/DB).

Covers both what the pipeline returns and what it hands to tracing: the stage
records are what make retrieval-vs-generation failures separable later, so a
silently empty stage would be a real defect.
"""

import asyncio
import uuid

from app.routers import query as qr
from app.schemas import QueryRequest
from app.services.retrieve import HybridResult, RetrievedChunk
from app.services.tracing import (
    STAGE_DENSE,
    STAGE_RERANK,
    STAGE_RRF,
    STAGE_SPARSE,
    Stopwatch,
)

USER_ID = uuid.uuid4()
DENSE = [0.1]
SPARSE = object()  # opaque to the pipeline; only passed through


def _chunk(text: str, score: float = 0.0) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        page_from=1,
        page_to=1,
        content=text,
        score=score,
    )


def _run(body: QueryRequest, use_hybrid: bool, sparse=SPARSE):
    return asyncio.run(
        qr._retrieve_chunks(
            None, body, use_hybrid, USER_ID, DENSE, sparse, Stopwatch()
        )
    )


def test_dense_path_uses_search_and_skips_rerank(monkeypatch):
    called = {"search": 0, "hybrid": 0, "rerank": 0}
    hit = _chunk("dense hit", 0.9)

    async def fake_search(session, emb, *, user_id, document_id=None):
        called["search"] += 1
        assert user_id == USER_ID
        return [hit]

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        called["hybrid"] += 1
        return HybridResult([], [], [])

    async def fake_rerank(question, texts):
        called["rerank"] += 1
        return []

    monkeypatch.setattr(qr.retrieve, "search", fake_search)
    monkeypatch.setattr(qr.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qr.rerank, "rerank", fake_rerank)

    out = _run(QueryRequest(question="q", hybrid=False), use_hybrid=False)

    assert [c.content for c in out.chunks] == ["dense hit"]
    assert called == {"search": 1, "hybrid": 0, "rerank": 0}
    # dense-only path still traces what it retrieved
    assert out.stage_chunks[STAGE_DENSE] == [hit]


def test_missing_sparse_vector_falls_back_to_dense(monkeypatch):
    """Hybrid requested but no sparse vector must not call hybrid_search."""
    called = {"search": 0, "hybrid": 0}

    async def fake_search(session, emb, *, user_id, document_id=None):
        called["search"] += 1
        return [_chunk("dense hit")]

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        called["hybrid"] += 1
        return HybridResult([], [], [])

    monkeypatch.setattr(qr.retrieve, "search", fake_search)
    monkeypatch.setattr(qr.retrieve, "hybrid_search", fake_hybrid)

    out = _run(QueryRequest(question="q"), use_hybrid=True, sparse=None)

    assert [c.content for c in out.chunks] == ["dense hit"]
    assert called == {"search": 1, "hybrid": 0}


def test_hybrid_path_reranks_and_truncates(monkeypatch):
    candidates = [_chunk(f"c{i}") for i in range(5)]
    dense_ids = [c.chunk_id for c in candidates]
    sparse_ids = [candidates[0].chunk_id]

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        assert user_id == USER_ID
        return HybridResult(candidates, dense_ids, sparse_ids)

    async def fake_rerank(question, texts):
        # deliberately not in candidate order; the pipeline must honour it
        return [(3, 9.0), (1, 8.0), (4, 7.0), (0, 6.0), (2, 5.0)]

    monkeypatch.setattr(qr.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qr.rerank, "rerank", fake_rerank)
    monkeypatch.setattr(qr.settings, "rerank_top", 3)

    out = _run(QueryRequest(question="q", hybrid=True), use_hybrid=True)

    assert [c.content for c in out.chunks] == ["c3", "c1", "c4"]  # top-3 by reranker
    assert [c.score for c in out.chunks] == [9.0, 8.0, 7.0]  # reranker scores


def test_hybrid_path_records_every_stage(monkeypatch):
    """All four stages must be traceable, not just the final context."""
    candidates = [_chunk(f"c{i}") for i in range(4)]
    dense_ids = [c.chunk_id for c in candidates]
    sparse_ids = [candidates[2].chunk_id, candidates[0].chunk_id]

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        return HybridResult(candidates, dense_ids, sparse_ids)

    async def fake_rerank(question, texts):
        return [(1, 5.0), (0, 4.0), (2, 3.0), (3, 2.0)]

    monkeypatch.setattr(qr.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qr.rerank, "rerank", fake_rerank)
    monkeypatch.setattr(qr.settings, "rerank_top", 2)

    out = _run(QueryRequest(question="q", hybrid=True), use_hybrid=True)

    assert out.stage_ids[STAGE_DENSE] == dense_ids
    assert out.stage_ids[STAGE_SPARSE] == sparse_ids
    assert out.stage_chunks[STAGE_RRF] == candidates  # full candidate set
    assert [c.content for c in out.stage_chunks[STAGE_RERANK]] == ["c1", "c0"]
    # A chunk retrieved but dropped by reranking stays visible in earlier stages.
    dropped = candidates[3].chunk_id
    assert dropped in out.stage_ids[STAGE_DENSE]
    assert dropped not in [c.chunk_id for c in out.stage_chunks[STAGE_RERANK]]


def test_hybrid_path_no_candidates_skips_rerank(monkeypatch):
    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        return HybridResult([], [], [])

    async def fake_rerank(question, texts):
        raise AssertionError("rerank must not run with zero candidates")

    monkeypatch.setattr(qr.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qr.rerank, "rerank", fake_rerank)

    out = _run(QueryRequest(question="q", hybrid=True), use_hybrid=True)
    assert out.chunks == []
    assert out.stage_chunks == {}


def test_stopwatch_records_stage_latency():
    async def scenario():
        watch = Stopwatch()
        async with watch.time("embed"):
            await asyncio.sleep(0.01)
        async with watch.time("generate"):
            await asyncio.sleep(0.01)
        return watch

    watch = asyncio.run(scenario())
    assert watch.stages["embed"] >= 5
    assert watch.stages["generate"] >= 5
    assert watch.total_ms() >= watch.stages["embed"]
