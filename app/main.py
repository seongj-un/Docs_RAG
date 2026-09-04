"""FastAPI application entrypoint.

Schema is bootstrapped on startup via ``init_db()`` (M1). Routers cover the
documents lifecycle and grounded query.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import init_db
from app.routers import documents, query


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Docs Q&A RAG (M1)", version="0.1.0", lifespan=lifespan)

app.include_router(documents.router)
app.include_router(query.router)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
