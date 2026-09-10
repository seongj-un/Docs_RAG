"""이메일 인증 토큰의 수명. Postgres 가 필요하다."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db import SessionLocal, engine


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


def test_migration_grandfathers_existing_accounts():
    """0008 이전에 있던 계정은 인증된 것으로 남아야 한다.

    특히 0003 의 시드 유저 — 로그인이 불가능한 계정이라 인증할 방법이 없고,
    eval 코퍼스가 거기 묶여 있다. 미인증으로 두면 되살릴 수 없이 잠긴다.
    """

    async def scenario():
        async with SessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT email_verified_at FROM users "
                    "WHERE email = 'seed@local.invalid'"
                )
            )
            return result.first()

    row = run_async(scenario)
    assert row is not None, "시드 유저가 없다 — 0003 이 적용되지 않았다"
    assert row[0] is not None, "시드 유저가 미인증으로 남았다"


def test_token_table_exists_with_unique_hash():
    """같은 해시를 두 번 넣을 수 없어야 한다."""

    async def scenario():
        from app.models import EmailVerificationToken

        async with SessionLocal() as session:
            user_id = uuid.UUID("00000000-0000-0000-0000-00000000dead")
            expires = datetime.now(timezone.utc) + timedelta(hours=1)
            session.add(
                EmailVerificationToken(
                    user_id=user_id, token_hash="duplicate-me", expires_at=expires
                )
            )
            await session.commit()

            session.add(
                EmailVerificationToken(
                    user_id=user_id, token_hash="duplicate-me", expires_at=expires
                )
            )
            with pytest.raises(Exception):
                await session.commit()
            await session.rollback()

            await session.execute(
                text(
                    "DELETE FROM email_verification_tokens "
                    "WHERE token_hash = 'duplicate-me'"
                )
            )
            await session.commit()

    run_async(scenario)
