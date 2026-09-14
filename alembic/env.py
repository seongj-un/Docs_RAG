"""Alembic environment (async).

Schema is defined by ``app.models`` and the DB URL comes from app settings, so
migrations, the ORM, and the app never drift apart.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.config import settings
from app.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

# Only the command line gets its logging from the ini. `alembic upgrade head`
# at a terminal has nobody else to configure it, so it still reads alembic.ini
# as before; the app sets configure_logger=False (app/db.py) because it has
# already installed its own handler, format and level (app/logging.py) by the
# time it runs migrations.
#
# Without that gate this call owns the logging of a running API server, which
# is how 3988fc9 happened: disable_existing_loggers defaults to True, so the
# startup migration silenced uvicorn's loggers wholesale -- no access log, and
# 500s came back as a bare "Internal Server Error" with the traceback thrown
# away, for six milestones. The keyword below fixed that symptom and stays
# (the CLI path should not disable anything either), but the symptom was never
# the only one available: fileConfig additionally flushes and closes every
# handler alive in the process. Today that happens to be survivable only
# because uvicorn's handlers are StreamHandlers, whose close() leaves the
# stream open. Point one file handler anywhere and startup would silently
# destroy it.
if (
    config.attributes.get("configure_logger", True)
    and config.config_file_name is not None
):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
