"""FastAPI application entrypoint.

Schema is applied on startup via Alembic (``run_migrations()``). Routers cover
the documents lifecycle and grounded query, and M7 mounts an MCP server on the
same app so agents reach the same pipeline over the same sessions.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.routing import Route

from app.db import run_migrations
from app.config import settings
from app.mcp.server import MCP_HTTP_METHODS, build_mcp_app, build_mcp_server
from app.routers import (
    admin, auth, chunks, conversations, documents, query, traces, usage,
)
from app.services import mailer

# MCP 서버는 앱보다 먼저 만들어져야 한다. streamable_http_app() 을 부르는
# 것이 session_manager 를 만드는 행위이고, 아래 lifespan 이 그 manager 를
# 실행해야 하기 때문이다 — 앱을 만든 뒤에 부르면 lifespan 이 이미 정의된
# 시점이라 엮을 곳이 없다. 꺼져 있으면 둘 다 None 이고 마운트도 하지 않는다.
mcp_server = build_mcp_server() if settings.mcp_enabled else None
mcp_app = build_mcp_app(mcp_server) if mcp_server is not None else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 요청마다가 아니라 프로세스당 한 번 — 설정은 기동 중에 바뀌지 않는다.
    mailer.warn_if_base_url_looks_local()
    await run_migrations()

    if mcp_server is None:
        yield
        return

    # Starlette 의 Mount 는 서브앱의 lifespan 을 실행해 주지 않는다. 이걸
    # 엮지 않으면 앱은 멀쩡히 뜨고 기존 라우트도 전부 동작하는데 MCP 요청만
    # RuntimeError("Task group is not initialized") 로 500 이 된다 — 기동
    # 로그에는 아무 경고도 없다. 그래서 호스트 앱의 lifespan 이 직접 돌린다.
    async with mcp_server.session_manager.run():
        yield


app = FastAPI(title="Docs Q&A RAG (M1)", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,  # the session cookie must ride along
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin.router)
app.include_router(auth.router)
app.include_router(chunks.router)
app.include_router(conversations.router)
app.include_router(documents.router)
app.include_router(query.router)
app.include_router(traces.router)
app.include_router(usage.router)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


# MCP 앱은 **Mount 가 아니라 Route 로** 건다. 셋 다 실제로 띄워서 재 본 결과다:
#
#   구성                                    기존 라우트 405   POST /mcp
#   A  mount("/") + 서브앱 /mcp                 ❌ 404         401 ✓
#   B  mount("/mcp") + 서브앱 "/"               405 ✓         ❌ 307
#   C  Route("/mcp", endpoint=서브앱)           405 ✓         401 ✓   ← 이것
#
# B 가 안 되는 이유는 알려진 함정이다. mount("/mcp") 에 서브앱 경로를 "/" 로
# 두면 POST /mcp 가 307 리다이렉트를 받는데, MCP 클라이언트는 POST 리다이렉트를
# 따라가지 않아 "연결이 안 된다"는 증상만 남는다.
#
# ⚠️ A 가 안 되는 이유가 덜 알려져 있고, 그래서 여기 적는다. 루트 마운트는
# 기존 라우트를 **가리지는 않는다**(등록 순서대로 매칭하므로 앞선 것이 이긴다).
# 대신 **405 를 전부 404 로 바꾼다.** Starlette 라우터는 경로는 맞고 메서드가
# 틀린 라우트를 Match.PARTIAL 로 기억해 두고 계속 찾다가, 끝까지 full match 가
# 없을 때에만 그 기억을 꺼내 405 를 낸다. 그런데 Mount("/") 는 무엇이든 full
# match 라 그 fallback 에 영영 도달하지 못하고, MCP 서브앱의 404 가 이긴다.
# 실측으로 GET /query·GET /auth/login·POST /health 가 전부 405 → 404 로
# 바뀌었다(2026-09-14). API 전체의 오류 의미가 조용히 망가지는 종류의 회귀다.
#
# Route 는 캐치올이 아니라 경로가 정확히 /mcp 일 때만 맞으므로 PARTIAL fallback
# 이 그대로 살아 있다. endpoint 가 함수가 아니면 Starlette 가 ASGI 앱으로
# 취급하기 때문에 서브앱의 미들웨어 스택(베어러 인증·transport security)도
# 온전하다. 여는 메서드를 POST 하나로 좁힌 근거는 app/mcp/server.py 의
# MCP_HTTP_METHODS 주석 참조.
#
# tests/test_mcp_server.py::test_mount_does_not_shadow_existing_routes 가
# **틀린 메서드**로도 찔러서 405 가 405 로 남는지 단언한다 — 편의상 mount("/")
# 로 되돌리기 쉬운 자리라, 되돌리면 그 테스트가 먼저 빨개진다.
if mcp_app is not None:
    app.router.routes.append(
        Route(settings.mcp_path, endpoint=mcp_app, methods=MCP_HTTP_METHODS)
    )
