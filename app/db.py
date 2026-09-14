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

# ``hide_parameters=True`` keeps bound values out of the exception text. Without
# it a DBAPI error arrives as ``... [parameters: (...)]`` with up to 300
# characters of the statement's values, and this app binds the user's question,
# the generated answer and chunk text into the statements most likely to fail
# under load (``traces``, ``messages``). Those strings then reach stderr through
# the ``logger.exception`` calls that exist precisely to record a swallowed
# failure -- so the one path built to make failures visible was also the one
# path that could publish a contract. The statement, the table and the error
# class all survive, which is what actually identifies the bug; the row that
# tripped it is recoverable from ``documents.error`` or the trace instead.
#
# This narrows the leak, it does not close it: a driver may write the offending
# value into its *own* message, which no SQLAlchemy setting can reach (asyncpg
# spells out ``$1: '...'`` for a type error, measured). The difference that is
# actually bought is one value versus every value bound to the statement -- and
# the statements most likely to fail here bind the question and the answer
# together. ``tests/test_logging.py`` pins exactly that much and no more.
engine = create_async_engine(
    settings.database_url, echo=False, pool_pre_ping=True, hide_parameters=True
)

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
    # Do not let the migration reconfigure the running server's logging.
    # env.py calls fileConfig(), which rebuilds the root logger from
    # alembic.ini and flushes and closes every handler in the process. That is
    # correct for `alembic upgrade head` at a terminal, where nothing else has
    # set logging up, and wrong here, where the app configured its own
    # handler seconds earlier (app/logging.py). It has already gone wrong once:
    # 3988fc9, where the same call silenced uvicorn for six milestones. The
    # attribute is alembic's own idiom for "the caller is a program, not the
    # CLI"; env.py reads it.
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


async def run_migrations() -> None:
    """Apply Alembic migrations to head.

    Runs in a worker thread because Alembic's online env opens its own event
    loop (``asyncio.run``), which cannot nest inside the running app loop.
    """
    await asyncio.to_thread(_upgrade_to_head)
