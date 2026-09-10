"""인증 라우트. Postgres 가 필요하다 (메일은 콘솔 어댑터로 나간다)."""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.main import app
from app.services import verification


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
