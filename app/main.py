"""FastAPI application entrypoint.

Schema is applied on startup via Alembic (``run_migrations()``). Routers cover
the documents lifecycle and grounded query. Logging is set up here too, before
anything else runs: the app owns its own format, level and request id
(``app/logging.py``) instead of inheriting whatever the migration tool's ini
file happened to install.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import run_migrations
from app.config import settings
from app.logging import RequestIdMiddleware, configure_logging
from app.routers import (
    admin, auth, chunks, conversations, documents, query, traces, usage,
)
from app.services import mailer


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 무엇보다 먼저다. 마이그레이션보다 먼저인 이유는 alembic 이 예전에 앱의
    # 로깅을 정하던 쪽이었기 때문이고(app/logging.py), 아래 경고보다 먼저인
    # 이유는 그러지 않으면 그 한 줄만 stdlib lastResort 로 나가서 — 레벨도
    # 로거 이름도 시각도 없이 — 기동 도중에 같은 로거의 출력 모양이 한 번
    # 바뀌기 때문이다.
    configure_logging()
    # 요청마다가 아니라 프로세스당 한 번 — 설정은 기동 중에 바뀌지 않는다.
    mailer.warn_about_mail_configuration()
    await run_migrations()
    yield


app = FastAPI(title="Docs Q&A RAG (M1)", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,  # the session cookie must ride along
    allow_methods=["*"],
    allow_headers=["*"],
    # 브라우저는 기본적으로 안전 목록에 없는 응답 헤더를 스크립트에서 못 읽게
    # 가린다. 프론트는 별도 오리진에서 돌므로(cors_origins), 이 줄이 없으면
    # X-Request-Id 는 네트워크 탭에만 보이고 "실패 화면에 상관 ID 를 띄워
    # 그대로 서버 로그를 찾는다"는 이 ID 의 쓸모가 사라진다.
    expose_headers=["X-Request-Id"],
)

# CORS 보다 나중에 더한다 = 바깥에 선다(add_middleware 는 앞에 끼워 넣고,
# 목록의 앞이 바깥이다). 프리플라이트·CORS 거부·핸들러 예외까지 포함해 나가는
# 모든 응답이 상관 ID 를 달고, 요청이 앱에 닿기 전에 contextvar 가 채워진다.
app.add_middleware(RequestIdMiddleware)

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
