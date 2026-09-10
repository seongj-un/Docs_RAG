"""인증 라우트. Postgres 가 필요하다 (메일은 콘솔 어댑터로 나간다)."""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.services import verification
from app.services.ratelimit import auth_limiter, upload_limiter, verify_resend_limiter

BROKEN_PDF = b"%PDF-1.4 not actually a pdf"


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


async def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    )


async def _signup(client: AsyncClient) -> tuple[str, dict]:
    email = f"verify-{uuid.uuid4().hex}@example.com"
    response = await client.post(
        "/auth/signup", json={"email": email, "password": "password-123"}
    )
    assert response.status_code == 201, response.text
    return email, response.json()


async def _token_for(email: str) -> str:
    """콘솔 어댑터로는 메일을 읽을 수 없으므로 새로 발급해 대신 쓴다."""
    from app.services import auth

    async with SessionLocal() as session:
        user = await auth.get_user_by_email(session, email)
        assert user is not None
        return await verification.issue_token(session, user.id)


def test_signup_starts_unverified():
    async def scenario():
        async with await _client() as client:
            _, body = await _signup(client)
        return body

    body = run_async(scenario)

    assert body["email_verified"] is False


def test_verify_marks_the_account_and_me_reflects_it():
    async def scenario():
        async with await _client() as client:
            email, _ = await _signup(client)
            token = await _token_for(email)

            response = await client.post("/auth/verify", json={"token": token})
            assert response.status_code == 200, response.text
            assert response.json()["email_verified"] is True

            me = await client.get("/auth/me")
            assert me.status_code == 200
            assert me.json()["email_verified"] is True

    run_async(scenario)


def test_a_second_click_says_already_verified():
    async def scenario():
        async with await _client() as client:
            email, _ = await _signup(client)
            token = await _token_for(email)
            await client.post("/auth/verify", json={"token": token})

            again = await client.post("/auth/verify", json={"token": token})
        return again

    again = run_async(scenario)

    assert again.status_code == 409
    assert again.json()["detail"] == "email already verified"


def test_a_bad_token_is_a_400():
    async def scenario():
        async with await _client() as client:
            await _signup(client)
            response = await client.post("/auth/verify", json={"token": "nonsense"})
        return response

    response = run_async(scenario)

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid or expired token"


def test_verify_needs_no_session():
    """메일 링크는 다른 브라우저에서 열린다. 로그인 상태를 요구하면 막힌다."""

    async def scenario():
        async with await _client() as client:
            email, _ = await _signup(client)
            token = await _token_for(email)

        async with await _client() as fresh:  # 쿠키 없는 새 클라이언트
            response = await fresh.post("/auth/verify", json={"token": token})
        return response

    response = run_async(scenario)

    assert response.status_code == 200


def test_resend_requires_a_session():
    async def scenario():
        async with await _client() as client:
            response = await client.post("/auth/resend-verification")
        return response

    response = run_async(scenario)

    assert response.status_code == 401


def test_resend_is_throttled():
    async def scenario():
        async with await _client() as client:
            await _signup(client)

            first = await client.post("/auth/resend-verification")
            second = await client.post("/auth/resend-verification")
        return first, second

    first, second = run_async(scenario)

    assert first.status_code == 204
    assert second.status_code == 429
    assert second.json()["detail"] == "verification email rate limit exceeded"


def test_resend_to_a_verified_account_is_a_409():
    async def scenario():
        async with await _client() as client:
            email, _ = await _signup(client)
            token = await _token_for(email)
            await client.post("/auth/verify", json={"token": token})

            response = await client.post("/auth/resend-verification")
        return response

    response = run_async(scenario)

    assert response.status_code == 409
    assert response.json()["detail"] == "email already verified"


def test_resend_to_a_verified_account_never_burns_the_rate_limit():
    """already-verified 는 리미터보다 먼저 걸려야 한다.

    분당 1 회라 순서가 뒤집히면(리미터를 먼저 소비) 티가 난다: 첫 호출은
    빈 버킷이 아직 여유가 있어 통과하고 email_verified 에서 409 로 끝나
    "우연히" 맞아 보이지만, 두 번째 호출은 리미터가 이미 바닥나 429 로
    샌다. 검증된 계정은 몇 번을 다시 불러도 항상 409 여야 하고, 그건
    리미터 토큰을 전혀 축내지 않을 때만 성립한다.
    """

    async def scenario():
        async with await _client() as client:
            email, _ = await _signup(client)
            token = await _token_for(email)
            await client.post("/auth/verify", json={"token": token})

            first = await client.post("/auth/resend-verification")
            second = await client.post("/auth/resend-verification")
        return first, second

    first, second = run_async(scenario)

    for response in (first, second):
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "email already verified"


def test_the_sixth_question_is_refused_until_verified():
    """맛보기를 다 쓰면 429 가 아니라 403 이어야 한다 — 기다려도 안 풀린다."""
    from app.services import usage

    async def scenario():
        async with await _client() as client:
            email, body = await _signup(client)
            user_id = uuid.UUID(body["id"])

            async with SessionLocal() as session:
                for _ in range(settings.unverified_quota_queries):
                    await usage.record(session, user_id, "query")

            response = await client.post(
                "/query", json={"question": "맛보기를 다 쓴 뒤의 질문"}
            )
        return response

    response = run_async(scenario)

    assert response.status_code == 403
    assert response.json()["detail"] == "email verification required"


def test_verifying_restores_the_normal_quota():
    from app.services import usage

    async def scenario():
        async with await _client() as client:
            email, body = await _signup(client)
            user_id = uuid.UUID(body["id"])

            async with SessionLocal() as session:
                for _ in range(settings.unverified_quota_queries):
                    await usage.record(session, user_id, "query")

            token = await _token_for(email)
            await client.post("/auth/verify", json={"token": token})

            response = await client.post(
                "/query", json={"question": "인증한 뒤의 질문"}
            )
        return response

    response = run_async(scenario)

    # 403 만 아니면 된다. 임베딩 서버가 없으면 503 이 정상이고, 그건 게이트
    # 가 열렸다는 뜻이다.
    assert response.status_code != 403


def test_the_second_upload_is_refused_and_deleting_does_not_reopen_it():
    """지우기로 한도가 초기화되면 무제한 임베딩이 공짜가 된다."""
    upload_limiter.reset()

    async def scenario():
        async with await _client() as client:
            await _signup(client)

            first = await client.post(
                "/documents",
                files={"file": ("one.pdf", BROKEN_PDF, "application/pdf")},
            )
            assert first.status_code == 202, first.text
            doc_id = first.json()["id"]

            second = await client.post(
                "/documents",
                files={"file": ("two.pdf", BROKEN_PDF, "application/pdf")},
            )
            assert second.status_code == 403
            assert second.json()["detail"] == "email verification required"

            assert (await client.delete(f"/documents/{doc_id}")).status_code == 204

            third = await client.post(
                "/documents",
                files={"file": ("three.pdf", BROKEN_PDF, "application/pdf")},
            )
        return third

    third = run_async(scenario)

    assert third.status_code == 403, "문서를 지우자 한도가 초기화됐다"
