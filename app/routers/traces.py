"""Trace read endpoints.

Makes the recorded traces usable for diagnosis without reaching for psql.
Owner-scoped like everything else: a trace belonging to another user answers
404, never 403.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.deps import get_current_user
from app.models import Trace, User
from app.schemas import TraceDetail, TraceSummary

router = APIRouter(prefix="/traces", tags=["traces"])


@router.get("", response_model=list[TraceSummary])
async def list_traces(
    limit: int = Query(default=20, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Trace]:
    result = await session.execute(
        select(Trace)
        .where(Trace.user_id == user.id)
        .order_by(Trace.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


@router.get("/{trace_id}", response_model=TraceDetail)
async def get_trace(
    trace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Trace:
    """One trace with every retrieval stage, ordered dense -> sparse -> rrf -> rerank."""
    result = await session.execute(
        select(Trace)
        .options(selectinload(Trace.chunks))
        .where(Trace.id == trace_id, Trace.user_id == user.id)
    )
    trace = result.scalars().first()
    if trace is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="not found"
        )

    order = {"dense": 0, "sparse": 1, "rrf": 2, "rerank": 3}
    trace.chunks.sort(key=lambda c: (order.get(c.stage, 9), c.rank))
    return trace
