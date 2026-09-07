"""The operational stats endpoint: access rules and how it counts.

The access tests need no infrastructure. The counting tests do — the numbers
come out of SQL aggregates over ``traces``, so mocking the database would
prove nothing about the thing under test.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.models import Trace, User
from app.services import auth

TOKEN = "test-admin-token"


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _get(headers: dict | None = None, params: str = "") -> int:
    async def call():
        try:
            async with _client() as c:
                r = await c.get(f"/admin/stats{params}", headers=headers or {})
                return r.status_code
        finally:
            await engine.dispose()

    return asyncio.run(call())


@pytest.fixture(autouse=True)
def restore_token():
    original = settings.admin_token
    yield
    settings.admin_token = original


def test_disabled_by_default_returns_404_not_401():
    """An unconfigured deployment must not confirm the endpoint exists."""
    settings.admin_token = ""
    assert _get({"X-Admin-Token": "anything"}) == 404


def test_configured_but_no_token_is_401():
    settings.admin_token = TOKEN
    assert _get() == 401


def test_configured_with_wrong_token_is_401():
    settings.admin_token = TOKEN
    assert _get({"X-Admin-Token": "wrong"}) == 401


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


needs_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


@needs_db
def test_counts_and_stage_percentiles():
    """A cache hit skips embed/rerank/generate, so it must not enter those
    percentiles — otherwise every stage looks faster than it ever runs."""

    async def scenario():
        try:
            async with SessionLocal() as session:
                user = User(
                    email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
                    password_hash=auth.hash_password("password123"),
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)

                now = datetime.now(timezone.utc)
                session.add_all([
                    Trace(user_id=user.id, question="q1", cached=False, refused=False,
                          tokens_in=10, tokens_out=5, embed_ms=100, retrieve_ms=50,
                          rerank_ms=1000, generate_ms=2000, total_ms=3150,
                          created_at=now),
                    Trace(user_id=user.id, question="q2", cached=False, refused=True,
                          tokens_in=8, tokens_out=2, embed_ms=200, retrieve_ms=60,
                          rerank_ms=1200, generate_ms=2400, total_ms=3860,
                          created_at=now),
                    # Cache hit: no embed/rerank/generate at all.
                    Trace(user_id=user.id, question="q3", cached=True, refused=False,
                          tokens_in=0, tokens_out=0, total_ms=770, created_at=now),
                    # Outside the window; must not be counted.
                    Trace(user_id=user.id, question="old", cached=False,
                          tokens_in=999, tokens_out=999, total_ms=99999,
                          created_at=now - timedelta(days=3)),
                ])
                await session.commit()
                user_id = user.id

            settings.admin_token = TOKEN
            async with _client() as c:
                resp = await c.get(
                    "/admin/stats?hours=1", headers={"X-Admin-Token": TOKEN}
                )
                body = resp.json()

            async with SessionLocal() as session:
                fresh = await session.get(User, user_id)
                await session.delete(fresh)  # traces cascade
                await session.commit()
            return resp.status_code, body
        finally:
            await engine.dispose()

    status, body = asyncio.run(scenario())
    assert status == 200

    # Other rows may exist from earlier runs, so assert on what this test added.
    assert body["queries"]["total"] >= 3
    assert body["queries"]["cached"] >= 1
    assert body["queries"]["refused"] >= 1
    assert body["tokens_in"] >= 18

    # The 3-day-old trace's 99999ms must not reach the window's tail.
    assert body["total_ms"]["p95"] < 99999

    # Stage percentiles come only from uncached traces, so they sit inside the
    # range those rows actually recorded — a cached 0/None would drag p50 down.
    embed = body["stages"]["embed"]
    assert embed["p50"] is not None and embed["p50"] > 0
    assert set(body["stages"]) == {"embed", "retrieve", "rerank", "generate"}


@needs_db
def test_window_is_honoured():
    settings.admin_token = TOKEN
    assert _get({"X-Admin-Token": TOKEN}, "?hours=1") == 200
    assert _get({"X-Admin-Token": TOKEN}, "?hours=0") == 422  # ge=1
    assert _get({"X-Admin-Token": TOKEN}, "?hours=99999") == 422  # le=90 days
