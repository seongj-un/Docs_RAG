"""Tenant isolation proofs (M3 완료기준 1-3). Requires Postgres.

These are integration tests on purpose: isolation is enforced by SQL joins and
FastAPI dependencies, so mocking the database would prove nothing. They skip
when Postgres is unreachable so the unit suite still runs without infra.

Each test runs in a single ``asyncio.run`` and disposes the engine afterwards —
asyncpg connections are bound to the loop that created them, so a pooled
connection must not leak into the next test's loop.
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.models import Chunk, Document, User
from app.services import auth, ingest, retrieve

EMBED_DIM = settings.embed_dim


def run_async(coro_fn):
    """Run one coroutine, always disposing the engine afterwards."""

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


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="Postgres not reachable"
)


async def _make_user(session, label: str) -> User:
    user = User(
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=auth.hash_password("password123"),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _make_doc(session, user: User, filename: str, content: str) -> Document:
    doc = Document(
        user_id=user.id,
        filename=filename,
        mime_type="application/pdf",
        status="ready",
        num_pages=1,
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    session.add(
        Chunk(
            document_id=doc.id,
            chunk_index=0,
            page_from=1,
            page_to=1,
            content=content,
            token_count=5,
            embed_model="test",
            embedding=[0.1] * EMBED_DIM,
        )
    )
    await session.commit()
    return doc


async def _cleanup(session, *users: User) -> None:
    for user in users:
        if user is None:
            continue
        fresh = await session.get(User, user.id)
        if fresh is not None:
            await session.delete(fresh)  # documents/chunks cascade
    await session.commit()


def test_search_never_returns_another_users_chunks():
    async def scenario():
        async with SessionLocal() as session:
            alice = await _make_user(session, "alice")
            bob = await _make_user(session, "bob")
            await _make_doc(session, alice, "alice.pdf", "ALICE SECRET CONTENT")
            await _make_doc(session, bob, "bob.pdf", "BOB SECRET CONTENT")

            query = [0.1] * EMBED_DIM
            alice_hits = await retrieve.search(session, query, user_id=alice.id)
            bob_hits = await retrieve.search(session, query, user_id=bob.id)

            alice_text = [h.content for h in alice_hits]
            bob_text = [h.content for h in bob_hits]

            await _cleanup(session, alice, bob)
            return alice_text, bob_text

    alice_text, bob_text = run_async(scenario)

    assert alice_text == ["ALICE SECRET CONTENT"]
    assert bob_text == ["BOB SECRET CONTENT"]
    assert "BOB SECRET CONTENT" not in alice_text
    assert "ALICE SECRET CONTENT" not in bob_text


def test_document_id_filter_cannot_reach_another_users_document():
    """Passing someone else's document_id must not widen access."""

    async def scenario():
        async with SessionLocal() as session:
            alice = await _make_user(session, "alice")
            bob = await _make_user(session, "bob")
            bob_doc = await _make_doc(session, bob, "bob.pdf", "BOB SECRET CONTENT")
            await _make_doc(session, alice, "alice.pdf", "ALICE SECRET CONTENT")

            hits = await retrieve.search(
                session, [0.1] * EMBED_DIM, user_id=alice.id, document_id=bob_doc.id
            )
            texts = [h.content for h in hits]

            await _cleanup(session, alice, bob)
            return texts

    assert run_async(scenario) == []


def test_get_and_list_documents_are_owner_scoped():
    async def scenario():
        async with SessionLocal() as session:
            alice = await _make_user(session, "alice")
            bob = await _make_user(session, "bob")
            bob_doc = await _make_doc(session, bob, "bob.pdf", "BOB")
            alice_doc = await _make_doc(session, alice, "alice.pdf", "ALICE")

            cross = await ingest.get_document(session, bob_doc.id, user_id=alice.id)
            own = await ingest.get_document(session, alice_doc.id, user_id=alice.id)
            listed = await ingest.list_documents(session, user_id=alice.id)
            names = [d.filename for d in listed]

            await _cleanup(session, alice, bob)
            return cross, own is not None, names

    cross, own_found, names = run_async(scenario)

    assert cross is None  # someone else's document is invisible
    assert own_found
    assert names == ["alice.pdf"]


def test_search_requires_user_id():
    """Omitting user_id must fail loudly, not silently search everything."""
    with pytest.raises(TypeError):
        asyncio.run(retrieve.search(None, [0.1] * EMBED_DIM))  # type: ignore[call-arg]


def test_api_rejects_unauthenticated_requests():
    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return {
                "list": (await client.get("/documents")).status_code,
                "get": (await client.get(f"/documents/{uuid.uuid4()}")).status_code,
                "delete": (await client.delete(f"/documents/{uuid.uuid4()}")).status_code,
                "query": (await client.post("/query", json={"question": "hi"})).status_code,
                "me": (await client.get("/auth/me")).status_code,
                "health": (await client.get("/health")).status_code,
            }

    codes = run_async(scenario)

    assert codes["list"] == 401
    assert codes["get"] == 401
    assert codes["delete"] == 401
    assert codes["query"] == 401
    assert codes["me"] == 401
    assert codes["health"] == 200  # health stays public


def test_api_hides_another_users_document_as_404():
    async def scenario():
        async with SessionLocal() as session:
            bob = await _make_user(session, "bob")
            bob_doc = await _make_doc(session, bob, "bob.pdf", "BOB SECRET")
            bob_doc_id = bob_doc.id

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            email = f"alice-{uuid.uuid4().hex[:8]}@example.com"
            signup = await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            me = await client.get("/auth/me")
            get_other = await client.get(f"/documents/{bob_doc_id}")
            del_other = await client.delete(f"/documents/{bob_doc_id}")
            listed = await client.get("/documents")
            alice_email = me.json()["email"]

        async with SessionLocal() as session:
            still_there = await session.get(Document, bob_doc_id)
            bob_doc_survived = still_there is not None
            alice = await auth.get_user_by_email(session, email)
            await _cleanup(session, alice, bob)

        return {
            "signup": signup.status_code,
            "me_email": alice_email,
            "get": get_other.status_code,
            "delete": del_other.status_code,
            "list": listed.json(),
            "survived": bob_doc_survived,
        }

    r = run_async(scenario)

    assert r["signup"] == 201
    assert r["me_email"].startswith("alice-")
    assert r["get"] == 404  # 404, not 403 — existence is hidden
    assert r["delete"] == 404
    assert r["list"] == []  # sees none of Bob's documents
    assert r["survived"], "another user's document must not be deletable"


def test_login_logout_session_lifecycle():
    async def scenario():
        email = f"carol-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            after_signup = (await client.get("/auth/me")).status_code
            await client.post("/auth/logout")
            after_logout = (await client.get("/auth/me")).status_code

            bad = await client.post(
                "/auth/login", json={"email": email, "password": "wrong-password"}
            )
            unknown = await client.post(
                "/auth/login",
                json={"email": "nobody@example.com", "password": "password123"},
            )
            good = await client.post(
                "/auth/login", json={"email": email, "password": "password123"}
            )
            after_login = (await client.get("/auth/me")).status_code

        async with SessionLocal() as session:
            user = await auth.get_user_by_email(session, email)
            await _cleanup(session, user)

        return {
            "after_signup": after_signup,
            "after_logout": after_logout,
            "bad": bad.status_code,
            "unknown": unknown.status_code,
            "good": good.status_code,
            "after_login": after_login,
        }

    r = run_async(scenario)

    assert r["after_signup"] == 200
    assert r["after_logout"] == 401  # logout revokes the session server-side
    assert r["bad"] == 401
    assert r["unknown"] == 401  # same status as wrong password: no user enumeration
    assert r["good"] == 200
    assert r["after_login"] == 200


def test_seed_account_cannot_be_logged_into():
    """Pre-M3 documents were preserved under a seed user that must stay unusable.

    Two independent barriers: the stored hash is a sentinel that can never
    verify, and the seed address sits on a special-use domain the request
    schema refuses outright (422). Either way authentication never succeeds.
    """
    from app.constants import SEED_USER_EMAIL

    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            attempts = []
            for password in ("!unusable", "password123", "x"):
                resp = await client.post(
                    "/auth/login",
                    json={"email": SEED_USER_EMAIL, "password": password},
                )
                attempts.append(resp.status_code)
            return attempts

    codes = run_async(scenario)
    assert all(code != 200 for code in codes), codes
    assert all(code in (401, 422) for code in codes), codes
