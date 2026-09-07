"""Orchestration tests for the query retrieval pipeline (no infra/DB).

Embedding now happens once in the route (the dense vector is reused for the
semantic-cache probe), so ``_retrieve_chunks`` receives the vectors rather than
computing them.
"""

import asyncio
import uuid

from app.routers import query as qr
from app.schemas import QueryRequest
from app.services.retrieve import RetrievedChunk

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
        qr._retrieve_chunks(None, body, use_hybrid, USER_ID, DENSE, sparse)
    )


def test_dense_path_uses_search_and_skips_rerank(monkeypatch):
    called = {"search": 0, "hybrid": 0, "rerank": 0}

    async def fake_search(session, emb, *, user_id, document_id=None):
        called["search"] += 1
        assert user_id == USER_ID
        return [_chunk("dense hit", 0.9)]

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        called["hybrid"] += 1
        return []

    async def fake_rerank(question, texts):
        called["rerank"] += 1
        return []

    monkeypatch.setattr(qr.retrieve, "search", fake_search)
    monkeypatch.setattr(qr.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qr.rerank, "rerank", fake_rerank)

    out = _run(QueryRequest(question="q", hybrid=False), use_hybrid=False)

    assert [c.content for c in out] == ["dense hit"]
    assert called == {"search": 1, "hybrid": 0, "rerank": 0}


def test_missing_sparse_vector_falls_back_to_dense(monkeypatch):
    """Hybrid requested but no sparse vector must not call hybrid_search."""
    called = {"search": 0, "hybrid": 0}

    async def fake_search(session, emb, *, user_id, document_id=None):
        called["search"] += 1
        return [_chunk("dense hit")]

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        called["hybrid"] += 1
        return []

    monkeypatch.setattr(qr.retrieve, "search", fake_search)
    monkeypatch.setattr(qr.retrieve, "hybrid_search", fake_hybrid)

    out = _run(QueryRequest(question="q"), use_hybrid=True, sparse=None)

    assert [c.content for c in out] == ["dense hit"]
    assert called == {"search": 1, "hybrid": 0}


def test_hybrid_path_reranks_and_truncates(monkeypatch):
    candidates = [_chunk(f"c{i}") for i in range(5)]

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        assert user_id == USER_ID
        return candidates

    async def fake_rerank(question, texts):
        # deliberately not in candidate order; the pipeline must honour it
        return [(3, 9.0), (1, 8.0), (4, 7.0), (0, 6.0), (2, 5.0)]

    monkeypatch.setattr(qr.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qr.rerank, "rerank", fake_rerank)
    monkeypatch.setattr(qr.settings, "rerank_top", 3)

    out = _run(QueryRequest(question="q", hybrid=True), use_hybrid=True)

    assert [c.content for c in out] == ["c3", "c1", "c4"]  # top-3 by reranker
    assert [c.score for c in out] == [9.0, 8.0, 7.0]  # score replaced by reranker


def test_hybrid_path_no_candidates_skips_rerank(monkeypatch):
    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        return []

    async def fake_rerank(question, texts):
        raise AssertionError("rerank must not run with zero candidates")

    monkeypatch.setattr(qr.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qr.rerank, "rerank", fake_rerank)

    assert _run(QueryRequest(question="q", hybrid=True), use_hybrid=True) == []
