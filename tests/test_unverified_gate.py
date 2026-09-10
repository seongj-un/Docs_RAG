"""미인증 계정의 맛보기 쿼터. Postgres 가 필요하다.

핵심은 한도가 아니라 **창**이다. 기존 쿼터처럼 "오늘"로 세면 미인증 계정이
매일 다시 5회를 받아, 막으려던 재가입 어뷰즈가 그대로 통과한다.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, engine
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


async def _fresh_user(session, verified: bool):
    from app.services import auth

    user = await auth.create_user(
        session, f"gate-{uuid.uuid4().hex}@example.com", "password-123"
    )
    if verified:
        user.email_verified_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(user)
    return user


async def _record_queries(session, user_id, count: int) -> None:
    for _ in range(count):
        await usage.record(session, user_id, "query")


async def _record_uploads(session, user_id, count: int) -> None:
    """업로드 '수락' 이벤트. 인덱싱 성공 여부와 무관하게 남는다."""
    for _ in range(count):
        await usage.record(session, user_id, "upload")


def test_query_gate_opens_and_then_closes():
    async def scenario():
        async with SessionLocal() as session:
            user = await _fresh_user(session, verified=False)

            assert await usage.unverified_query_exceeded(session, user.id) is False

            await _record_queries(session, user.id, settings.unverified_quota_queries)

            assert await usage.unverified_query_exceeded(session, user.id) is True

    run_async(scenario)


def test_the_query_window_is_lifetime_not_today():
    """어제 쓴 것도 센다. '오늘'로 세면 매일 5회가 다시 열린다."""

    async def scenario():
        async with SessionLocal() as session:
            user = await _fresh_user(session, verified=False)
            await _record_queries(session, user.id, settings.unverified_quota_queries)

            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            await session.execute(
                text(
                    "UPDATE usage_events SET created_at = :then WHERE user_id = :uid"
                # uid 를 str 로 넘기면 asyncpg 가 ::VARCHAR 로 컴파일해서
                # "operator does not exist: uuid = character varying" 로 죽는다.
                ).bindparams(then=yesterday, uid=user.id)
            )
            await session.commit()

            assert await usage.queries_today(session, user.id) == 0
            assert await usage.unverified_query_exceeded(session, user.id) is True

    run_async(scenario)


def test_upload_gate_counts_accepted_uploads_not_surviving_documents():
    """올렸다 지워도 되돌아가지 않아야 한다.

    usage_events 는 documents 가 아니라 users 를 참조하므로 문서를 지워도
    행이 남는다. documents 를 셌다면 지우기로 한도가 초기화된다.
    """

    async def scenario():
        async with SessionLocal() as session:
            user = await _fresh_user(session, verified=False)

            await _record_uploads(session, user.id, settings.unverified_quota_documents)

            assert await usage.unverified_upload_exceeded(session, user.id) is True

            # 문서를 전부 지운 상황을 흉내낸다 (애초에 documents 행이 없다).
            assert await usage.documents_total(session, user.id) == (
                settings.unverified_quota_documents
            )

    run_async(scenario)


def test_indexing_events_do_not_count_toward_the_upload_gate():
    """수락과 색인은 다른 사건이다. ingest 를 세면 색인이 끝나기 전에
    던진 업로드가 전부 통과하고, 실패한 업로드는 아예 세어지지 않는다."""

    async def scenario():
        async with SessionLocal() as session:
            user = await _fresh_user(session, verified=False)
            await usage.record(session, user.id, "ingest", pages=40)

            assert await usage.documents_total(session, user.id) == 0
            assert await usage.unverified_upload_exceeded(session, user.id) is False

    run_async(scenario)


def test_a_verified_account_is_not_gated():
    async def scenario():
        async with SessionLocal() as session:
            user = await _fresh_user(session, verified=True)
            await _record_queries(session, user.id, settings.unverified_quota_queries + 5)

            assert user.email_verified is True
            # 게이트 함수 자체는 인증 여부를 묻지 않는다 — 호출자가 먼저 본다.
            # 여기서 확인하는 것은 정상 쿼터가 아직 살아 있다는 것.
            assert await usage.query_quota_exceeded(session, user.id) is False

    run_async(scenario)


def test_zero_disables_the_gate():
    original = settings.unverified_quota_queries
    try:
        settings.unverified_quota_queries = 0

        async def scenario():
            async with SessionLocal() as session:
                user = await _fresh_user(session, verified=False)
                await _record_queries(session, user.id, 3)
                assert await usage.unverified_query_exceeded(session, user.id) is False

        run_async(scenario)
    finally:
        settings.unverified_quota_queries = original
