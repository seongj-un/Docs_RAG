"""Orchestration tests for the query retrieval pipeline (no infra/DB)."""

import asyncio
import uuid

from app.routers import query as qr
from app.schemas import QueryRequest
from app.services.retrieve import RetrievedChunk


def _chunk(text: str, score: float = 0.0) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        page_from=1,
        page_to=1,
        content=text,
        score=score,
    )


def test_dense_path_skips_sparse_and_rerank(monkeypatch):
    called = {"embed_query": 0, "search": 0, "rerank": 0, "embed_full": 0}

    async def fake_embed_query(text):
        called["embed_query"] += 1
        return [0.1]

    async def fake_search(session, emb, document_id=None):
        called["search"] += 1
        return [_chunk("dense hit", 0.9)]

    async def fake_embed_full(text):
        called["embed_full"] += 1
        return [0.1], object()

    async def fake_rerank(q, texts):
        called["rerank"] += 1
        return []

    monkeypatch.setattr(qr.embeddings, "embed_query", fake_embed_query)
    monkeypatch.setattr(qr.retrieve, "search", fake_search)
    monkeypatch.setattr(qr.embeddings, "embed_query_full", fake_embed_full)
    monkeypatch.setattr(qr.rerank, "rerank", fake_rerank)

    body = QueryRequest(question="q", hybrid=False)
    out = asyncio.run(qr._retrieve_chunks(None, body, use_hybrid=False))

    assert [c.content for c in out] == ["dense hit"]
    assert called == {"embed_query": 1, "search": 1, "rerank": 0, "embed_full": 0}


def test_hybrid_path_reranks_and_truncates(monkeypatch):
    candidates = [_chunk(f"c{i}") for i in range(5)]

    async def fake_embed_full(text):
        return [0.1], object()

    async def fake_hybrid(session, dense, sparse, document_id=None):
        return candidates

    # reranker returns worst-first on purpose; pipeline must sort/truncate.
    async def fake_rerank(q, texts):
        # give c3 the top score, then c1, c4, c0, c2
        order = [(3, 9.0), (1, 8.0), (4, 7.0), (0, 6.0), (2, 5.0)]
        return order

    monkeypatch.setattr(qr.embeddings, "embed_query_full", fake_embed_full)
    monkeypatch.setattr(qr.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qr.rerank, "rerank", fake_rerank)
    monkeypatch.setattr(qr.settings, "rerank_top", 3)

    body = QueryRequest(question="q", hybrid=True)
    out = asyncio.run(qr._retrieve_chunks(None, body, use_hybrid=True))

    assert [c.content for c in out] == ["c3", "c1", "c4"]  # top-3 by reranker
    assert [c.score for c in out] == [9.0, 8.0, 7.0]  # score replaced by reranker


def test_hybrid_path_no_candidates_returns_empty(monkeypatch):
    async def fake_embed_full(text):
        return [0.1], object()

    async def fake_hybrid(session, dense, sparse, document_id=None):
        return []

    async def fake_rerank(q, texts):
        raise AssertionError("rerank must not run with zero candidates")

    monkeypatch.setattr(qr.embeddings, "embed_query_full", fake_embed_full)
    monkeypatch.setattr(qr.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qr.rerank, "rerank", fake_rerank)

    body = QueryRequest(question="q", hybrid=True)
    out = asyncio.run(qr._retrieve_chunks(None, body, use_hybrid=True))
    assert out == []
