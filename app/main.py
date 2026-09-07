"""FastAPI application entrypoint.

Schema is applied on startup via Alembic (``run_migrations()``). Routers cover
the documents lifecycle and grounded query.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import run_migrations
from app.config import settings
from app.routers import auth, conversations, documents, query, traces


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_migrations()
    yield


app = FastAPI(title="Docs Q&A RAG (M1)", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,  # the session cookie must ride along
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(conversations.router)
app.include_router(documents.router)
app.include_router(query.router)
app.include_router(traces.router)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
