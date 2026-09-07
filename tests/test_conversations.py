"""M5 backend: conversation threads and SSE streaming. Requires Postgres.

External services are stubbed — what matters here is the thread persistence,
the event protocol, and that ownership rules survive the move to streaming
(where a 403/404 can no longer be an HTTP status, because the response is
already 200 by the time anything goes wrong).
"""

import asyncio
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.models import Conversation, Document
from app.services import auth, llm
from app.services.ratelimit import query_limiter
from app.services.retrieve import HybridResult, RetrievedChunk

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


def _chunk(content: str, score: float = 0.9) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        page_from=3,
        page_to=3,
        content=content,
        score=score,
    )


def _stub(monkeypatch, *, chunks=None, tokens=("답변", "입니다 [p.3].")):
    """Stub retrieval and streaming generation."""
    from app.services import pipeline as pl

    chunks = [_chunk("근거 청크")] if chunks is None else chunks

    async def fake_embed_full(question):
        return [0.02] * EMBED_DIM, object()

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        return HybridResult(chunks, [c.chunk_id for c in chunks], [])

    async def fake_rerank(question, texts):
        return [(i, chunks[i].score) for i in range(len(chunks))]

    async def fake_stream(system_prompt, user_prompt, *, model=None):
        for piece in tokens:
            yield piece
        yield llm.Generation(text="".join(tokens), tokens_in=11, tokens_out=7)

    monkeypatch.setattr(pl.embeddings, "embed_query_full", fake_embed_full)
    monkeypatch.setattr(pl.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(pl.rerank, "rerank", fake_rerank)
    monkeypatch.setattr(llm, "generate_stream", fake_stream)


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, data) pairs."""
    events = []
    for frame in body.strip().split("\n\n"):
        if not frame.strip():
            continue
        name, payload = None, None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
        if name:
            events.append((name, payload))
    return events


async def _signup(client: AsyncClient) -> str:
    email = f"conv-{uuid.uuid4().hex[:8]}@example.com"
    await client.post("/auth/signup", json={"email": email, "password": "password123"})
    return email


async def _drop_user(email: str) -> None:
    async with SessionLocal() as session:
        user = await auth.get_user_by_email(session, email)
        if user is not None:
            await session.delete(user)
            await session.commit()


def test_conversation_crud_and_ownership(monkeypatch):
    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as alice:
            alice_email = await _signup(alice)
            created = await alice.post("/conversations", json={"title": "첫 대화"})
            conv_id = created.json()["id"]
            listed = await alice.get("/conversations")
            detail = await alice.get(f"/conversations/{conv_id}")

            async with AsyncClient(transport=transport, base_url="http://test") as bob:
                bob_email = await _signup(bob)
                bob_list = await bob.get("/conversations")
                bob_get = await bob.get(f"/conversations/{conv_id}")
                bob_delete = await bob.delete(f"/conversations/{conv_id}")

            still_there = await alice.get(f"/conversations/{conv_id}")
            removed = await alice.delete(f"/conversations/{conv_id}")
            after = await alice.get(f"/conversations/{conv_id}")

        out = {
            "created": created.status_code,
            "title": created.json()["title"],
            "listed": len(listed.json()),
            "detail_messages": detail.json()["messages"],
            "bob_list": len(bob_list.json()),
            "bob_get": bob_get.status_code,
            "bob_delete": bob_delete.status_code,
            "survived": still_there.status_code,
            "removed": removed.status_code,
            "after": after.status_code,
        }
        await _drop_user(alice_email)
        await _drop_user(bob_email)
        return out

    r = run_async(scenario)
    assert r["created"] == 201 and r["title"] == "첫 대화"
    assert r["listed"] == 1
    assert r["detail_messages"] == []
    assert r["bob_list"] == 0  # sees none of Alice's threads
    assert r["bob_get"] == 404 and r["bob_delete"] == 404  # 404, not 403
    assert r["survived"] == 200, "another user must not be able to delete the thread"
    assert r["removed"] == 204 and r["after"] == 404


def test_streaming_turn_emits_protocol_and_persists_messages(monkeypatch):
    _stub(monkeypatch)

    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            email = await _signup(client)
            conv_id = (await client.post("/conversations", json={})).json()["id"]
            resp = await client.post(
                f"/conversations/{conv_id}/query",
                json={"question": "관리비는 얼마인가요?"},
            )
            events = parse_sse(resp.text)
            detail = (await client.get(f"/conversations/{conv_id}")).json()
        out = (resp.status_code, resp.headers.get("content-type"), events, detail)
        await _drop_user(email)
        return out

    status, content_type, events, detail = run_async(scenario)

    assert status == 200
    assert "text/event-stream" in content_type

    names = [name for name, _ in events]
    assert names[0] == "meta"
    assert names[-1] == "done"
    assert "token" in names

    streamed = "".join(d["text"] for n, d in events if n == "token")
    done = dict(events)["done"]
    assert streamed == done["answer"]  # streamed text matches the final answer
    assert done["refused"] is False
    assert done["citations"], "a grounded answer must carry citations"

    # The turn is persisted as two messages, question first.
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "관리비는 얼마인가요?"
    assert detail["messages"][1]["content"] == done["answer"]
    assert detail["messages"][1]["citations"]
    # First question becomes the thread title.
    assert detail["title"] == "관리비는 얼마인가요?"


def test_refusal_streams_without_citations(monkeypatch):
    # Score below the reranker floor -> refused before generation runs.
    _stub(monkeypatch, chunks=[_chunk("무관한 청크", score=0.0)])

    async def exploding_stream(*args, **kwargs):
        raise AssertionError("generation must not run when nothing is grounded")
        yield  # pragma: no cover

    monkeypatch.setattr(llm, "generate_stream", exploding_stream)

    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            email = await _signup(client)
            conv_id = (await client.post("/conversations", json={})).json()["id"]
            resp = await client.post(
                f"/conversations/{conv_id}/query", json={"question": "없는 내용"}
            )
            events = parse_sse(resp.text)
            detail = (await client.get(f"/conversations/{conv_id}")).json()
        await _drop_user(email)
        return events, detail

    events, detail = run_async(scenario)
    done = dict(events)["done"]
    assert done["refused"] is True
    assert done["citations"] == []  # a refusal must not cite anything
    assert detail["messages"][1]["refused"] is True


def test_stream_reports_errors_in_band(monkeypatch):
    """Once streaming starts the status is 200, so failures ride an event."""
    _stub(monkeypatch)

    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            email = await _signup(client)
            # A conversation the caller does not own (does not exist at all).
            resp = await client.post(
                f"/conversations/{uuid.uuid4()}/query", json={"question": "안녕"}
            )
            events = parse_sse(resp.text)
        await _drop_user(email)
        return resp.status_code, events

    status, events = run_async(scenario)
    assert status == 200  # the stream opened before the failure was known
    assert events and events[-1][0] == "error"
    assert events[-1][1]["status"] == 404


def test_unauthenticated_conversation_access_is_401():
    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return {
                "list": (await client.get("/conversations")).status_code,
                "create": (await client.post("/conversations", json={})).status_code,
                "get": (
                    await client.get(f"/conversations/{uuid.uuid4()}")
                ).status_code,
                "query": (
                    await client.post(
                        f"/conversations/{uuid.uuid4()}/query",
                        json={"question": "안녕"},
                    )
                ).status_code,
            }

    codes = run_async(scenario)
    assert all(code == 401 for code in codes.values()), codes


def test_conversation_scope_must_be_owned():
    async def scenario():
        async with SessionLocal() as session:
            other = await auth.create_user(
                session, f"other-{uuid.uuid4().hex[:8]}@example.com", "password123"
            )
            doc = Document(
                user_id=other.id,
                filename="other.pdf",
                mime_type="application/pdf",
                status="ready",
                num_pages=1,
            )
            session.add(doc)
            await session.commit()
            await session.refresh(doc)
            foreign_doc_id, other_email = doc.id, other.email

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            email = await _signup(client)
            resp = await client.post(
                "/conversations", json={"scope_document_id": str(foreign_doc_id)}
            )
            out = resp.status_code
        await _drop_user(email)
        await _drop_user(other_email)
        return out

    assert run_async(scenario) == 404


def test_title_is_trimmed_on_a_word_boundary():
    from app.routers.conversations import _title_from

    short = "관리비는 얼마인가요?"
    assert _title_from(short) == short

    long = "가 " * 60
    title = _title_from(long)
    assert len(title) <= 61 and title.endswith("…")
    assert not title.rstrip("…").endswith(" ")


def test_explicit_null_scope_means_every_document(monkeypatch):
    """`document_id: null` is a choice, not an omission.

    M5 requires switching a thread's scope between one document and the whole
    corpus. Reading None as "unset" would make that one-way: a thread created
    against a document could never ask across everything again.
    """
    _stub(monkeypatch)

    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            email = await _signup(client)

            async with SessionLocal() as session:
                user = await auth.get_user_by_email(session, email)
                doc = Document(
                    user_id=user.id,
                    filename="범위.pdf",
                    mime_type="application/pdf",
                    status="ready",
                    num_pages=1,
                )
                session.add(doc)
                await session.commit()
                await session.refresh(doc)
                doc_id = str(doc.id)

            conv_id = (
                await client.post(
                    "/conversations", json={"scope_document_id": doc_id}
                )
            ).json()["id"]

            # Field absent -> fall back to the thread's own scope.
            inherited = parse_sse(
                (
                    await client.post(
                        f"/conversations/{conv_id}/query",
                        json={"question": "이 문서만"},
                    )
                ).text
            )

            # Field present and null -> every document.
            widened = parse_sse(
                (
                    await client.post(
                        f"/conversations/{conv_id}/query",
                        json={"question": "전체에서", "document_id": None},
                    )
                ).text
            )

        await _drop_user(email)
        return doc_id, dict(inherited)["meta"], dict(widened)["meta"]

    doc_id, inherited, widened = run_async(scenario)
    assert inherited["scope"] == doc_id
    assert widened["scope"] is None
