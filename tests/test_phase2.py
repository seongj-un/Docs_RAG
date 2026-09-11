"""M3 Phase 2 proofs: quota 429, rate-limit 429, semantic cache. Requires Postgres.

The cache test is the one the spec calls out — "시맨틱 캐시 히트 시 LLM 호출
0회" — so it makes the LLM raise if touched: a hit that reached the model would
fail the test rather than pass silently.
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.models import QueryCache, User
from app.services import auth, cache, llm
from app.services.ratelimit import query_limiter

EMBED_DIM = settings.embed_dim


def run_async(coro_fn):
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


@pytest.fixture(autouse=True)
def _clean_limiter():
    """Buckets are process-global; isolate tests from each other."""
    query_limiter.reset()
    yield
    query_limiter.reset()


async def _signup(client: AsyncClient) -> str:
    email = f"phase2-{uuid.uuid4().hex[:8]}@example.com"
    await client.post("/auth/signup", json={"email": email, "password": "password123"})
    return email


async def _drop_user(email: str) -> None:
    async with SessionLocal() as session:
        user = await auth.get_user_by_email(session, email)
        if user is not None:
            await session.delete(user)
            await session.commit()


def test_rate_limit_returns_429_with_retry_after():
    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            email = await _signup(client)
            user_id = (await client.get("/auth/me")).json()["id"]

            # Drain this user's bucket without spending any embedding calls.
            while query_limiter.allow(f"user:{user_id}"):
                pass

            resp = await client.post("/query", json={"question": "안녕하세요"})
            out = (resp.status_code, resp.headers.get("Retry-After"))
        await _drop_user(email)
        return out

    status, retry_after = run_async(scenario)
    assert status == 429
    assert retry_after == "60"


def test_quota_exceeded_returns_429(monkeypatch):
    monkeypatch.setattr(settings, "quota_queries_per_day", 2)

    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            email = await _signup(client)
            user_id = uuid.UUID((await client.get("/auth/me")).json()["id"])

            async with SessionLocal() as session:
                from app.services import usage, verification

                # 이 한도는 인증된 계정의 것이다. 미인증 상태로 두면 이보다
                # 먼저 걸리는 맛보기 게이트(unverified_quota_queries)를 보게
                # 되어 이 테스트가 검사하려는 429 대신 다른 경로를 탄다.
                token = await verification.issue_token(session, user_id)
                await verification.consume_token(session, token)

                for _ in range(2):  # exactly the daily allowance
                    await usage.record(session, user_id, "query")

            resp = await client.post("/query", json={"question": "안녕하세요"})
            out = resp.status_code
        await _drop_user(email)
        return out

    assert run_async(scenario) == 429


def test_concurrent_queries_from_a_verified_account_accept_only_one_over_quota(
    monkeypatch,
):
    """검증된 계정의 일일 쿼터도 미인증 게이트와 같은 틈을 갖고 있었다 —
    이 수정이 그 둘에 같은 보장을 주는지는 별개로 확인해야 한다.

    tests/test_verification_api.py 의 업로드 경쟁 테스트와 같은 요령이다:
    세 번째 커넥션으로 QueryRunner.enforce_limits 가 재검사 직전에 잡는
    것과 같은 어드바이저리 락을 먼저 쥐어 둘을 그 뒤에 정말로 묶어세우고
    (타임아웃 안에 끝나지 않았다는 것으로 "진짜 막혔다"까지 확인한 뒤)
    락을 풀어, 어느 쪽이 이길지는 Postgres 의 잠금 대기열에 맡긴다.
    """
    monkeypatch.setattr(settings, "quota_queries_per_day", 1)

    from datetime import datetime, timezone

    from fastapi import HTTPException

    from app.models import User as UserModel
    from app.services import auth, usage
    from app.services.pipeline import QueryRunner

    async def scenario():
        async with SessionLocal() as setup:
            user = await auth.create_user(
                setup, f"query-race-{uuid.uuid4().hex}@example.com", "password123"
            )
            user.email_verified_at = datetime.now(timezone.utc)
            await setup.commit()
            user_id = user.id

        async with (
            SessionLocal() as blocker,
            SessionLocal() as session_a,
            SessionLocal() as session_b,
        ):
            await usage.acquire_quota_lock(blocker, user_id, "query")

            # QueryRunner only ever reads user.id/email_verified, so a
            # detached stand-in per session avoids a second lookup query —
            # the same trick tests/test_query_pipeline.py uses.
            verified_now = datetime.now(timezone.utc)
            user_a = UserModel(
                id=user_id, email="a", password_hash="x", email_verified_at=verified_now
            )
            user_b = UserModel(
                id=user_id, email="b", password_hash="x", email_verified_at=verified_now
            )
            runner_a = QueryRunner(session_a, user_a, "q", document_id=None, hybrid=False)
            runner_b = QueryRunner(session_b, user_b, "q", document_id=None, hybrid=False)

            task_a = asyncio.create_task(runner_a.enforce_limits("10.0.0.1"))
            task_b = asyncio.create_task(runner_b.enforce_limits("10.0.0.2"))

            # 블로커가 락을 쥔 채로 이미 둘 다 끝나버렸다면 이 테스트는
            # 아무 경쟁도 만들지 못한 것이다 — 조용히 통과하는 대신 여기서
            # 드러낸다.
            done, _pending = await asyncio.wait({task_a, task_b}, timeout=0.3)
            assert not done, "블로커가 잠갔는데 enforce_limits 가 먼저 끝났다"

            await blocker.rollback()  # 아무것도 안 바꿨으니 롤백으로 락만 푼다

            results = await asyncio.gather(task_a, task_b, return_exceptions=True)

        async with SessionLocal() as check:
            total = await usage.queries_total(check, user_id)
        return results, total

    results, total = run_async(scenario)

    oks = [r for r in results if r is None]
    errs = [r for r in results if isinstance(r, HTTPException)]
    assert len(oks) == 1 and len(errs) == 1, results
    assert errs[0].status_code == 429
    assert errs[0].detail == "daily query quota exceeded"
    # 응답 개수뿐 아니라 실제로 커밋된 예약도 하나여야 한다.
    assert total == 1, f"{total}개의 예약이 커밋됐다 — 일일 쿼터(1)가 뚫렸다"


def test_a_failed_query_does_not_spend_the_quota(monkeypatch):
    """임베딩 서버가 죽어 503 이 나도 그 질의는 쿼터를 쓰면 안 된다.

    enforce_limits 는 체크와 함께 예약을 커밋해 둔다 — 뒤따르는
    embed/retrieve/rerank/generate 가 실패하면 그 예약은
    release_reservation 이 지워야 한다. 안 지워지면 일어나지도 않은
    작업에 요금이 매겨진다(스펙이 금지하는 바로 그것).
    """
    from app.services import pipeline as qr
    from app.services import usage
    from app.services.upstream import UpstreamUnavailable

    async def failing_embed(question):
        raise UpstreamUnavailable("embedding", RuntimeError("model server down"))

    async def failing_embed_full(question):
        raise UpstreamUnavailable("embedding", RuntimeError("model server down"))

    monkeypatch.setattr(qr.embeddings, "embed_query", failing_embed)
    monkeypatch.setattr(qr.embeddings, "embed_query_full", failing_embed_full)

    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            email = await _signup(client)
            user_id = uuid.UUID((await client.get("/auth/me")).json()["id"])

            resp = await client.post("/query", json={"question": "안녕하세요"})
            status = resp.status_code

            async with SessionLocal() as session:
                total = await usage.queries_total(session, user_id)
        await _drop_user(email)
        return status, total

    status, total = run_async(scenario)

    assert status == 503
    assert total == 0, "실패한 질의가 쿼터를 소비했다"


def test_semantic_cache_hit_makes_zero_llm_calls(monkeypatch):
    """A cache hit must short-circuit before retrieval and generation."""
    probe = [0.05] * EMBED_DIM

    async def fake_embed_full(question):
        return probe, object()

    async def exploding_generate(*args, **kwargs):
        raise AssertionError("LLM must not be called on a cache hit")

    from app.services import pipeline as qr

    monkeypatch.setattr(qr.embeddings, "embed_query_full", fake_embed_full)
    monkeypatch.setattr(llm, "generate", exploding_generate)

    async def scenario():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            email = await _signup(client)
            user_id = uuid.UUID((await client.get("/auth/me")).json()["id"])

            async with SessionLocal() as session:
                session.add(
                    QueryCache(
                        user_id=user_id,
                        document_id=None,
                        question="보증금은 얼마인가요?",
                        question_embedding=probe,
                        answer="보증금은 오천만원입니다 [p.1].",
                        refused=False,
                        citations=[],
                    )
                )
                await session.commit()

            resp = await client.post("/query", json={"question": "보증금 얼마?"})
            out = (resp.status_code, resp.json())
        await _drop_user(email)
        return out

    status, body = run_async(scenario)
    assert status == 200
    assert body["answer"] == "보증금은 오천만원입니다 [p.1]."
    assert body["refused"] is False


def test_cache_never_serves_across_tenants_or_scopes():
    async def scenario():
        probe = [0.05] * EMBED_DIM
        async with SessionLocal() as session:
            alice = User(
                email=f"a-{uuid.uuid4().hex[:8]}@example.com",
                password_hash=auth.hash_password("password123"),
            )
            bob = User(
                email=f"b-{uuid.uuid4().hex[:8]}@example.com",
                password_hash=auth.hash_password("password123"),
            )
            session.add_all([alice, bob])
            await session.commit()
            await session.refresh(alice)
            await session.refresh(bob)

            session.add(
                QueryCache(
                    user_id=alice.id,
                    document_id=None,
                    question="q",
                    question_embedding=probe,
                    answer="ALICE ANSWER",
                    refused=False,
                    citations=[],
                )
            )
            await session.commit()

            own = await cache.lookup(
                session, user_id=alice.id, document_id=None, question_embedding=probe
            )
            other_tenant = await cache.lookup(
                session, user_id=bob.id, document_id=None, question_embedding=probe
            )
            # Same user, but a document-scoped question must not hit the
            # corpus-wide entry.
            other_scope = await cache.lookup(
                session,
                user_id=alice.id,
                document_id=uuid.uuid4(),
                question_embedding=probe,
            )

            await session.delete(alice)
            await session.delete(bob)
            await session.commit()
            return own, other_tenant, other_scope

    own, other_tenant, other_scope = run_async(scenario)
    assert own is not None and own.answer == "ALICE ANSWER"
    assert other_tenant is None
    assert other_scope is None


def test_dissimilar_question_misses_the_cache():
    async def scenario():
        async with SessionLocal() as session:
            user = User(
                email=f"c-{uuid.uuid4().hex[:8]}@example.com",
                password_hash=auth.hash_password("password123"),
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

            stored = [1.0] + [0.0] * (EMBED_DIM - 1)
            unrelated = [0.0] * (EMBED_DIM - 1) + [1.0]  # orthogonal
            session.add(
                QueryCache(
                    user_id=user.id,
                    document_id=None,
                    question="q",
                    question_embedding=stored,
                    answer="STORED",
                    refused=False,
                    citations=[],
                )
            )
            await session.commit()

            hit = await cache.lookup(
                session, user_id=user.id, document_id=None, question_embedding=unrelated
            )
            await session.delete(user)
            await session.commit()
            return hit

    assert run_async(scenario) is None


def test_corpus_wide_cache_is_invalidated_on_corpus_change():
    async def scenario():
        probe = [0.05] * EMBED_DIM
        async with SessionLocal() as session:
            user = User(
                email=f"d-{uuid.uuid4().hex[:8]}@example.com",
                password_hash=auth.hash_password("password123"),
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

            session.add(
                QueryCache(
                    user_id=user.id,
                    document_id=None,
                    question="q",
                    question_embedding=probe,
                    answer="STALE",
                    refused=False,
                    citations=[],
                )
            )
            await session.commit()

            before = await cache.lookup(
                session, user_id=user.id, document_id=None, question_embedding=probe
            )
            await cache.invalidate_corpus_wide(session, user.id)
            after = await cache.lookup(
                session, user_id=user.id, document_id=None, question_embedding=probe
            )

            await session.delete(user)
            await session.commit()
            return before, after

    before, after = run_async(scenario)
    assert before is not None
    assert after is None
