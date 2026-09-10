"""사용량 화면이 미인증 계정에 거짓말하지 않는지. Postgres 가 필요하다."""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.services import usage


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


pytestmark = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


def test_unverified_usage_reports_the_taster_limits():
    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            signup = await client.post(
                "/auth/signup",
                json={
                    "email": f"usage-{uuid.uuid4().hex}@example.com",
                    "password": "password-123",
                },
            )
            assert signup.status_code == 201
            user_id = uuid.UUID(signup.json()["id"])

            async with SessionLocal() as session:
                # 세 종류의 개수를 일부러 다르게 둔다. 전부 1개씩이면
                # documents_total 이 upload 대신 ingest 를 세도 똑같이 1 이
                # 나와서, 이 테스트가 막으려는 바로 그 혼동을 통과시킨다.
                for _ in range(3):
                    await usage.record(session, user_id, "query")
                for _ in range(2):
                    await usage.record(session, user_id, "upload")
                await usage.record(session, user_id, "ingest", pages=4)

            return (await client.get("/usage")).json()

    body = run_async(scenario)

    assert body["email_verified"] is False
    assert body["queries_total"] == 3
    # 2 여야 한다. ingest(1건) 를 셌다면 1, query(3건) 를 셌다면 3 이 나온다.
    assert body["documents_total"] == 2
    assert body["unverified_query_limit"] == settings.unverified_quota_queries
    assert body["unverified_document_limit"] == settings.unverified_quota_documents
    # 기존 필드는 뜻까지 그대로 남는다 — 인증하고 나면 화면이 이쪽을 쓴다.
    # pages_this_month 는 ingest 의 쪽수만 세므로 upload 2건에 영향받지 않는다.
    assert body["queries_today"] == 3
    assert body["pages_this_month"] == 4
    assert body["queries_per_day"] == settings.quota_queries_per_day
    assert body["pages_per_month"] == settings.quota_upload_pages_per_month
