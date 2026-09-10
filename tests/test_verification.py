"""이메일 인증 토큰의 수명. Postgres 가 필요하다."""

import asyncio
import hashlib
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

    # verification.hash_token(raw) 하고만 비교하면 sha256 을 다른 다이제스트로
    # 바꿔도 자기 자신과는 항상 일치해 초록불이 뜬다 — 그러면 배포된 순간
    # 이미 발급된 인증 링크가 전부 조용히 무효화되는 변경도 이 테스트를
    # 통과한다. 알고리즘 자체를 고정하려면 그 함수를 거치지 않고 독립적으로
    # 계산해야 한다.
    assert stored == [hashlib.sha256(raw.encode("utf-8")).hexdigest()]
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


# --- 경쟁 조건: consume_token 은 원자적 클레임이어야 한다 ---


def test_concurrent_consume_of_the_same_token_yields_exactly_one_ok():
    """중복 제출·두 탭처럼 같은 토큰이 동시에 두 번 들어와도 승자는 하나다.

    그냥 asyncio.gather 로 두 소비를 던지기만 하면, 로컬 Postgres 는 왕복이
    워낙 빨라서 매번 우연히 순차 실행처럼 끝나버릴 수 있다 — 그러면 통과해도
    진짜 경쟁을 검증한 게 아니라 운을 검증한 것이다. 그래서 세 번째
    커넥션으로 토큰 행에 SELECT ... FOR UPDATE 락을 걸어 두 소비를 그 락
    뒤에 정말로 묶어세우고(타임아웃 안에 끝나지 않았다는 것으로 "진짜
    막혔다"까지 확인한 뒤), 락을 풀어 어느 쪽이 이길지는 Postgres 의 행
    잠금 대기열에 맡긴다. 승자·패자는 매 실행마다 바뀔 수 있으므로 정렬한
    쌍으로 비교한다.
    """
    from app.services import verification

    async def scenario():
        async with SessionLocal() as setup:
            user = await _make_user(setup, f"race-{uuid.uuid4().hex}@example.com")
            raw = await verification.issue_token(setup, user.id)

        async with (
            SessionLocal() as blocker,
            SessionLocal() as session_a,
            SessionLocal() as session_b,
        ):
            await blocker.execute(
                text(
                    "SELECT id FROM email_verification_tokens "
                    "WHERE token_hash = :h FOR UPDATE"
                ).bindparams(h=verification.hash_token(raw))
            )

            task_a = asyncio.create_task(verification.consume_token(session_a, raw))
            task_b = asyncio.create_task(verification.consume_token(session_b, raw))

            # 블로커가 락을 쥔 채로 이미 끝나버렸다면 이 테스트는 아무
            # 경쟁도 만들지 못한 것이다 — 조용히 통과하는 대신 여기서 드러낸다.
            done, _pending = await asyncio.wait({task_a, task_b}, timeout=0.3)
            assert not done, "블로커가 행을 잠갔는데 소비가 먼저 끝났다"

            await blocker.rollback()  # 아무것도 안 바꿨으니 롤백으로 락만 푼다

            (result_a, user_a), (result_b, user_b) = await asyncio.gather(
                task_a, task_b
            )
        return result_a, user_a, result_b, user_b

    result_a, user_a, result_b, user_b = run_async(scenario)

    assert sorted([result_a.value, result_b.value]) == sorted(["ok", "already"])

    winner_user = user_a if result_a is verification.VerifyResult.OK else user_b
    loser_user = user_b if result_a is verification.VerifyResult.OK else user_a
    assert winner_user is not None and winner_user.email_verified is True
    assert loser_user is None


def test_a_consume_racing_a_reissue_returns_invalid_not_a_crash():
    """오래된 링크를 클릭한 순간 같은 계정에 재발송이 겹치는 경우.

    issue_token 은 미소비 토큰을 지운다. consume_token 이 그 행을 아직
    처리하는 중에 재발급이 먼저 지우고 커밋해버리면, 이전 구현(SELECT 로
    읽어 파이썬 객체로 들고 있다가 나중에 UPDATE)은 대상이 사라진 UPDATE 에
    SQLAlchemy 가 StaleDataError 를 던져 죽었다 — "절대 예외를 던지지
    않는다"는 이 모듈의 계약을 깨는 것이었다.

    재발급(B)을 블로커의 락 대기열에 먼저 세운 뒤에 소비(A)를 걸어야 한다.
    그래야 락을 풀었을 때 Postgres 가 대기열 순서대로 B 를 먼저 들여보내
    "재발급이 먼저 지우고 커밋한 뒤에 소비가 그 사실을 본다"는 순서가
    강제된다 — 반대로 A 가 먼저 이겨버리면 이 경쟁 자체가 일어나지 않는다.
    """
    from app.services import verification

    async def scenario():
        async with SessionLocal() as setup:
            user = await _make_user(
                setup, f"reissue-race-{uuid.uuid4().hex}@example.com"
            )
            raw = await verification.issue_token(setup, user.id)
            user_id = user.id

        async with (
            SessionLocal() as blocker,
            SessionLocal() as session_a,
            SessionLocal() as session_b,
        ):
            await blocker.execute(
                text(
                    "SELECT id FROM email_verification_tokens "
                    "WHERE user_id = :uid AND consumed_at IS NULL FOR UPDATE"
                ).bindparams(uid=user_id)
            )

            # B(재발급)를 먼저 대기열에 세운다 — 락 해제 뒤 B 가 먼저
            # 처리되게 해서 "재발급이 이긴다"는 순서를 강제하기 위해서다.
            task_b = asyncio.create_task(verification.issue_token(session_b, user_id))
            done, _pending = await asyncio.wait({task_b}, timeout=0.3)
            assert not done, "재발급이 블로커 락을 기다리지 않고 먼저 끝났다"

            task_a = asyncio.create_task(verification.consume_token(session_a, raw))
            done, _pending = await asyncio.wait({task_a}, timeout=0.3)
            assert not done, "소비가 블로커 락을 기다리지 않고 먼저 끝났다"

            await blocker.rollback()  # 아무것도 안 바꿨으니 롤백으로 락만 푼다

            _new_raw, (result, verified_user) = await asyncio.gather(task_b, task_a)
        return result, verified_user

    result, verified_user = run_async(scenario)

    assert result is verification.VerifyResult.INVALID
    assert verified_user is None
