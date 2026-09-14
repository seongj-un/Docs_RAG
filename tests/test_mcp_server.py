"""MCP server surface (M7 W2). Requires Postgres.

Integration tests on purpose, for the same reason ``test_isolation.py`` is:
what this milestone actually promises is that an agent reaches exactly the
documents its session's owner uploaded, and that promise is kept by a SQL join
and a token verifier that reads a real ``sessions`` row. Mocking either one
would leave the promise untested.

Two things the suite works around, both properties of the SDK rather than of
this repo:

*The session manager runs once per server instance.* ``run()`` raises on a
second call, so each test builds its own server through the same factories
``main.py`` uses rather than sharing the process-wide one. The real app's mount
is still checked — by ``test_mount_does_not_shadow_existing_routes``, which
needs no session manager because it never reaches the MCP app.

*httpx's ASGI transport does not run lifespan.* The existing suite already
relies on that (migrations are applied out of band); here it means the test has
to enter ``session_manager.run()`` itself, which is precisely what ``main.py``'s
lifespan does in production.

Model servers are absent in CI, so embedding and reranking are stubbed exactly
as ``test_query_pipeline.py`` stubs them. Retrieval itself is not stubbed: the
pgvector query is the thing under test.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from starlette.routing import Route

from app.config import settings
from app.db import SessionLocal, engine
from app.mcp.server import MCP_HTTP_METHODS, build_mcp_app, build_mcp_server
from app.models import Chunk, Document, Session, User
from app.services import auth, embeddings, pipeline, rerank
from app.services.tracing import SOURCE_MCP_SEARCH

EMBED_DIM = settings.embed_dim
PROTOCOL_VERSION = "2026-07-28"

# Host 헤더가 여기서 나온다. 기본 허용 목록이 로컬호스트뿐이므로(설정을 비워
# 두면 그렇게 되도록 골랐다) 테스트도 로컬호스트로 말한다 — 덕분에 "기본값이
# 로컬 개발에서 동작한다"는 주장 자체가 매 실행 검증된다. 기존 테스트들이 쓰는
# http://test 를 그대로 쓰면 421 이 난다.
BASE_URL = "http://localhost:8000"


def run_async(coro_fn):
    """Run one coroutine, always disposing the engine afterwards.

    asyncpg 커넥션은 자기를 만든 루프에 묶여 있어서, 풀에 남은 커넥션이 다음
    테스트의 루프로 새어 들어가면 안 된다 — test_isolation.py 와 같은 이유로
    같은 모양을 쓴다.
    """

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


# --- MCP 클라이언트 -------------------------------------------------------
#
# 서버는 modern era(2026-07-28) 요청을 엄격하게 검증한다: params._meta 에
# 프로토콜 버전과 클라이언트 능력이 둘 다 있어야 하고, Mcp-Method 헤더가 본문의
# method 와, tools/call 이면 Mcp-Name 이 params.name 과 일치해야 한다. 하나라도
# 어긋나면 -32020/-32602 로 떨어진다. 진짜 클라이언트가 보내는 것과 같은 모양을
# 여기 한 번만 만들어 둔다.


def _body(method: str, params: dict | None = None) -> dict:
    merged = dict(params or {})
    merged["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": merged}


def _headers(method: str, token: str | None, name: str | None = None) -> dict:
    headers = {
        # json_response=False 라 서버는 SSE 로 답할 수 있다. 둘 다 받겠다고
        # 말하지 않으면 406 이 난다.
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _call(client: AsyncClient, method: str, token, params=None, name=None):
    return await client.post(
        settings.mcp_path,
        json=_body(method, params),
        headers=_headers(method, token, name),
    )


async def _search(client: AsyncClient, token: str, **arguments):
    return await _call(
        client,
        "tools/call",
        token,
        {"name": "search_documents", "arguments": arguments},
        name="search_documents",
    )


@asynccontextmanager
async def mcp_client():
    """A live MCP endpoint, wired the way ``main.py`` wires the real one.

    Same factories, same mount position, same lifespan duty — a fresh instance
    only because ``session_manager.run()`` refuses to be called twice.
    """
    server = build_mcp_server()
    host = FastAPI()

    @host.get("/health")
    async def _health() -> dict[str, str]:  # pragma: no cover - 배선 증인용
        return {"status": "ok"}

    # main.py 와 **같은 방식**으로 건다. 여기만 Mount 로 두면 프로덕션 배선이
    # 아닌 것을 테스트하게 된다.
    host.router.routes.append(
        Route(settings.mcp_path, endpoint=build_mcp_app(server), methods=MCP_HTTP_METHODS)
    )

    async with server.session_manager.run():
        transport = ASGITransport(app=host)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            yield client


# --- 픽스처 --------------------------------------------------------------


async def _make_user(db, label: str, *, verified: bool = True) -> User:
    user = User(
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=auth.hash_password("password123"),
        # 미인증 계정은 계정 수명 전체로 5회만 검색할 수 있다. 쿼터를 세는지
        # 확인하는 테스트가 아니라면 인증된 상태가 기본이어야, 한도가 아니라
        # 보려던 것이 검증된다.
        email_verified_at=datetime.now(timezone.utc) if verified else None,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_session(db, user: User, *, ttl_days: int = 14) -> uuid.UUID:
    row = Session(
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=ttl_days),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row.id


async def _make_chunk(db, user: User, content: str, *, page: int = 1) -> Chunk:
    doc = Document(
        user_id=user.id,
        filename=f"{content[:8]}.pdf",
        mime_type="application/pdf",
        status="ready",
        num_pages=page,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    chunk = Chunk(
        document_id=doc.id,
        chunk_index=0,
        page_from=page,
        page_to=page,
        content=content,
        token_count=len(content.split()),
        embed_model="test",
        embedding=[0.1] * EMBED_DIM,
    )
    db.add(chunk)
    await db.commit()
    await db.refresh(chunk)
    return chunk


async def _drop_users(*users: User) -> None:
    async with SessionLocal() as db:
        for user in users:
            row = await db.get(User, user.id)
            if row is not None:
                # 문서·청크·세션·usage·트레이스가 전부 사용자에서 캐스케이드된다.
                await db.delete(row)
        await db.commit()


@pytest.fixture(autouse=True)
def stub_models(monkeypatch):
    """임베딩·리랭크 서버는 CI 에 없다 — 기존 테스트들과 같은 방식으로 대체한다.

    리트리벌 자체는 대체하지 않는다. 소유자 조인이 든 진짜 pgvector 쿼리가
    이 파일이 증명하려는 바로 그것이라서다.
    """

    async def fake_embed_query(text: str) -> list[float]:
        return [0.1] * EMBED_DIM

    monkeypatch.setattr(embeddings, "embed_query", fake_embed_query)
    monkeypatch.setattr(pipeline.embeddings, "embed_query", fake_embed_query)
    # 하이브리드 경로는 sparse 벡터와 리랭커가 둘 다 필요하다. 기본을 dense 로
    # 내려 두면 테스트가 진짜 SQL 을 지나가면서도 스텁이 하나로 끝난다 —
    # 하이브리드 쪽 순서·컷오프는 test_query_pipeline.py 가 이미 증명한다.
    monkeypatch.setattr(settings, "hybrid_enabled", False)

    async def unreachable_rerank(query, texts):  # pragma: no cover - 방어용
        raise AssertionError("dense path must not call the reranker")

    monkeypatch.setattr(rerank, "rerank", unreachable_rerank)


# --- 인증 ----------------------------------------------------------------


def test_missing_token_is_401():
    async def scenario():
        async with mcp_client() as client:
            return await _call(client, "tools/list", None)

    assert run_async(scenario).status_code == 401


def test_malformed_token_is_401():
    """UUID 가 아닌 토큰은 DB 를 건드리지도 않고 거절된다."""

    async def scenario():
        async with mcp_client() as client:
            return await _call(client, "tools/list", "not-a-session-id")

    assert run_async(scenario).status_code == 401


def test_unknown_session_is_401():
    async def scenario():
        async with mcp_client() as client:
            return await _call(client, "tools/list", str(uuid.uuid4()))

    assert run_async(scenario).status_code == 401


def test_expired_session_is_401_and_is_deleted():
    """만료는 쿠키 경로와 같은 의미여야 한다 — 거절되고, 행이 사라진다."""

    async def scenario():
        async with SessionLocal() as db:
            user = await _make_user(db, "mcp-exp")
            row = Session(
                user_id=user.id,
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            session_id = row.id

        async with mcp_client() as client:
            response = await _call(client, "tools/list", str(session_id))

        async with SessionLocal() as db:
            still_there = await db.get(Session, session_id)

        await _drop_users(user)
        return response.status_code, still_there

    status, still_there = run_async(scenario)
    assert status == 401
    # auth.get_session_user 가 만료 행을 지운다. MCP 가 그 경로를 그대로
    # 타는지가 요점 — 따로 구현했다면 행이 남아 있을 것이다.
    assert still_there is None


def test_valid_session_reaches_the_tool():
    async def scenario():
        async with SessionLocal() as db:
            user = await _make_user(db, "mcp-ok")
            session_id = await _make_session(db, user)
            chunk = await _make_chunk(db, user, "인공지능기본법의 목적은 무엇인가", page=3)

        async with mcp_client() as client:
            response = await _search(client, str(session_id), question="목적이 무엇인가")

        await _drop_users(user)
        return response, chunk

    response, chunk = run_async(scenario)
    assert response.status_code == 200

    result = response.json()["result"]
    assert result["isError"] is False
    hits = result["structuredContent"]["hits"]
    assert [h["chunk_id"] for h in hits] == [str(chunk.id)]

    hit = hits[0]
    # 에이전트가 인용을 만들 수 있어야 한다 — 본문·문서·쪽 범위·점수가 전부.
    assert hit["content"] == "인공지능기본법의 목적은 무엇인가"
    assert hit["document_id"] == str(chunk.document_id)
    assert hit["page_from"] == 3
    assert hit["page_to"] == 3
    assert isinstance(hit["score"], float)
    assert result["structuredContent"]["searched_document_id"] is None


# --- 테넌트 격리 (타협 불가) ----------------------------------------------


def test_another_users_chunks_never_appear():
    """M7 의 핵심 약속. 같은 질문에 각자 자기 것만 나와야 한다."""

    async def scenario():
        async with SessionLocal() as db:
            alice = await _make_user(db, "mcp-alice")
            bob = await _make_user(db, "mcp-bob")
            alice_session = await _make_session(db, alice)
            bob_session = await _make_session(db, bob)
            alice_chunk = await _make_chunk(db, alice, "앨리스의 비밀 문서 내용")
            await _make_chunk(db, bob, "밥의 문서 내용")

        async with mcp_client() as client:
            # 밥이 앨리스의 문서 본문을 그대로 물어봐도,
            bob_hits = await _search(client, str(bob_session), question="앨리스의 비밀 문서 내용")
            # 앨리스의 document_id 로 콕 집어도.
            bob_scoped = await _search(
                client,
                str(bob_session),
                question="앨리스의 비밀 문서 내용",
                document_id=str(alice_chunk.document_id),
            )
            alice_hits = await _search(client, str(alice_session), question="비밀")

        await _drop_users(alice, bob)
        return bob_hits, bob_scoped, alice_hits, alice_chunk

    bob_hits, bob_scoped, alice_hits, alice_chunk = run_async(scenario)

    bob_contents = [
        h["content"] for h in bob_hits.json()["result"]["structuredContent"]["hits"]
    ]
    assert bob_contents == ["밥의 문서 내용"]
    assert "앨리스의 비밀 문서 내용" not in bob_contents

    # 남의 document_id 는 "없는 것"이다 — 존재를 확인해 주는 403 이 아니라,
    # 기존 라우터와 같은 의미의 not found.
    scoped = bob_scoped.json()["result"]
    assert scoped["isError"] is True
    message = scoped["content"][0]["text"].lower()
    assert "no such document" in message
    # 앨리스의 문서가 **존재한다**는 사실이 어떤 형태로도 새어 나가면 안 된다.
    assert "앨리스" not in message and str(alice_chunk.document_id) not in message

    alice_contents = [
        h["content"] for h in alice_hits.json()["result"]["structuredContent"]["hits"]
    ]
    assert alice_contents == ["앨리스의 비밀 문서 내용"]


def test_own_document_scope_narrows_without_widening():
    async def scenario():
        async with SessionLocal() as db:
            user = await _make_user(db, "mcp-scope")
            session_id = await _make_session(db, user)
            first = await _make_chunk(db, user, "첫 번째 문서의 내용")
            await _make_chunk(db, user, "두 번째 문서의 내용")

        async with mcp_client() as client:
            response = await _search(
                client,
                str(session_id),
                question="내용",
                document_id=str(first.document_id),
            )

        await _drop_users(user)
        return response, first

    response, first = run_async(scenario)
    result = response.json()["result"]["structuredContent"]
    assert [h["content"] for h in result["hits"]] == ["첫 번째 문서의 내용"]
    assert result["searched_document_id"] == str(first.document_id)


# --- 리스트/디스커버리 응답 ------------------------------------------------


def test_tools_list_carries_cache_hints():
    """W2 완료 항목: 리스트 응답에 ttlMs · cacheScope 가 실린다."""

    async def scenario():
        async with SessionLocal() as db:
            user = await _make_user(db, "mcp-list")
            session_id = await _make_session(db, user)

        async with mcp_client() as client:
            response = await _call(client, "tools/list", str(session_id))

        await _drop_users(user)
        return response

    result = run_async(scenario).json()["result"]
    assert result["ttlMs"] == settings.mcp_tools_cache_ttl_ms
    # public 이 되는 순간 공유 캐시가 한 사용자의 목록을 다른 사용자에게 준다.
    # 지금은 목록이 정적이라 새어 나갈 것이 없지만, 정적이 아니게 되는 날
    # 아무 오류 없이 새기 시작한다 — 근거는 app/mcp/server.py 의 _cache_hints.
    assert result["cacheScope"] == "private"

    names = [t["name"] for t in result["tools"]]
    # W2 는 툴 하나만 노출한다. 늘어나면 그것은 결정이지 사고가 아니어야 한다.
    assert names == ["search_documents"]

    tool = result["tools"][0]
    # description= 인자가 docstring 을 이긴다 — 모델이 보는 것은 W4 가 갈아끼울
    # descriptions.py 의 상수다. 그게 실제로 실려 나가는지 확인한다.
    assert "returns ranked passages, not an answer" in tool["description"].lower()
    assert set(tool["inputSchema"]["properties"]) == {
        "question",
        "document_id",
        "max_results",
    }
    # Context 로 주석된 파라미터는 스키마에 새어 나오면 안 된다.
    assert "ctx" not in tool["inputSchema"]["properties"]


def test_server_discover_responds():
    async def scenario():
        async with SessionLocal() as db:
            user = await _make_user(db, "mcp-disc")
            session_id = await _make_session(db, user)

        async with mcp_client() as client:
            response = await _call(client, "server/discover", str(session_id))

        await _drop_users(user)
        return response

    result = run_async(scenario).json()["result"]
    assert PROTOCOL_VERSION in result["supportedVersions"]
    assert "tools" in result["capabilities"]
    # 에이전트가 읽을 안내문. 이 서버가 답을 쓰지 않는다는 사실이 여기 있어야
    # 한다 — 그걸 모르면 에이전트는 답을 돌려주는 툴을 찾다가 포기한다.
    assert "never a written answer" in result["instructions"]


# --- 실패 경로 ------------------------------------------------------------


def test_empty_question_is_a_validation_error():
    async def scenario():
        async with SessionLocal() as db:
            user = await _make_user(db, "mcp-empty")
            session_id = await _make_session(db, user)

        async with mcp_client() as client:
            response = await _search(client, str(session_id), question="")

        await _drop_users(user)
        return response

    result = run_async(scenario).json()["result"]
    # pydantic 검증 실패는 "예상한 실패"라서 프로토콜 오류(-32602)가 아니라
    # isError 결과로 돌아온다 — 즉 모델이 메시지를 읽고 스스로 고칠 수 있다.
    # 연결을 끊는 대신 고쳐 쓰게 하는 쪽이 맞다.
    assert result["isError"] is True
    assert "at least 1 character" in result["content"][0]["text"]


def test_quota_exhaustion_is_a_tool_error(monkeypatch):
    """쿼터를 센다는 사실 자체가 이 툴의 설계 판단이다 — 증인을 남긴다."""
    monkeypatch.setattr(settings, "quota_queries_per_day", 1)

    async def scenario():
        async with SessionLocal() as db:
            user = await _make_user(db, "mcp-quota")
            session_id = await _make_session(db, user)
            await _make_chunk(db, user, "쿼터 시험용 문서")

        async with mcp_client() as client:
            first = await _search(client, str(session_id), question="문서")
            second = await _search(client, str(session_id), question="문서")

        async with SessionLocal() as db:
            events = (
                await db.execute(
                    text(
                        "SELECT count(*) FROM usage_events "
                        "WHERE user_id = :uid AND kind = 'query'"
                    ).bindparams(uid=user.id)
                )
            ).scalar_one()

        await _drop_users(user)
        return first, second, events

    first, second, events = run_async(scenario)
    assert first.json()["result"]["isError"] is False

    refused = second.json()["result"]
    assert refused["isError"] is True
    # 모델이 읽고 행동을 바꿔야 하는 실패라 ToolError 로 나간다 — 일반 예외면
    # 이유가 지워진 채 "Error executing tool ..." 만 도착해 무한히 재시도한다.
    assert "quota" in refused["content"][0]["text"].lower()

    # 성공한 한 번만 셈에 남는다. 거절된 쪽의 예약이 풀리지 않았다면 2가 된다.
    assert events == 1


def test_unowned_scope_releases_the_quota_reservation():
    """실패한 검색은 쿼터를 쓰지 않아야 한다 — routers/query.py 와 같은 약속."""

    async def scenario():
        async with SessionLocal() as db:
            user = await _make_user(db, "mcp-release")
            session_id = await _make_session(db, user)

        async with mcp_client() as client:
            response = await _search(
                client,
                str(session_id),
                question="남의 문서",
                document_id=str(uuid.uuid4()),
            )

        async with SessionLocal() as db:
            events = (
                await db.execute(
                    text(
                        "SELECT count(*) FROM usage_events WHERE user_id = :uid"
                    ).bindparams(uid=user.id)
                )
            ).scalar_one()

        await _drop_users(user)
        return response, events

    response, events = run_async(scenario)
    assert response.json()["result"]["isError"] is True
    assert events == 0


def test_foreign_host_header_is_rejected():
    """DNS 리바인딩 보호가 정말로 켜져 있는지. 꺼져 있으면 증상이 없다.

    **유효한 세션으로** 찌른다. SDK 는 베어러 인증을 Host 검사보다 먼저 돌려서,
    토큰 없이 보내면 401 이 먼저 나오고 보호가 꺼져 있어도 테스트가 통과해
    버린다 — 검사하려던 것을 검사하지 않는 테스트가 된다.
    """

    async def scenario():
        async with SessionLocal() as db:
            user = await _make_user(db, "mcp-host")
            session_id = await _make_session(db, user)

        async with mcp_client() as client:
            response = await client.post(
                settings.mcp_path,
                json=_body("tools/list"),
                headers={
                    **_headers("tools/list", str(session_id)),
                    "Host": "evil.example.com",
                },
            )

        await _drop_users(user)
        return response

    assert run_async(scenario).status_code == 421


# --- 관측성 --------------------------------------------------------------


def test_search_is_traced_for_w4_l3_metrics():
    """W4/W5 가 이 행에서 L3 지표를 뽑는다. 지금 안 남기면 그때는 늦다."""

    async def scenario():
        async with SessionLocal() as db:
            user = await _make_user(db, "mcp-trace")
            session_id = await _make_session(db, user)
            chunk = await _make_chunk(db, user, "트레이스가 남아야 하는 문서")

        async with mcp_client() as client:
            await _search(client, str(session_id), question="트레이스")

        async with SessionLocal() as db:
            trace = (
                await db.execute(
                    text(
                        "SELECT id, question, source, llm_model, answer, "
                        "tokens_in, tokens_out, retrieve_ms FROM traces "
                        "WHERE user_id = :uid"
                    ).bindparams(uid=user.id)
                )
            ).mappings().all()
            stages = (
                await db.execute(
                    text(
                        "SELECT stage, chunk_id FROM trace_chunks "
                        "WHERE trace_id = :tid"
                    ).bindparams(tid=trace[0]["id"])
                )
            ).mappings().all()

        await _drop_users(user)
        return trace, stages, chunk

    traces, stages, chunk = run_async(scenario)
    assert len(traces) == 1
    row = traces[0]
    assert row["question"] == "트레이스"
    assert row["retrieve_ms"] is not None

    # W4/W5 가 에이전트 트래픽을 고르는 조건절. 이제 **선언된 표식**이다
    # (마이그레이션 0012) — 생성 없는 네 번째 소비자가 생겨도 그 소비자는
    # 자기 source 를 갖게 되므로 이 단언은 그대로 참이다. 예전의 파생 표식
    # (llm_model IS NULL)은 바로 그 상황에서 조용히 거짓이 됐을 것이다.
    assert row["source"] == SOURCE_MCP_SEARCH

    # 생성이 없었다는 사실은 별개로 남는다 — source 가 "누가 불렀나"이고
    # 이쪽은 "무엇을 했나"다. 둘을 한 컬럼으로 합치지 않은 이유이기도 하다.
    assert row["llm_model"] is None
    assert row["answer"] is None
    assert row["tokens_in"] == 0 and row["tokens_out"] == 0

    assert {s["stage"] for s in stages} == {"dense"}
    assert str(stages[0]["chunk_id"]) == str(chunk.id)


# --- 마운트 --------------------------------------------------------------


def test_mount_does_not_shadow_existing_routes():
    """MCP 앱을 거는 방식이 나머지 API 의 의미를 바꾸지 않는지.

    실제 ``app.main.app`` 을 쓴다. MCP 서브앱에 닿지 않는 요청들이라 세션
    매니저가 필요 없고, 그래서 여기서만 진짜 배선을 검사할 수 있다.

    ⚠️ **틀린 메서드 케이스가 이 테스트의 핵심이다.** 맞는 메서드로만 찌르면
    캐치올 ``mount("/")`` 로 되돌려도 전부 초록이다 — 경로가 가려지지는 않기
    때문이다. 실제로 깨지는 것은 405 다: Starlette 는 경로가 맞고 메서드가
    틀리면 Match.PARTIAL 로 기억해 두었다가 full match 가 끝까지 없을 때에만
    405 를 내는데, ``Mount("/")`` 는 무엇이든 full match 라 그 fallback 이
    영영 안 쓰이고 서브앱의 404 가 이긴다. 그래서 앱 전체에서 405 가
    사라진다. 아래 단언들이 그 회귀를 잡는 자리다.
    """

    async def scenario():
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            return {
                "health": await client.get("/health"),
                # 인증이 없으니 401 — 404 나 405 였다면 라우트가 삼켜진 것이다.
                "query": await client.post("/query", json={"question": "x"}),
                "documents": await client.get("/documents"),
                "usage": await client.get("/usage"),
                "openapi": await client.get("/openapi.json"),
                # --- 틀린 메서드: 405 가 405 로 남아야 한다 ---
                "query_get": await client.get("/query"),
                "login_get": await client.get("/auth/login"),
                "health_post": await client.post("/health"),
                "health_delete": await client.delete("/health"),
                # --- 없는 경로는 여전히 404 ---
                "missing": await client.get("/definitely-not-a-route"),
                # --- /mcp 자체도 안 쓰는 메서드는 405(Allow: POST) ---
                "mcp_get": await client.get(settings.mcp_path),
                "mcp_patch": await client.patch(settings.mcp_path),
            }

    r = run_async(scenario)
    assert r["health"].status_code == 200
    assert r["health"].json() == {"status": "ok"}
    assert r["query"].status_code == 401
    assert r["documents"].status_code == 401
    assert r["usage"].status_code == 401
    assert r["openapi"].status_code == 200

    # 캐치올 마운트로 되돌리면 여기가 전부 404 로 바뀌며 먼저 빨개진다.
    assert r["query_get"].status_code == 405
    assert r["login_get"].status_code == 405
    assert r["health_post"].status_code == 405
    assert r["health_delete"].status_code == 405

    # 반대로 진짜 없는 경로까지 405 가 되면 안 된다 — 405 를 살리겠다고
    # 404 를 잃으면 같은 종류의 거짓말이다.
    assert r["missing"].status_code == 404

    # POST 만 여는 근거는 app/mcp/server.py 의 MCP_HTTP_METHODS 주석.
    assert r["mcp_get"].status_code == 405
    assert r["mcp_patch"].status_code == 405
    assert r["mcp_get"].headers["allow"] == "POST"

    # 배선 자체도 고정한다: 캐치올 Mount 가 아니라 경로가 정확히 /mcp 인 Route.
    from starlette.routing import Mount, Route

    from app.main import app
    from app.mcp.server import MCP_HTTP_METHODS

    mcp_routes = [
        route
        for route in app.router.routes
        if isinstance(route, Route) and route.path == settings.mcp_path
    ]
    assert len(mcp_routes) == 1
    assert set(mcp_routes[0].methods or set()) == set(MCP_HTTP_METHODS)
    # 어떤 캐치올도 등록돼 있으면 안 된다.
    assert not [
        route
        for route in app.router.routes
        if isinstance(route, Mount) and route.path in ("", "/")
    ]


# --- retrieve(limit=) ----------------------------------------------------


def test_retrieve_limit_caps_context_without_hiding_stages(monkeypatch):
    """MCP 만 쓰는 인자다. 컨텍스트만 자르고 스테이지 기록은 온전해야 한다.

    자른 청크가 트레이스에서도 사라지면 "리트리벌이 못 찾았다"와 "상한이
    잘랐다"가 구별되지 않는다 — tracing.py 가 막으려는 바로 그 혼동이다.
    """
    from app.services.retrieve import HybridResult, RetrievedChunk

    def _chunk(text: str) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            page_from=1,
            page_to=1,
            content=text,
            score=0.0,
        )

    candidates = [_chunk(f"c{i}") for i in range(5)]

    async def fake_hybrid(session, dense, sparse, *, user_id, document_id=None):
        return HybridResult(candidates, [c.chunk_id for c in candidates], [])

    async def fake_rerank(question, texts):
        return [(i, 9.0 - i) for i in range(5)]

    monkeypatch.setattr(pipeline.retrieve, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(pipeline.rerank, "rerank", fake_rerank)
    monkeypatch.setattr(pipeline.settings, "rerank_top", 3)

    runner = pipeline.QueryRunner(
        None,
        User(id=uuid.uuid4(), email="u@example.com", password_hash="x"),
        "질문",
        document_id=None,
        hybrid=True,
        source=SOURCE_MCP_SEARCH,
    )
    runner.dense = [0.1]
    runner.sparse = object()

    default = asyncio.run(runner.retrieve())
    assert [c.content for c in default.chunks] == ["c0", "c1", "c2"]

    widened = asyncio.run(runner.retrieve(limit=5))
    assert [c.content for c in widened.chunks] == ["c0", "c1", "c2", "c3", "c4"]

    narrowed = asyncio.run(runner.retrieve(limit=1))
    assert [c.content for c in narrowed.chunks] == ["c0"]
    # 잘린 것들이 스테이지 기록에는 그대로 있다.
    assert len(narrowed.stage_chunks["rrf"]) == 5
