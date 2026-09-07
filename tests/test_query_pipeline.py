"""Retrieval orchestration tests for the shared pipeline (no infra/DB).

The runner is shared by ``POST /query`` and the streaming conversation
endpoint, so these cover both. Stage records are asserted too: they are what
make retrieval-vs-generation failures separable later, so a silently empty
stage would be a real defect.
"""

import asyncio
import uuid

import pytest

from app.models import User
from app.services import pipeline
from app.services.retrieve import HybridResult, RetrievedChunk
from app.services.tracing import (
    STAGE_DENSE,
    STAGE_RERANK,
    STAGE_RRF,
    STAGE_SPARSE,
    Stopwatch,
)

USER = User(id=uuid.uuid4(), email="u@example.com", password_hash="x")
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


def _runner(hybrid: bool, sparse=SPARSE) -> pipeline.QueryRunner:
    runner = pipeline.QueryRunner(
        None, USER, "질문", document_id=None, hybrid=hybrid
    )
    runner.dense = DENSE
    runner.sparse = sparse
    return runner


def test_dense_path_uses_search_and_skips_rerank(monkeypatch):
    called = {"search": 0, "hybrid": 0, "rerank": 0}
    hit = _chunk("dense hit", 0.9)

    async def fake_search(session, emb, *, user_id, document_id=None):
        called["search"] += 1
        assert user_id == USER.id
        return [hit]

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        called["hybrid"] += 1
        return HybridResult([], [], [])

    async def fake_rerank(question, texts):
        called["rerank"] += 1
        return []

    monkeypatch.setattr(pipeline.retrieve, "search", fake_search)
    monkeypatch.setattr(pipeline.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(pipeline.rerank, "rerank", fake_rerank)

    found = asyncio.run(_runner(hybrid=False).retrieve())

    assert [c.content for c in found.chunks] == ["dense hit"]
    assert called == {"search": 1, "hybrid": 0, "rerank": 0}
    assert found.stage_chunks[STAGE_DENSE] == [hit]


def test_missing_sparse_vector_falls_back_to_dense(monkeypatch):
    """Hybrid requested but no sparse vector must not call hybrid_search."""
    called = {"search": 0, "hybrid": 0}

    async def fake_search(session, emb, *, user_id, document_id=None):
        called["search"] += 1
        return [_chunk("dense hit")]

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        called["hybrid"] += 1
        return HybridResult([], [], [])

    monkeypatch.setattr(pipeline.retrieve, "search", fake_search)
    monkeypatch.setattr(pipeline.retrieve, "hybrid_search", fake_hybrid)

    found = asyncio.run(_runner(hybrid=True, sparse=None).retrieve())

    assert [c.content for c in found.chunks] == ["dense hit"]
    assert called == {"search": 1, "hybrid": 0}


def test_hybrid_path_reranks_and_truncates(monkeypatch):
    candidates = [_chunk(f"c{i}") for i in range(5)]

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        assert user_id == USER.id
        return HybridResult(candidates, [c.chunk_id for c in candidates], [])

    async def fake_rerank(question, texts):
        # deliberately not in candidate order; the pipeline must honour it
        return [(3, 9.0), (1, 8.0), (4, 7.0), (0, 6.0), (2, 5.0)]

    monkeypatch.setattr(pipeline.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(pipeline.rerank, "rerank", fake_rerank)
    monkeypatch.setattr(pipeline.settings, "rerank_top", 3)

    found = asyncio.run(_runner(hybrid=True).retrieve())

    assert [c.content for c in found.chunks] == ["c3", "c1", "c4"]
    assert [c.score for c in found.chunks] == [9.0, 8.0, 7.0]


def test_hybrid_path_records_every_stage(monkeypatch):
    """All four stages must be traceable, not just the final context."""
    candidates = [_chunk(f"c{i}") for i in range(4)]
    dense_ids = [c.chunk_id for c in candidates]
    sparse_ids = [candidates[2].chunk_id, candidates[0].chunk_id]

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        return HybridResult(candidates, dense_ids, sparse_ids)

    async def fake_rerank(question, texts):
        return [(1, 5.0), (0, 4.0), (2, 3.0), (3, 2.0)]

    monkeypatch.setattr(pipeline.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(pipeline.rerank, "rerank", fake_rerank)
    monkeypatch.setattr(pipeline.settings, "rerank_top", 2)

    found = asyncio.run(_runner(hybrid=True).retrieve())

    assert found.stage_ids[STAGE_DENSE] == dense_ids
    assert found.stage_ids[STAGE_SPARSE] == sparse_ids
    assert found.stage_chunks[STAGE_RRF] == candidates
    assert [c.content for c in found.stage_chunks[STAGE_RERANK]] == ["c1", "c0"]
    # A chunk retrieved but dropped by reranking stays visible upstream.
    dropped = candidates[3].chunk_id
    assert dropped in found.stage_ids[STAGE_DENSE]
    assert dropped not in [c.chunk_id for c in found.stage_chunks[STAGE_RERANK]]


def test_hybrid_path_no_candidates_skips_rerank(monkeypatch):
    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        return HybridResult([], [], [])

    async def fake_rerank(question, texts):
        raise AssertionError("rerank must not run with zero candidates")

    monkeypatch.setattr(pipeline.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(pipeline.rerank, "rerank", fake_rerank)

    found = asyncio.run(_runner(hybrid=True).retrieve())
    assert found.chunks == []
    assert found.stage_chunks == {}


def test_grounding_floor_differs_by_path():
    """Cosine and reranker sigmoid are different quantities."""
    assert _runner(hybrid=True).grounding_floor == pipeline.settings.rerank_min_score
    assert _runner(hybrid=False).grounding_floor is None


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
