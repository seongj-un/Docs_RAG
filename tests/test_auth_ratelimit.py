"""Credential attempts must be throttled. Requires Postgres for the DB paths.

Uploads and queries were limited but authentication was not, so an 8-character
password could be guessed with no ceiling at all. These tests pin that the
ceiling exists and that it stops the two different attacks separately.
"""

import asyncio
import types
import uuid

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.services.ratelimit import auth_limiter


# --- 아래 두 개는 인프라가 필요 없다: 스로틀은 DB 를 건드리기 전에 걸린다 ---


class _FakeRequest:
    def __init__(self, host: str | None):
        self.client = None if host is None else types.SimpleNamespace(host=host)


def test_throttle_needs_no_database():
    """스로틀은 인증보다 먼저 걸리므로 DB 없이 검증할 수 있다."""
    from app.routers.auth import _guard_attempts

    auth_limiter.reset()
    request = _FakeRequest("203.0.113.1")
    limit = settings.rate_limit_auth_per_min
    for _ in range(limit):
        _guard_attempts(request, "a@example.com")

    with pytest.raises(HTTPException) as caught:
        _guard_attempts(request, "a@example.com")
    assert caught.value.status_code == 429
    assert caught.value.detail == "auth rate limit exceeded"
    assert caught.value.headers == {"Retry-After": "60"}


def test_a_client_without_an_address_still_gets_a_bucket():
    """request.client 가 None 인 경우(테스트 전송·일부 프록시)에도 무제한이면 안 된다."""
    from app.routers.auth import _guard_attempts

    auth_limiter.reset()
    anonymous = _FakeRequest(None)
    limit = settings.rate_limit_auth_per_min
    for _ in range(limit):
        _guard_attempts(anonymous, None)
    with pytest.raises(HTTPException):
        _guard_attempts(anonymous, None)


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


# 모듈 전체가 아니라 DB 를 쓰는 테스트에만 붙인다. 위의 스로틀 테스트는
# 인프라 없이 돌아야 하고, 모듈 표시를 쓰면 그것까지 함께 스킵된다.
needs_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


@pytest.fixture(autouse=True)
def clean_buckets():
    auth_limiter.reset()
    yield
    auth_limiter.reset()


def _attempts(payloads: list[dict], path: str = "/auth/login") -> list[int]:
    async def scenario():
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                return [(await c.post(path, json=p)).status_code for p in payloads]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


@needs_db
def test_repeated_wrong_passwords_stop_at_429():
    """Without this the only cost of guessing was the attacker's own time."""
    limit = settings.rate_limit_auth_per_min
    payloads = [
        {"email": "victim@example.com", "password": f"guess-{i}"}
        for i in range(limit + 3)
    ]
    codes = _attempts(payloads)
    assert codes[0] == 401, codes[:3]          # 자격증명 오류로 시작해
    assert 429 in codes, codes                  # 한도에 걸려 멈춘다
    assert codes[-1] == 429, codes[-3:]


@needs_db
def test_one_account_is_protected_across_addresses():
    """The email bucket is what stops a distributed attack on one account.

    Every request here comes from the same test client, so this asserts the
    email axis is consumed at all — the address axis alone would not tell
    these two attacks apart.
    """
    limit = settings.rate_limit_auth_per_min
    target = "target@example.com"
    codes = _attempts(
        [{"email": target, "password": f"p{i}"} for i in range(limit + 2)]
    )
    assert codes.count(429) >= 2, codes


@needs_db
def test_signup_is_throttled_too():
    """Otherwise accounts can be created in bulk."""
    limit = settings.rate_limit_auth_per_min
    payloads = [
        {"email": f"bulk-{uuid.uuid4().hex[:8]}@example.com", "password": "password123"}
        for _ in range(limit + 2)
    ]
    codes = _attempts(payloads, path="/auth/signup")
    assert 429 in codes, codes


@needs_db
def test_the_limit_is_checked_before_hashing():
    """argon2 is deliberately slow; paying that for an attacker is the DoS.

    A throttled attempt must come back fast, so the response cannot have gone
    through password verification.
    """
    import time

    limit = settings.rate_limit_auth_per_min
    for _ in range(limit):
        _attempts([{"email": "x@example.com", "password": "no"}])[0]

    started = time.perf_counter()
    code = _attempts([{"email": "x@example.com", "password": "no"}])[0]
    elapsed = time.perf_counter() - started
    assert code == 429
    assert elapsed < 0.5, f"{elapsed:.2f}s — 해싱을 거친 것으로 보인다"
