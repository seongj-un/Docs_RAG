"""Async SQLAlchemy engine/session and schema migration.

The schema is owned by Alembic (``alembic/versions``). ``run_migrations()``
applies migrations up to head; the app calls it on startup so ``uvicorn`` works
out of the box, and it can also be run manually via ``alembic upgrade head``.
"""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

_ROOT = Path(__file__).resolve().parent.parent

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async session."""
    async with SessionLocal() as session:
        yield session


def _upgrade_to_head() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    command.upgrade(cfg, "head")


async def run_migrations() -> None:
    """Apply Alembic migrations to head.

    Runs in a worker thread because Alembic's online env opens its own event
    loop (``asyncio.run``), which cannot nest inside the running app loop.
    """
    await asyncio.to_thread(_upgrade_to_head)
