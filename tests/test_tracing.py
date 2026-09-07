"""M4 Phase 1 proofs: every query leaves a trace, down to the chunk ids.

Requires Postgres. External services (embeddings, rerank, LLM) are stubbed —
the point here is what reaches the database, not what the models say.
``trace_chunks.chunk_id`` has no foreign key by design, so synthetic chunk ids
are valid trace content.
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.models import Document, Trace, User
from app.services import auth, tracing
from app.services.generate import Answer
from app.services.ratelimit import query_limiter
from app.services.retrieve import HybridResult, RetrievedChunk
from app.services.tracing import (
    STAGE_DENSE,
    STAGE_RERANK,
    STAGE_RRF,
    STAGE_SPARSE,
    Stopwatch,
    TraceDraft,
)

EMBED_DIM = settings.embed_dim


def run_async(coro_fn):
    async def wrapper():
        try:
            return await coro_fn()
        finally:
            await engine.dispose()

    return asyncio.run(wrapper())


def _db_available() -> bool:
    async def check():
        try:
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.run(check())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


@pytest.fixture(autouse=True)
def _clean_limiter():
    query_limiter.reset()
    yield
    query_limiter.reset()


def _chunk(text_: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        page_from=3,
        page_to=3,
        content=text_,
        score=0.0,
    )


async def _drop_user(email: str) -> None:
    async with SessionLocal() as session:
        user = await auth.get_user_by_email(session, email)
        if user is not None:
            await session.delete(user)
            await session.commit()


def _stub_pipeline(monkeypatch, candidates, ranked):
    from app.routers import query as rq
    from app.services import pipeline as qr

    async def fake_embed_full(question):
        return [0.02] * EMBED_DIM, object()

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        return HybridResult(
            candidates,
            [c.chunk_id for c in candidates],
            [candidates[-1].chunk_id],
        )

    async def fake_rerank(question, texts):
        return ranked

    async def fake_answer(question, chunks, **kwargs):
        return Answer(
            answer="테스트 답변 [p.3].",
            refused=False,
            citations=[],
            tokens_in=123,
            tokens_out=45,
        )

    monkeypatch.setattr(qr.embeddings, "embed_query_full", fake_embed_full)
    monkeypatch.setattr(qr.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qr.rerank, "rerank", fake_rerank)
    monkeypatch.setattr(rq.generate, "answer_question", fake_answer)


def test_query_records_trace_with_every_stage(monkeypatch):
    candidates = [_chunk(f"c{i}") for i in range(3)]
    _stub_pipeline(monkeypatch, candidates, ranked=[(1, 5.0), (0, 4.0), (2, 3.0)])
    monkeypatch.setattr(settings, "rerank_top", 2)

    async def scenario():
        email = f"trace-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            resp = await client.post("/query", json={"question": "관리비 얼마?"})
            status = resp.status_code

        async with SessionLocal() as session:
            user = await auth.get_user_by_email(session, email)
            row = (
                await session.execute(
                    select(Trace)
                    .options(selectinload(Trace.chunks))
                    .where(Trace.user_id == user.id)
                )
            ).scalars().first()
            snapshot = {
                "question": row.question,
                "hybrid": row.hybrid,
                "cached": row.cached,
                "refused": row.refused,
                "answer": row.answer,
                "model": row.llm_model,
                "tokens": (row.tokens_in, row.tokens_out),
                "has_total": row.total_ms is not None,
                "has_embed": row.embed_ms is not None,
                "stages": {
                    stage: [(c.chunk_id, c.rank) for c in row.chunks if c.stage == stage]
                    for stage in (STAGE_DENSE, STAGE_SPARSE, STAGE_RRF, STAGE_RERANK)
                },
            }
        await _drop_user(email)
        return status, snapshot, candidates

    status, snap, candidates = run_async(scenario)

    assert status == 200
    assert snap["question"] == "관리비 얼마?"
    assert snap["hybrid"] is True and snap["cached"] is False
    assert snap["answer"] == "테스트 답변 [p.3]."
    assert snap["tokens"] == (123, 45)
    assert snap["model"] == settings.llm_model
    assert snap["has_total"] and snap["has_embed"]

    # All four stages recorded, in rank order.
    assert [cid for cid, _ in snap["stages"][STAGE_DENSE]] == [
        c.chunk_id for c in candidates
    ]
    assert len(snap["stages"][STAGE_SPARSE]) == 1
    assert len(snap["stages"][STAGE_RRF]) == 3
    # rerank kept top-2 in reranker order (c1, c0)
    assert [cid for cid, _ in snap["stages"][STAGE_RERANK]] == [
        candidates[1].chunk_id,
        candidates[0].chunk_id,
    ]
    # The chunk reranking dropped is still visible upstream — this is what makes
    # "retrieved but discarded" distinguishable from "never retrieved".
    dropped = candidates[2].chunk_id
    assert dropped in [cid for cid, _ in snap["stages"][STAGE_DENSE]]
    assert dropped not in [cid for cid, _ in snap["stages"][STAGE_RERANK]]


def test_trace_api_is_owner_scoped(monkeypatch):
    candidates = [_chunk("c0")]
    _stub_pipeline(monkeypatch, candidates, ranked=[(0, 1.0)])

    async def scenario():
        alice_email = f"a-{uuid.uuid4().hex[:8]}@example.com"
        bob_email = f"b-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as alice:
            await alice.post(
                "/auth/signup", json={"email": alice_email, "password": "password123"}
            )
            await alice.post("/query", json={"question": "앨리스 질문"})
            listed = await alice.get("/traces")
            trace_id = listed.json()[0]["id"]
            detail = await alice.get(f"/traces/{trace_id}")

        async with AsyncClient(transport=transport, base_url="http://test") as bob:
            await bob.post(
                "/auth/signup", json={"email": bob_email, "password": "password123"}
            )
            bob_list = await bob.get("/traces")
            bob_peek = await bob.get(f"/traces/{trace_id}")

        out = {
            "alice_count": len(listed.json()),
            "alice_detail": detail.status_code,
            "alice_chunks": len(detail.json()["chunks"]),
            "bob_count": len(bob_list.json()),
            "bob_peek": bob_peek.status_code,
        }
        await _drop_user(alice_email)
        await _drop_user(bob_email)
        return out

    r = run_async(scenario)

    assert r["alice_count"] == 1
    assert r["alice_detail"] == 200
    assert r["alice_chunks"] > 0
    assert r["bob_count"] == 0  # sees none of Alice's traces
    assert r["bob_peek"] == 404  # 404, not 403


def test_trace_recording_failure_does_not_raise():
    """Observability must never take down the thing it observes."""

    async def scenario():
        async with SessionLocal() as session:
            # user_id violates the FK -> the insert fails inside record()
            draft = TraceDraft(user_id=uuid.uuid4(), question="q")
            return await tracing.record(session, draft, Stopwatch())

    assert run_async(scenario) is None  # swallowed, not raised


def test_unknown_or_foreign_document_scope_is_404(monkeypatch):
    """An unowned scope must be rejected before the pipeline runs.

    Regression guard: the semantic cache has an FK on document_id, so an
    unvalidated scope used to surface as a 500 from a foreign-key violation.
    """
    candidates = [_chunk("c0")]
    _stub_pipeline(monkeypatch, candidates, ranked=[(0, 1.0)])

    async def scenario():
        email = f"scope-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            resp = await client.post(
                "/query", json={"question": "q", "document_id": str(uuid.uuid4())}
            )
            traces = await client.get("/traces")
            out = (resp.status_code, len(traces.json()))
        await _drop_user(email)
        return out

    status, trace_count = run_async(scenario)
    assert status == 404
    assert trace_count == 0  # rejected before any work was done


def test_trace_survives_deletion_of_its_document(monkeypatch):
    """A trace is history: deleting the document it referenced must not erase it."""
    candidates = [_chunk("c0")]
    _stub_pipeline(monkeypatch, candidates, ranked=[(0, 1.0)])

    async def scenario():
        email = f"hist-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            user_id = uuid.UUID((await client.get("/auth/me")).json()["id"])

            async with SessionLocal() as session:
                doc = Document(
                    user_id=user_id,
                    filename="temp.pdf",
                    mime_type="application/pdf",
                    status="ready",
                    num_pages=1,
                )
                session.add(doc)
                await session.commit()
                await session.refresh(doc)
                doc_id = doc.id

            await client.post(
                "/query", json={"question": "q", "document_id": str(doc_id)}
            )
            before = len((await client.get("/traces")).json())

            deleted = await client.delete(f"/documents/{doc_id}")
            after = (await client.get("/traces")).json()

        out = (before, deleted.status_code, len(after), after[0]["document_id"] if after else None)
        await _drop_user(email)
        return out, str(doc_id)

    (before, delete_status, after_count, recorded_doc), doc_id = run_async(scenario)

    assert before == 1
    assert delete_status == 204
    assert after_count == 1, "the trace must outlive the document it referenced"
    assert recorded_doc == doc_id  # id retained even though the row is gone
