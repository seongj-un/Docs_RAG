"""실패한 질의도 흔적을 남긴다.

M4 이후로 ``traces`` 에 행을 쓰는 곳은 ``record_cache_hit`` 와 ``finalize``
둘뿐이었다 — 즉 성공한 질의와 캐시 히트만. 503/500/404 로 끝난 질의는 DB
어디에도 없었고, 그래서 임베딩 서버가 죽으면 ``/admin/stats`` 의
``queries.total`` 이 오히려 **줄어들어** 장애 시간대가 한가했던 것처럼
보였다.

여기서 증명하는 것은 세 가지다.
1. 실패한 질의가 상태·사유·요청 id 와 함께 기록된다(두 경로 모두).
2. 그 기록이 쿼터 예약 정리를 밀어내지 않는다 — 실패한 질의는 여전히
   사용자의 하루치를 먹지 않는다.
3. 기록이 실패해도 질의는 죽지 않는다. 관측이 관측 대상을 무너뜨리면 안
   된다는 원칙은 실패 경로에서 더 중요하다 — 이미 뭔가 잘못된 상태니까.

외부 서비스(임베딩·리랭크·LLM)는 스텁이다. 확인하려는 것은 모델의 말이
아니라 DB 에 도달한 것이다.
"""

import asyncio
import logging
import uuid

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import SessionLocal, engine
from app.logging import REQUEST_ID_HEADER
from app.main import app
from app.models import Trace, UsageEvent, User
from app.services import auth, tracing
from app.services.generate import Answer
from app.services.ratelimit import query_limiter
from app.services.retrieve import HybridResult, RetrievedChunk
from app.services.tracing import SOURCE_QUERY, Stopwatch, TraceDraft
from app.services.upstream import UpstreamUnavailable

EMBED_DIM = settings.embed_dim


# --- 인프라 없이 도는 단위 테스트 ---------------------------------------


def test_http_failures_keep_the_detail_the_caller_already_saw():
    failure = tracing.describe_failure(
        HTTPException(status_code=503, detail="search unavailable")
    )
    assert failure.status_code == 503
    assert failure.reason == "search unavailable"


def test_a_crash_records_the_class_but_never_the_message():
    """메시지에는 질문 원문·파일 경로·업스트림 응답이 섞여 들어온다.

    전체 트레이스백은 이미 앱 로그에 있고, 이제 request_id 로 이 행과
    이어진다. DB 에 남길 이유가 없다.
    """
    secret = "질문 원문과 sk-live-0123456789 같은 것들"
    failure = tracing.describe_failure(RuntimeError(secret))
    assert failure.status_code == 500
    assert failure.reason == "RuntimeError"
    assert secret not in failure.reason


def test_a_client_that_hung_up_is_not_filed_as_a_server_fault():
    """499 는 진짜 상태 코드가 아니라 이 컬럼을 읽는 사람을 위한 표식이다.

    끊긴 연결을 500 으로 적으면 운영자가 있지도 않은 서버 오류를 쫓는다.
    """
    failure = tracing.describe_failure(asyncio.CancelledError())
    assert failure.status_code == tracing.CLIENT_CLOSED_REQUEST == 499
    assert failure.reason == "client disconnected"


def test_a_pathological_detail_cannot_blow_up_the_reason_column():
    """HTTPException.detail 은 Any 라 dict 도 긴 문자열도 올 수 있다."""
    failure = tracing.describe_failure(HTTPException(400, detail="x" * 5000))
    assert len(failure.reason) == tracing.REASON_MAX


class _BrokenSession:
    """flush 에서 터지고, 롤백에서 한 번 더 터지는 세션."""

    def __init__(self, rollback_exc: BaseException | None = None) -> None:
        self.rolled_back = 0
        self._rollback_exc = rollback_exc

    def add(self, _obj) -> None:
        pass

    def add_all(self, _objs) -> None:
        pass

    async def flush(self) -> None:
        raise ValueError("insert failed")

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        self.rolled_back += 1
        if self._rollback_exc is not None:
            raise self._rollback_exc


class _CancelledSession(_BrokenSession):
    async def flush(self) -> None:
        raise asyncio.CancelledError()


def test_a_rollback_that_throws_does_not_escape_the_recorder():
    """롤백이 실패를 대체하면 안 된다 — 실패한 트레이스가 실패한 질의가 된다.

    이전에는 ``await session.rollback()`` 이 except 절 안에 맨몸으로 있어서,
    롤백이 던지면(끊긴 커넥션이 흔한 경로다) 예외가 record() 를 그대로
    빠져나갔다 — 절대 안 던진다고 약속한 바로 그 함수에서.
    """
    session = _BrokenSession(rollback_exc=RuntimeError("connection is gone"))
    result = asyncio.run(
        tracing.record(session, TraceDraft(user_id=uuid.uuid4(), question="q", source=SOURCE_QUERY), Stopwatch())
    )
    assert result is None
    assert session.rolled_back == 1


def test_cancellation_cleans_the_session_and_still_propagates():
    """CancelledError 는 BaseException 이라 ``except Exception`` 이 못 봤다.

    그래서 취소된 요청은 롤백 없이 record() 를 빠져나갔고, 열린 트랜잭션을
    쥔 세션이 다음 사용자에게 넘어갔다. 정리는 하되 삼키지는 않는다 —
    취소를 삼키면 뜯겨 나가는 중인 요청이 멀쩡히 끝난 것처럼 보인다.
    """
    session = _CancelledSession()

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await tracing.record(
                session, TraceDraft(user_id=uuid.uuid4(), question="q", source=SOURCE_QUERY), Stopwatch()
            )

    asyncio.run(scenario())
    assert session.rolled_back == 1


# --- DB 가 필요한 통합 테스트 -------------------------------------------


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


needs_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


@pytest.fixture(autouse=True)
def _clean_limiter():
    query_limiter.reset()
    yield
    query_limiter.reset()


def _chunk(text_: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        page_from=1,
        page_to=1,
        content=text_,
        score=0.0,
    )


async def _drop_user(email: str) -> None:
    async with SessionLocal() as session:
        user = await auth.get_user_by_email(session, email)
        if user is not None:
            await session.delete(user)
            await session.commit()


async def _traces_of(email: str) -> list[Trace]:
    """이 사용자의 트레이스. 단계 행까지 미리 붙여서 돌려준다.

    세션이 닫힌 뒤에 읽으므로 eager load 가 아니면 detached 접근으로 터진다.
    """
    async with SessionLocal() as session:
        user = await auth.get_user_by_email(session, email)
        rows = (
            await session.execute(
                select(Trace)
                .options(selectinload(Trace.chunks))
                .where(Trace.user_id == user.id)
            )
        ).scalars().all()
        return list(rows)


async def _query_events_of(email: str) -> int:
    async with SessionLocal() as session:
        user = await auth.get_user_by_email(session, email)
        rows = (
            await session.execute(
                select(UsageEvent).where(
                    UsageEvent.user_id == user.id, UsageEvent.kind == "query"
                )
            )
        ).scalars().all()
        return len(rows)


def _stub_healthy_pipeline(monkeypatch, candidates):
    """실패 지점만 바꿔 끼울 수 있게, 나머지 단계는 전부 성공시킨다."""
    from app.routers import query as rq
    from app.services import pipeline as qr

    async def fake_embed_full(question):
        return [0.02] * EMBED_DIM, object()

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        return HybridResult(candidates, [c.chunk_id for c in candidates], [])

    async def fake_rerank(question, texts):
        return [(i, 5.0 - i) for i in range(len(texts))]

    async def fake_answer(question, chunks, **kwargs):
        return Answer(
            answer="답변 [p.1].", refused=False, citations=[], tokens_in=1, tokens_out=1
        )

    monkeypatch.setattr(qr.embeddings, "embed_query_full", fake_embed_full)
    monkeypatch.setattr(qr.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qr.rerank, "rerank", fake_rerank)
    monkeypatch.setattr(rq.generate, "answer_question", fake_answer)


@needs_db
def test_a_dead_embedding_server_leaves_a_row(monkeypatch):
    """이 테스트가 원래의 불만 그 자체다.

    임베딩 서버가 죽으면 503 이 나가고 DB 에는 아무것도 안 남았다. 장애
    중에 운영자가 가장 필요로 하는 숫자가 없는 상태다. 요청 id 까지 같이
    확인한다 — 이 행이 Caddy·uvicorn·앱 로그와 이어지는 유일한 열쇠다.
    """
    _stub_healthy_pipeline(monkeypatch, [_chunk("c0")])

    from app.services import pipeline as qr

    async def dead_embedder(question):
        raise UpstreamUnavailable("embedding", ConnectionError("refused"))

    monkeypatch.setattr(qr.embeddings, "embed_query_full", dead_embedder)

    async def scenario():
        email = f"fail-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            resp = await client.post("/query", json={"question": "관리비 얼마?"})
            out = (resp.status_code, resp.headers.get(REQUEST_ID_HEADER))

        rows = await _traces_of(email)
        snapshot = [
            (r.question, r.status_code, r.error, r.answer, r.request_id) for r in rows
        ]
        await _drop_user(email)
        return out, snapshot

    (status, request_id), rows = run_async(scenario)

    assert status == 503
    assert len(rows) == 1, "실패한 질의도 정확히 한 행을 남긴다"
    question, status_code, error, answer, recorded_id = rows[0]
    assert question == "관리비 얼마?"
    assert status_code == 503
    assert error == "search unavailable"
    assert answer is None
    # 응답 헤더의 id 와 DB 의 id 가 같아야 로그와 DB 가 이어진다.
    assert recorded_id == request_id and recorded_id


@needs_db
def test_a_crash_is_recorded_as_500_without_leaking_the_message(monkeypatch):
    _stub_healthy_pipeline(monkeypatch, [_chunk("c0")])

    from app.routers import query as rq

    secret = "Bearer sk-live-must-not-be-stored"

    async def exploding_answer(question, chunks, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(rq.generate, "answer_question", exploding_answer)

    async def scenario():
        email = f"boom-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            try:
                await client.post("/query", json={"question": "q"})
            except RuntimeError:
                pass  # ASGITransport 는 500 을 만들지 않고 그대로 올려보낸다

        snapshot = [
            (r.status_code, r.error, len(r.chunks)) for r in await _traces_of(email)
        ]
        await _drop_user(email)
        return snapshot

    rows = run_async(scenario)

    assert len(rows) == 1
    status_code, error, chunk_rows = rows[0]
    assert status_code == 500
    assert error == "RuntimeError"
    assert secret not in (error or "")
    # 생성 단계에서 터졌으므로 검색은 이미 답을 찾아둔 상태였다. 그 사실이
    # 남아야 "검색이 못 찾았다"와 "찾았는데 생성이 죽었다"가 갈린다.
    assert chunk_rows > 0


@needs_db
def test_recording_the_failure_does_not_charge_the_quota(monkeypatch):
    """실패 기록이 예약 정리를 밀어내면 안 된다.

    실패 경로에는 ``release_reservation`` 이 얽혀 있다. 기록을 그 앞에
    끼워 넣으면 기록이 던지는 날 예약이 그대로 남아, 일어나지 않은 질의가
    사용자의 하루치를 먹는다. 그래서 정리가 먼저다 — 이 테스트가 그 순서를
    고정한다.
    """
    _stub_healthy_pipeline(monkeypatch, [_chunk("c0")])

    from app.services import pipeline as qr

    async def dead_embedder(question):
        raise UpstreamUnavailable("embedding", ConnectionError("refused"))

    monkeypatch.setattr(qr.embeddings, "embed_query_full", dead_embedder)

    async def scenario():
        email = f"quota-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            resp = await client.post("/query", json={"question": "q"})
            usage = (await client.get("/usage")).json()

        events = await _query_events_of(email)
        traces = len(await _traces_of(email))
        await _drop_user(email)
        return resp.status_code, usage["queries_today"], events, traces

    status, queries_today, events, traces = run_async(scenario)

    assert status == 503
    assert traces == 1, "실패는 기록된다"
    assert events == 0, "그러나 예약은 풀려 있어야 한다"
    assert queries_today == 0


@needs_db
def test_rejections_at_the_door_are_not_traced(monkeypatch):
    """레이트리밋 429 는 기록하지 않는다 — 판단이고, 근거가 있다.

    거절을 싸게 만드는 것이 레이트리밋의 존재 이유인데 거절마다 행을
    하나씩 심으면 재시도 루프에 빠진 클라이언트 하나가 무제한의 DB 쓰기가
    된다. 게다가 이미 세어진다: 요청 id 가 찍힌 액세스 로그에 남는다.
    """
    _stub_healthy_pipeline(monkeypatch, [_chunk("c0")])

    from app.services import pipeline as qr

    # 버킷을 손으로 소진시키는 대신 한도 자체를 닫는다 — 이 테스트가 보려는
    # 것은 토큰 버킷의 산수가 아니라 "문 앞에서 튕긴 요청은 기록하지
    # 않는다"는 판단이다.
    monkeypatch.setattr(qr.query_limiter, "allow", lambda key: False)

    async def scenario():
        email = f"limit-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            resp = await client.post("/query", json={"question": "q"})

        traces = len(await _traces_of(email))
        events = await _query_events_of(email)
        await _drop_user(email)
        return resp.status_code, traces, events

    status, traces, events = run_async(scenario)
    assert status == 429
    assert traces == 0
    assert events == 0  # 예약도 없다: 아무것도 시작되지 않았다


@needs_db
def test_the_stream_path_also_refuses_to_trace_a_doorstep_rejection(monkeypatch):
    """같은 판단을 SSE 경로에서 확인한다 — 여기가 진짜 경계선이다.

    ``/query`` 는 enforce_limits 가 try 바깥이라 구조상 기록에 닿지도
    않는다. 반면 스트리밍 쪽은 enforce_limits 가 실패 핸들러 **안**에서
    돌기 때문에, "어디까지 기록할 것인가"를 실제로 정하는 것은 구조가
    아니라 ``QueryRunner._trace_pending`` 이다. 그 판단이 사라지면
    깨지는 테스트는 이쪽이다.
    """
    _stub_healthy_pipeline(monkeypatch, [_chunk("c0")])

    from app.services import pipeline as qr

    monkeypatch.setattr(qr.query_limiter, "allow", lambda key: False)

    async def scenario():
        email = f"slimit-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            created = await client.post("/conversations", json={})
            streamed = await client.post(
                f"/conversations/{created.json()['id']}/query",
                json={"question": "질문"},
            )
            body = streamed.text

        traces = len(await _traces_of(email))
        await _drop_user(email)
        return body, traces

    body, traces = run_async(scenario)

    assert '"status": 429' in body  # 클라이언트는 거절을 받는다
    assert traces == 0  # 그러나 DB 에는 아무것도 안 쌓인다


@needs_db
def test_a_failed_stream_is_recorded_too(monkeypatch):
    """SSE 경로도 같은 행을 남긴다.

    이쪽은 헤더가 이미 200 으로 나간 뒤라 상태 코드가 응답에 없다 —
    액세스 로그에는 200 만 찍힌다. 그래서 DB 의 행이 이 경로에서는 더
    중요하다.
    """
    _stub_healthy_pipeline(monkeypatch, [_chunk("c0")])

    from app.services import pipeline as qr

    async def dead_embedder(question):
        raise UpstreamUnavailable("embedding", ConnectionError("refused"))

    monkeypatch.setattr(qr.embeddings, "embed_query_full", dead_embedder)

    async def scenario():
        email = f"stream-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            created = await client.post("/conversations", json={})
            cid = created.json()["id"]
            streamed = await client.post(
                f"/conversations/{cid}/query", json={"question": "질문"}
            )
            body = streamed.text

        rows = await _traces_of(email)
        events = await _query_events_of(email)
        snapshot = [(r.status_code, r.error, r.request_id) for r in rows]
        await _drop_user(email)
        return streamed.status_code, body, snapshot, events

    http_status, body, rows, events = run_async(scenario)

    assert http_status == 200  # 스트림은 이미 열렸다
    assert '"status": 503' in body  # 클라이언트는 in-band 로 받는다
    assert len(rows) == 1, "그리고 서버에도 남는다"
    status_code, error, request_id = rows[0]
    assert status_code == 503
    assert error == "search unavailable"
    assert request_id  # 스트리밍 제너레이터 안에서도 요청 id 를 잃지 않는다
    assert events == 0, "실패한 턴은 쿼터를 먹지 않는다"


# --- 서버 로그: 비스트리밍 경로도 이유를 말한다 -------------------------
#
# 실패한 ``/query`` 는 트레이스는커녕 **앱 로그도 0줄**이었다. 라우터가
# 로깅 없이 그대로 re-raise 했기 때문이다. 같은 저장소의 SSE 경로는
# 왜 남겨야 하는지까지 주석으로 적어두고 남기고 있었는데, 그 교훈이 한쪽
# 경로에만 반영돼 있었다.


def test_a_client_that_left_gets_no_traceback(caplog):
    """끊긴 탭마다 트레이스백을 찍으면 진짜 크래시가 그 밑에 묻힌다."""
    from app.routers import query as rq

    with caplog.at_level(logging.INFO, logger="app.routers.query"):
        rq._log_failure(asyncio.CancelledError())

    record = caplog.records[-1]
    assert record.levelno == logging.INFO
    assert record.exc_info is None


@needs_db
def test_a_failed_query_says_why_on_the_server(monkeypatch, caplog):
    """503 의 이유가 서버에 남는다 — 액세스 로그에는 숫자밖에 없다."""
    _stub_healthy_pipeline(monkeypatch, [_chunk("c0")])

    from app.services import pipeline as qr

    async def dead_embedder(question):
        raise UpstreamUnavailable("embedding", ConnectionError("refused"))

    monkeypatch.setattr(qr.embeddings, "embed_query_full", dead_embedder)

    async def scenario():
        email = f"log-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            await client.post("/query", json={"question": "q"})
        await _drop_user(email)

    with caplog.at_level(logging.INFO, logger="app.routers.query"):
        run_async(scenario)

    lines = [r for r in caplog.records if r.name == "app.routers.query"]
    assert lines, "실패한 질의가 서버에 한 줄도 남기지 않았다"
    message = lines[-1].getMessage()
    assert "503" in message
    assert "search unavailable" in message
    # 이 줄에 요청 id 가 찍히는지는 여기서 재확인하지 않는다. 그것을 붙이는
    # 것은 앱 로그 포맷/레코드 팩토리(app/logging.py)이고, 그 설치는
    # lifespan 에서 일어나는데 ASGITransport 는 lifespan 을 돌리지 않는다 —
    # 여기서 단언하면 로깅 설정이 아니라 테스트 클라이언트의 성질을 재게
    # 된다. 대신 DB 쪽 상관관계는 위의 트레이스 테스트가 응답 헤더의 id 와
    # 행의 id 를 맞춰보는 것으로 확인한다.


@needs_db
def test_a_rejection_at_the_door_is_logged_even_though_it_is_not_traced(
    monkeypatch, caplog
):
    """어느 한도에 막혔는지는 액세스 로그가 말해주지 못한다.

    이 라우터가 던지는 거절 셋(버스트 한도 429, 일일 쿼터 429, 미인증
    403)은 본문의 detail 로만 갈린다.
    """
    _stub_healthy_pipeline(monkeypatch, [_chunk("c0")])

    from app.services import pipeline as qr

    monkeypatch.setattr(qr.query_limiter, "allow", lambda key: False)

    async def scenario():
        email = f"logrej-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            resp = await client.post("/query", json={"question": "q"})
            traces = len(await _traces_of(email))
        await _drop_user(email)
        return resp.status_code, traces

    with caplog.at_level(logging.INFO, logger="app.routers.query"):
        status, traces = run_async(scenario)

    assert status == 429
    lines = [r.getMessage() for r in caplog.records if r.name == "app.routers.query"]
    assert any("429" in m and "rate limit" in m for m in lines)
    assert traces == 0  # 로그는 남기고 행은 남기지 않는다 — 별개의 판단이다


@needs_db
def test_a_successful_query_is_still_not_marked_as_failed(monkeypatch):
    """NULL 이 "실패하지 않았다"라는 뜻이 되려면 성공 경로가 조용해야 한다."""
    _stub_healthy_pipeline(monkeypatch, [_chunk("c0")])

    async def scenario():
        email = f"ok-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/auth/signup", json={"email": email, "password": "password123"}
            )
            resp = await client.post("/query", json={"question": "q"})

        rows = await _traces_of(email)
        snapshot = [(r.status_code, r.error, r.request_id) for r in rows]
        await _drop_user(email)
        return resp.status_code, snapshot

    status, rows = run_async(scenario)
    assert status == 200
    assert len(rows) == 1
    status_code, error, request_id = rows[0]
    assert status_code is None and error is None
    # 성공한 행에도 요청 id 는 붙는다 — 로그와의 연결은 실패 전용이 아니다.
    assert request_id
