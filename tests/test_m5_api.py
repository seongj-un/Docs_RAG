"""M5 frontend support endpoints: chunk text and usage. Requires Postgres.

Both routes exist because a screen needed something the API could not answer.
The evidence modal needs the chunk an answer was grounded in, and a citation
only carries a 240-character snippet; the settings screen needs consumption
against the quotas, and the counting existed with no way to read it.
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.models import Chunk, Document
from app.services import auth, generate, usage

EMBED_DIM = settings.embed_dim

# Comfortably past the 240-character snippet cut, so a truncated response is
# visibly different from a whole one.
LONG_CONTENT = "근거가 되는 문장. " * 40


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


async def _signup(client: AsyncClient) -> str:
    email = f"m5-{uuid.uuid4().hex[:8]}@example.com"
    await client.post("/auth/signup", json={"email": email, "password": "password123"})
    return email


async def _drop_user(email: str) -> None:
    async with SessionLocal() as session:
        user = await auth.get_user_by_email(session, email)
        if user is not None:
            await session.delete(user)  # documents, chunks, usage events cascade
            await session.commit()


async def _seed_chunk(email: str, content: str) -> uuid.UUID:
    """Give the user one ready document holding a single chunk."""
    async with SessionLocal() as session:
        user = await auth.get_user_by_email(session, email)
        doc = Document(
            user_id=user.id,
            filename="근거.pdf",
            mime_type="application/pdf",
            status="ready",
            num_pages=7,
        )
        session.add(doc)
        await session.commit()
        await session.refresh(doc)

        chunk = Chunk(
            document_id=doc.id,
            chunk_index=2,
            page_from=7,
            page_to=7,
            content=content,
            token_count=len(content.split()),
            embed_model="test",
            embedding=[0.1] * EMBED_DIM,
        )
        session.add(chunk)
        await session.commit()
        await session.refresh(chunk)
        return chunk.id


def test_chunk_endpoint_returns_untruncated_text():
    """The modal must show the whole chunk, not the citation's snippet."""

    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            email = await _signup(client)
            chunk_id = await _seed_chunk(email, LONG_CONTENT)
            response = await client.get(f"/chunks/{chunk_id}")
        await _drop_user(email)
        return response

    response = run_async(scenario)
    assert response.status_code == 200

    body = response.json()
    assert body["content"] == LONG_CONTENT
    assert body["page_from"] == 7
    assert body["chunk_index"] == 2

    # The point of the endpoint: a citation would have cut this short.
    snippet = generate._snippet(LONG_CONTENT)
    assert snippet != LONG_CONTENT
    assert snippet.endswith("…")


def test_chunk_endpoint_is_owner_scoped():
    """Someone else's chunk is 404 — never 403, which would confirm it exists."""

    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as alice:
            alice_email = await _signup(alice)
            chunk_id = await _seed_chunk(alice_email, LONG_CONTENT)

            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as bob:
                bob_email = await _signup(bob)
                foreign = await bob.get(f"/chunks/{chunk_id}")

            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as stranger:
                anonymous = await stranger.get(f"/chunks/{chunk_id}")

            missing = await alice.get(f"/chunks/{uuid.uuid4()}")

        await _drop_user(alice_email)
        await _drop_user(bob_email)
        return foreign.status_code, anonymous.status_code, missing.status_code

    foreign, anonymous, missing = run_async(scenario)
    assert foreign == 404
    assert anonymous == 401
    assert missing == 404


def test_usage_endpoint_reports_counts_against_limits():
    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            email = await _signup(client)

            async with SessionLocal() as session:
                user = await auth.get_user_by_email(session, email)
                for _ in range(3):
                    await usage.record(session, user.id, "query")
                await usage.record(session, user.id, "ingest", pages=12)
                await usage.record(session, user.id, "ingest", pages=5)

            response = await client.get("/usage")

            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as stranger:
                anonymous = await stranger.get("/usage")

        await _drop_user(email)
        return response, anonymous.status_code

    response, anonymous = run_async(scenario)
    assert response.status_code == 200
    assert anonymous == 401

    body = response.json()
    assert body["queries_today"] == 3
    assert body["pages_this_month"] == 17
    assert body["queries_per_day"] == settings.quota_queries_per_day
    assert body["pages_per_month"] == settings.quota_upload_pages_per_month
