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


async def queries_total(session: AsyncSession, user_id: uuid.UUID) -> int:
    """계정 수명 전체의 질의 수. 시간 창이 없는 것이 요점이다."""
    result = await session.execute(
        select(func.count(UsageEvent.id)).where(
            UsageEvent.user_id == user_id,
            UsageEvent.kind == "query",
        )
    )
    return int(result.scalar_one())


async def documents_total(session: AsyncSession, user_id: uuid.UUID) -> int:
    """지금까지 **수락된** 업로드 수.

    ``documents`` 를 세지 않는다. ``UsageEvent`` 의 외래키는 ``documents``
    가 아니라 ``users`` 를 향하므로 문서를 지워도 행이 남고, 올렸다 지워서
    한도를 되돌리는 우회가 구조적으로 막힌다.

    ``ingest`` 도 세지 않는다. 그 행은 백그라운드 인덱싱이 성공한 뒤에야
    생겨서, 색인이 끝나기 전에 연달아 던진 업로드는 카운터가 0인 채로 전부
    통과하고 실패한 업로드는 아예 세어지지 않는다. ``upload`` 는 업로드를
    수락하는 그 요청 안에서 기록된다.
    """
    result = await session.execute(
        select(func.count(UsageEvent.id)).where(
            UsageEvent.user_id == user_id,
            UsageEvent.kind == "upload",
        )
    )
    return int(result.scalar_one())


async def pages_uploaded_total(session: AsyncSession, user_id: uuid.UUID) -> int:
    """계정 수명 전체에 걸쳐 **수락된** 업로드의 누적 쪽수.

    ``documents_total`` 과 같은 이유로 ``ingest`` 가 아니라 ``upload`` 를
    센다 — ``ingest`` 는 색인이 성공한 뒤에야 생겨서, 색인이 끝나기 전에
    연달아 던진 업로드는 전부 0쪽으로 보이고 실패한 업로드는 아예 빠진다.
    """
    result = await session.execute(
        select(func.coalesce(func.sum(UsageEvent.pages), 0)).where(
            UsageEvent.user_id == user_id,
            UsageEvent.kind == "upload",
        )
    )
    return int(result.scalar_one())


async def unverified_query_exceeded(
    session: AsyncSession, user_id: uuid.UUID
) -> bool:
    """미인증 계정의 맛보기 질의 한도를 넘겼는지. 0 이면 게이트를 끈다.

    호출자가 인증 여부를 먼저 확인한다 — 이 함수는 세기만 한다.
    """
    if settings.unverified_quota_queries <= 0:
        return False
    return await queries_total(session, user_id) >= settings.unverified_quota_queries


async def unverified_upload_exceeded(
    session: AsyncSession, user_id: uuid.UUID
) -> bool:
    if settings.unverified_quota_documents <= 0:
        return False
    return (
        await documents_total(session, user_id)
        >= settings.unverified_quota_documents
    )


async def unverified_pages_exceeded(
    session: AsyncSession, user_id: uuid.UUID, incoming_pages: int = 0
) -> bool:
    """미인증 계정의 맛보기 쪽수 한도를 이 업로드가 넘기는지.

    ``unverified_upload_exceeded`` 와 달리 이력만으로는 판단할 수 없다 —
    첫 업로드 자체가 500쪽이면 이력이 0이어도 그 한 번으로 한도를 넘겨야
    한다. ``upload_quota_exceeded`` 와 같은 모양(이력 + 들어오는 쪽수)인
    이유가 그것이다.
    """
    if settings.unverified_quota_pages <= 0:
        return False
    used = await pages_uploaded_total(session, user_id)
    return used + incoming_pages > settings.unverified_quota_pages


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
