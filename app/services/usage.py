"""Usage recording and database-backed quotas.

Quotas are aggregated from ``usage_events`` rather than kept in memory, so they
survive restarts and stay correct behind multiple workers — the opposite
trade-off from the in-process rate limiter, and the reason this is the layer
that actually bounds spend.

Windows are wall-clock calendar windows in UTC (today / this month), which is
what the spec's "일/월" limits mean.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import UsageEvent


def _start_of_day() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_month() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def queries_today(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count(UsageEvent.id)).where(
            UsageEvent.user_id == user_id,
            UsageEvent.kind == "query",
            UsageEvent.created_at >= _start_of_day(),
        )
    )
    return int(result.scalar_one())


async def pages_this_month(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.coalesce(func.sum(UsageEvent.pages), 0)).where(
            UsageEvent.user_id == user_id,
            UsageEvent.kind == "ingest",
            UsageEvent.created_at >= _start_of_month(),
        )
    )
    return int(result.scalar_one())


async def query_quota_exceeded(session: AsyncSession, user_id: uuid.UUID) -> bool:
    if settings.quota_queries_per_day <= 0:
        return False
    return await queries_today(session, user_id) >= settings.quota_queries_per_day


async def upload_quota_exceeded(
    session: AsyncSession, user_id: uuid.UUID, incoming_pages: int = 0
) -> bool:
    """True when this upload would push the month past the page allowance."""
    if settings.quota_upload_pages_per_month <= 0:
        return False
    used = await pages_this_month(session, user_id)
    return used + incoming_pages > settings.quota_upload_pages_per_month


async def record(
    session: AsyncSession,
    user_id: uuid.UUID,
    kind: str,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    pages: int = 0,
    cached: bool = False,
) -> None:
    session.add(
        UsageEvent(
            user_id=user_id,
            kind=kind,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            pages=pages,
            cached=cached,
        )
    )
    await session.commit()
