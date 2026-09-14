"""Shared harness for the MCP test modules (M7 W7).

``test_mcp_server.py`` (W2) grew its own copy of this while it was the only MCP
test file. W7 added three more, so the pieces every one of them needs — a live
endpoint wired the way ``main.py`` wires the real one, a modern-era request
body, a user with a session — live here instead of being pasted four times.

W2 의 파일은 일부러 건드리지 않았다. 통과하고 있는 800줄짜리 테스트를 리팩터링
하는 것은 W7 이 사지 말라고 한 종류의 분량이고, 254개 기준선을 위험에 빠뜨린다.
그래서 중복은 그 파일 하나에만 남아 있고, 새로 쓰는 것들은 전부 여기를 쓴다.
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
from app.mcp.server import (
    MCP_HTTP_METHODS,
    RESOURCE_METADATA_METHODS,
    build_mcp_app,
    build_mcp_server,
    resource_metadata_path,
)
from app.models import Chunk, Document, Session, User
from app.services import auth, embeddings, pipeline, rerank

EMBED_DIM = settings.embed_dim
PROTOCOL_VERSION = "2026-07-28"

# 기본 허용 Host 목록이 로컬호스트뿐이라(설정을 비우면 그렇게 되도록 골랐다)
# 테스트도 로컬호스트로 말한다. http://test 로 보내면 421 이 난다.
BASE_URL = "http://localhost:8000"


def run_async(coro_fn):
    """Run one coroutine, always disposing the engine afterwards.

    asyncpg 커넥션은 자기를 만든 루프에 묶여 있다. 풀에 남은 것이 다음 테스트의
    루프로 새어 들어가면 안 된다 — test_isolation.py 와 같은 이유, 같은 모양.
    """

    async def wrapper():
        try:
            return await coro_fn()
        finally:
            await engine.dispose()

    return asyncio.run(wrapper())


def db_available() -> bool:
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


# --- MCP 클라이언트 -------------------------------------------------------
#
# modern era(2026-07-28) 요청은 엄격하다: params._meta 에 프로토콜 버전과
# 클라이언트 능력이 둘 다 있어야 하고, Mcp-Method 헤더가 본문의 method 와,
# tools/call 이면 Mcp-Name 이 params.name 과 맞아야 한다.


def body(method: str, params: dict | None = None, meta: dict | None = None) -> dict:
    merged = dict(params or {})
    merged["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        **(meta or {}),
    }
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": merged}


def headers(method: str, token: str | None, name: str | None = None) -> dict:
    out = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        out["Mcp-Name"] = name
    if token is not None:
        out["Authorization"] = f"Bearer {token}"
    return out


async def call(client: AsyncClient, method: str, token, params=None, name=None, meta=None):
    return await client.post(
        settings.mcp_path,
        json=body(method, params, meta),
        headers=headers(method, token, name),
    )


async def search(client: AsyncClient, token: str, *, meta=None, **arguments):
    return await call(
        client,
        "tools/call",
        token,
        {"name": "search_documents", "arguments": arguments},
        name="search_documents",
        meta=meta,
    )


async def call_tool(client: AsyncClient, token: str, tool: str, **arguments):
    return await call(
        client,
        "tools/call",
        token,
        {"name": tool, "arguments": arguments},
        name=tool,
    )


@asynccontextmanager
async def mcp_client(*, extra_tools=None, variant=None):
    """A live MCP endpoint, wired the way ``main.py`` wires the real one.

    ``extra_tools`` is called with the freshly built server before its ASGI app
    exists, which is where a test registers a probe tool. 그 시점이어야 하는
    이유는 ``streamable_http_app()`` 이 session_manager 를 만드는 행위이고, 그
    뒤에 툴을 더하는 것은 프로덕션에서 일어나지 않는 순서이기 때문이다.

    ``variant`` 는 ``build_mcp_server`` 의 것과 같은 인자다. 기본값 None 이
    프로덕션 툴 표면이라 기존 호출자는 아무것도 달라지지 않고, W6 처럼 변형
    툴(``answer_question``)을 실제로 호출해 봐야 하는 테스트만 넘긴다 —
    ``main.py`` 가 절대 넘기지 않는다는 사실은 그대로다.

    A fresh instance per use because ``session_manager.run()`` refuses a second
    call. Also mirrors ``main.py`` in mounting the RFC 9728 metadata route — a
    test that skipped it would pass while the real deployment 404s.
    """
    server = build_mcp_server(variant)
    if extra_tools is not None:
        extra_tools(server)

    host = FastAPI()

    @host.get("/health")
    async def _health() -> dict[str, str]:  # pragma: no cover - 배선 증인용
        return {"status": "ok"}

    app = build_mcp_app(server)
    host.router.routes.append(
        Route(settings.mcp_path, endpoint=app, methods=MCP_HTTP_METHODS)
    )
    metadata_path = resource_metadata_path()
    if metadata_path is not None:
        host.router.routes.append(
            Route(metadata_path, endpoint=app, methods=RESOURCE_METADATA_METHODS)
        )

    async with server.session_manager.run():
        transport = ASGITransport(app=host)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            yield client


# --- 픽스처 --------------------------------------------------------------


@pytest.fixture
def stub_models(monkeypatch):
    """임베딩·리랭크 서버는 CI 에 없다 — 기존 테스트들과 같은 방식으로 대체한다.

    리트리벌 자체는 대체하지 않는다. 소유자 조인이 든 진짜 pgvector 쿼리가 이
    파일을 쓰는 테스트들이 증명하려는 바로 그것이라서다. 하이브리드를 끄는 것도
    같은 이유의 연장이다 — 그러면 스텁 하나로 끝나고, 하이브리드 쪽 순서·컷오프는
    test_query_pipeline.py 가 이미 증명한다.

    ``import`` 해서 쓰는 픽스처다. 자기 모듈에 이름만 끌어오면 된다:
    ``from tests.mcp_fixture import stub_models  # noqa: F401``
    """

    async def fake_embed_query(text: str) -> list[float]:
        return [0.1] * EMBED_DIM

    monkeypatch.setattr(embeddings, "embed_query", fake_embed_query)
    monkeypatch.setattr(pipeline.embeddings, "embed_query", fake_embed_query)
    monkeypatch.setattr(settings, "hybrid_enabled", False)

    async def unreachable_rerank(query, texts):  # pragma: no cover - 방어용
        raise AssertionError("dense path must not call the reranker")

    monkeypatch.setattr(rerank, "rerank", unreachable_rerank)


async def make_user(db, label: str, *, verified: bool = True) -> User:
    user = User(
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=auth.hash_password("password123"),
        email_verified_at=datetime.now(timezone.utc) if verified else None,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def make_session(db, user: User, *, ttl_days: int = 14) -> uuid.UUID:
    row = Session(
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=ttl_days),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row.id


async def make_chunk(db, user: User, content: str, *, page: int = 1) -> Chunk:
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


async def drop_users(*users: User) -> None:
    async with SessionLocal() as db:
        for user in users:
            row = await db.get(User, user.id)
            if row is not None:
                # 문서·청크·세션·usage·트레이스가 전부 사용자에서 캐스케이드된다.
                await db.delete(row)
        await db.commit()
