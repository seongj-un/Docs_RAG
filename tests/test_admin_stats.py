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
from sqlalchemy import delete, text

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.models import Document, Trace, User
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
    """단계 백분위가 어떤 표본 위에서 계산되는지를 고정한다.

    캐시 히트는 **임베딩을 건너뛰지 않는다** — 시맨틱 캐시라 질문이 벡터가
    돼야 조회가 되고(pipeline.embed → cache.lookup), 그래서 히트도 진짜
    ``embed_ms`` 를 남긴다. 안 도는 것은 뒤의 세 단계뿐이고 그건 NULL 이라
    percentile 이 알아서 건너뛴다. 한동안 반대로 적혀 있었고, 같은 문장이
    라우터·스키마·README·이 픽스처 네 군데에 함께 있어서 서로를 근거로
    살아남았다.
    """

    async def scenario():
        try:
            async with SessionLocal() as session:
                # 이 테스트만 정확한 백분위를 단언한다 — 다른 케이스들은
                # `>=` 로 느슨하게 보지만 여기서는 "히트를 넣었을 때와 뺐을
                # 때 p50 이 100 이냐 150 이냐"가 요점이라, 앞선 실행이 남긴
                # 행이 섞이면 그 차이가 흐려진다.
                await session.execute(delete(Trace))
                await session.commit()
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
                    # Cache hit. It still embeds — the cache is semantic, so the
                    # question has to become a vector before it can be looked
                    # up. Only the three later timers stay NULL.
                    Trace(user_id=user.id, question="q3", cached=True, refused=False,
                          tokens_in=0, tokens_out=0, embed_ms=90, total_ms=770,
                          created_at=now),
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
                # 같은 순간을 넓은 창으로도 본다. 3일 전 행이 창 밖이라는
                # 것은 이 둘의 **차이**로만 정직하게 증명된다 — 아래 참조.
                wide = (await c.get(
                    "/admin/stats?hours=96", headers={"X-Admin-Token": TOKEN}
                )).json()

            async with SessionLocal() as session:
                fresh = await session.get(User, user_id)
                await session.delete(fresh)  # traces cascade
                await session.commit()
            return resp.status_code, body, wide
        finally:
            await engine.dispose()

    status, body, wide = asyncio.run(scenario())
    assert status == 200

    # Other rows may exist from earlier runs, so assert on what this test added.
    assert body["queries"]["total"] >= 3
    assert body["queries"]["cached"] >= 1
    assert body["queries"]["refused"] >= 1
    assert body["tokens_in"] >= 18

    # 3일 전 행이 1시간 창 밖이라는 것을 창 둘의 차이로 증명한다.
    #
    # 전에는 `p95 < 99999` 하나로 확인했는데, 그건 두 방향 모두로 틀렸다.
    # ① 창 안에 99999ms 를 넘는 **다른** 행이 하나만 생겨도 깨진다 —
    #    실제로 이 DB 에 149초짜리 진짜 질의가 기록되면서 깨졌다.
    # ② 반대로 그 행이 창 안에 잘못 포함되더라도, 행이 충분히 많으면
    #    p95 는 여전히 99999 아래일 수 있어 버그를 놓친다.
    # 차이로 보면 다른 행이 몇 개 있든 양쪽 창에 똑같이 들어가므로
    # 상쇄되고, 남는 것은 이 테스트가 심은 행뿐이다. 파일 위쪽 단언들이
    # "이 테스트가 추가한 것만 본다"는 원칙과도 같아진다.
    assert wide["queries"]["total"] - body["queries"]["total"] >= 1
    assert wide["tokens_in"] - body["tokens_in"] >= 999

    # 캐시 히트의 embed 도 표본이다. 90·100·200 세 개가 다 들어가야 p50 이
    # 100 이고, 히트를 빼면 150 이 된다 — 그 차이가 이 단언의 요점이다.
    # 히트가 실제로 쓴 임베딩 시간을 버리면 남는 표본은 캐시가 잘 들을수록
    # 얇아지고, 그때 p95 가 제일 못 믿을 값이 된다.
    embed = body["stages"]["embed"]
    assert embed["p50"] == 100, body["stages"]
    # 나머지 세 단계는 히트에서 아예 안 돌아 NULL 이고, percentile 은 NULL 을
    # 건너뛴다 — 그래서 히트를 포함해도 이 값들은 흔들리지 않는다.
    assert body["stages"]["generate"]["p50"] == 2200  # 2000·2400 둘뿐, 히트는 NULL
    assert set(body["stages"]) == {"embed", "retrieve", "rerank", "generate"}


@needs_db
def test_window_is_honoured():
    settings.admin_token = TOKEN
    assert _get({"X-Admin-Token": TOKEN}, "?hours=1") == 200
    assert _get({"X-Admin-Token": TOKEN}, "?hours=0") == 422  # ge=1
    assert _get({"X-Admin-Token": TOKEN}, "?hours=99999") == 422  # le=90 days


@needs_db
def test_query_failures_are_counted_and_named():
    """장애 중에 오르는 유일한 숫자다.

    실패한 질의가 기록되지 않던 시절에는 임베딩 서버가 죽으면 total 이
    **줄어들어** 장애 시간대가 한가한 오후처럼 보였다. 이제 total 은
    실패를 포함하고, failed 가 그 부분집합이며, query_failures 가 왜인지를
    말한다.

    지연 백분위수는 반대로 실패를 **빼고** 재야 한다. 죽은 임베더로의
    연결은 한 자릿수 밀리초에 거절당하므로, 섞으면 장애 중에 p50 이
    오히려 좋아진다 — 같은 보고서가 깨진 시간대를 건강하게 묘사하는 두
    번째 방식이다. 그래서 2ms 짜리 503 을 심어두고 p50 이 그쪽으로
    끌려가지 않는지 본다.
    """

    async def scenario():
        try:
            async with SessionLocal() as session:
                user = User(
                    email=f"fails-{uuid.uuid4().hex[:8]}@example.com",
                    password_hash=auth.hash_password("password123"),
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)

                now = datetime.now(timezone.utc)
                session.add_all([
                    Trace(user_id=user.id, question="ok", cached=False,
                          total_ms=3000, embed_ms=300, created_at=now),
                    # 죽은 임베더: 빨리 실패한다.
                    Trace(user_id=user.id, question="f1", cached=False,
                          status_code=503, error="search unavailable",
                          total_ms=2, embed_ms=2, created_at=now),
                    Trace(user_id=user.id, question="f2", cached=False,
                          status_code=503, error="search unavailable",
                          total_ms=2, embed_ms=2, created_at=now),
                    Trace(user_id=user.id, question="f3", cached=False,
                          status_code=500, error="RuntimeError",
                          total_ms=5, created_at=now),
                    # 창 밖의 실패는 이번 창에 세어지면 안 된다.
                    Trace(user_id=user.id, question="old", cached=False,
                          status_code=503, error="search unavailable",
                          total_ms=3, created_at=now - timedelta(days=3)),
                ])
                await session.commit()
                user_id = user.id

            settings.admin_token = TOKEN
            async with _client() as c:
                narrow = (await c.get(
                    "/admin/stats?hours=1", headers={"X-Admin-Token": TOKEN}
                )).json()
                wide = (await c.get(
                    "/admin/stats?hours=96", headers={"X-Admin-Token": TOKEN}
                )).json()

            async with SessionLocal() as session:
                fresh = await session.get(User, user_id)
                await session.delete(fresh)
                await session.commit()
            return narrow, wide
        finally:
            await engine.dispose()

    narrow, wide = asyncio.run(scenario())

    # 다른 행이 있을 수 있으니 이 테스트가 심은 것만 본다.
    assert narrow["queries"]["failed"] >= 3
    assert narrow["queries"]["total"] >= 4

    reasons = {f["reason"]: f["count"] for f in narrow["query_failures"]}
    assert reasons.get("503 search unavailable", 0) >= 2
    assert reasons.get("500 RuntimeError", 0) >= 1

    # 3일 전 실패가 1시간 창 밖이라는 것은 두 창의 차이로만 정직하게
    # 증명된다 — 다른 행은 양쪽에 똑같이 들어가 상쇄된다.
    assert wide["queries"]["failed"] - narrow["queries"]["failed"] >= 1

    # 2ms 짜리 실패 셋이 지연 분포에 들어갔다면 p50 은 한 자릿수가 된다.
    assert narrow["total_ms"]["p50"] is not None
    assert narrow["total_ms"]["p50"] > 10
    assert narrow["stages"]["embed"]["p50"] > 10


@needs_db
def test_indexing_failures_respect_the_window():
    """``failures`` 는 창을 통째로 무시하고 있었다.

    응답 최상단에 ``window_hours: 1`` 이 찍힌 채로 몇 달 전 색인 실패가
    딸려 나왔다 — 방금 배포가 인덱싱을 깨뜨렸는지 확인하러 온 운영자가
    옛날 실패의 벽을 보고 그게 옛날 것인 줄 알 방법이 없었다.

    ``created_at`` 이 아니라 ``updated_at`` 으로 자른다: 행에 시각이
    찍히는 것은 status 가 failed 로 바뀌는 순간(services/ingest.py)이라,
    큐에서 오래 기다린 문서는 업로드 시각과 실패 시각이 다르다.
    """

    async def scenario():
        try:
            async with SessionLocal() as session:
                user = User(
                    email=f"docfail-{uuid.uuid4().hex[:8]}@example.com",
                    password_hash=auth.hash_password("password123"),
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)

                old = datetime.now(timezone.utc) - timedelta(days=30)
                doc = Document(
                    user_id=user.id,
                    filename="ancient.pdf",
                    mime_type="application/pdf",
                    status="failed",
                    error="AncientFailure: 한 달 전에 깨진 문서",
                )
                session.add(doc)
                await session.commit()
                # onupdate 가 now() 로 덮으므로 SQL 로 직접 되돌린다.
                await session.execute(
                    text("UPDATE documents SET created_at = :t, updated_at = :t "
                         "WHERE id = :id"),
                    {"t": old, "id": doc.id},
                )
                await session.commit()
                user_id = user.id

            settings.admin_token = TOKEN
            async with _client() as c:
                narrow = (await c.get(
                    "/admin/stats?hours=1", headers={"X-Admin-Token": TOKEN}
                )).json()
                wide = (await c.get(
                    "/admin/stats?hours=2160", headers={"X-Admin-Token": TOKEN}
                )).json()

            async with SessionLocal() as session:
                fresh = await session.get(User, user_id)
                await session.delete(fresh)
                await session.commit()
            return narrow, wide
        finally:
            await engine.dispose()

    narrow, wide = asyncio.run(scenario())

    def count(body) -> int:
        return sum(
            f["count"] for f in body["failures"] if f["reason"] == "AncientFailure"
        )

    assert count(narrow) == 0, "한 달 전 실패가 1시간 창에 들어오면 안 된다"
    assert count(wide) == 1, "창을 넓히면 다시 보여야 한다"

    # documents 는 일부러 창을 안 탄다 — 지금의 코퍼스 상태를 말하는
    # 게이지라서, 1시간으로 자르면 멀쩡한 코퍼스가 ready: 0 으로 보인다.
    assert narrow["documents"]["failed"] == wide["documents"]["failed"]
