"""Async SQLAlchemy engine/session and M1 schema bootstrap.

M1 creates the schema directly via ``init_db()`` (the pgvector extension, the
tables, and the HNSW index). Alembic migrations are deferred to M2 — see the
roadmap in the project spec.
"""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.models import Base

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async session."""
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create the pgvector extension, tables, and vector index if missing.

    The extension must exist before ``create_all`` runs, because the ``chunks``
    table declares a ``VECTOR`` column and an HNSW index over it.
    """
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
