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


# --- 토큰 수명 ---


async def _make_user(session, email: str):
    from app.services import auth

    return await auth.create_user(session, email, "password-123")


def test_raw_token_is_never_stored():
    """DB 에는 해시만 있어야 한다. 유출돼도 링크를 만들 수 없어야 하므로."""
    from app.services import verification

    async def scenario():
        async with SessionLocal() as session:
            user = await _make_user(session, f"raw-{uuid.uuid4().hex}@example.com")
            raw = await verification.issue_token(session, user.id)

            result = await session.execute(
                text("SELECT token_hash FROM email_verification_tokens "
                     "WHERE user_id = :uid").bindparams(uid=user.id)
            )
            stored = [row[0] for row in result]
        return raw, stored

    raw, stored = run_async(scenario)

    assert stored == [verification.hash_token(raw)]
    assert raw not in stored


def test_valid_token_marks_the_user_verified():
    from app.services import verification

    async def scenario():
        async with SessionLocal() as session:
            user = await _make_user(session, f"ok-{uuid.uuid4().hex}@example.com")
            assert user.email_verified is False

            raw = await verification.issue_token(session, user.id)
            result, verified = await verification.consume_token(session, raw)
        return result, verified

    result, verified = run_async(scenario)

    assert result is verification.VerifyResult.OK
    assert verified is not None
    assert verified.email_verified is True


def test_a_consumed_token_reports_already_verified():
    """두 번째 클릭은 '만료됐다'가 아니라 '이미 인증하셨다'여야 한다."""
    from app.services import verification

    async def scenario():
        async with SessionLocal() as session:
            user = await _make_user(session, f"twice-{uuid.uuid4().hex}@example.com")
            raw = await verification.issue_token(session, user.id)
            await verification.consume_token(session, raw)

            result, _ = await verification.consume_token(session, raw)
        return result

    result = run_async(scenario)

    assert result is verification.VerifyResult.ALREADY


def test_an_expired_token_is_refused():
    from datetime import datetime, timedelta, timezone

    from app.services import verification

    async def scenario():
        async with SessionLocal() as session:
            user = await _make_user(session, f"old-{uuid.uuid4().hex}@example.com")
            raw = await verification.issue_token(session, user.id)
            await session.execute(
                text("UPDATE email_verification_tokens SET expires_at = :past "
                     "WHERE user_id = :uid").bindparams(
                         past=datetime.now(timezone.utc) - timedelta(seconds=1),
                         uid=user.id,
                     )
            )
            await session.commit()

            result, _ = await verification.consume_token(session, raw)
        return result

    result = run_async(scenario)

    assert result is verification.VerifyResult.INVALID


def test_an_unknown_token_is_refused_the_same_way_as_an_expired_one():
    """둘을 나누면 임의 문자열을 던져 토큰 존재 여부를 캐낼 수 있게 된다."""
    from app.services import verification

    async def scenario():
        async with SessionLocal() as session:
            result, user = await verification.consume_token(session, "no-such-token")
        return result, user

    result, user = run_async(scenario)

    assert result is verification.VerifyResult.INVALID
    assert user is None


def test_reissuing_kills_the_previous_link():
    """살아 있는 링크가 둘이면 어느 쪽이 유효한지 사용자가 알 수 없다."""
    from app.services import verification

    async def scenario():
        async with SessionLocal() as session:
            user = await _make_user(session, f"resend-{uuid.uuid4().hex}@example.com")
            first = await verification.issue_token(session, user.id)
            second = await verification.issue_token(session, user.id)

            stale, _ = await verification.consume_token(session, first)
            fresh, _ = await verification.consume_token(session, second)
        return stale, fresh

    stale, fresh = run_async(scenario)

    assert stale is verification.VerifyResult.INVALID
    assert fresh is verification.VerifyResult.OK


def test_reissuing_keeps_consumed_rows():
    """소비 기록까지 지우면 '이미 인증하셨다'를 말할 수 없게 된다."""
    from app.services import verification

    async def scenario():
        async with SessionLocal() as session:
            user = await _make_user(session, f"keep-{uuid.uuid4().hex}@example.com")
            used = await verification.issue_token(session, user.id)
            await verification.consume_token(session, used)
            await verification.issue_token(session, user.id)

            result, _ = await verification.consume_token(session, used)
        return result

    result = run_async(scenario)

    assert result is verification.VerifyResult.ALREADY


def test_link_points_at_the_frontend_not_the_api():
    """메일 스캐너가 GET 을 미리 밟아도 토큰이 타지 않아야 한다."""
    from app.config import settings
    from app.services import verification

    link = verification.build_link("abc123")

    assert link.startswith(settings.app_base_url)
    assert "/verify?token=abc123" in link


def test_email_body_carries_the_link_in_both_parts():
    from app.services import verification

    subject, html, plain = verification.build_email("https://example.test/verify?token=x")

    assert subject
    assert "https://example.test/verify?token=x" in html
    assert "https://example.test/verify?token=x" in plain
