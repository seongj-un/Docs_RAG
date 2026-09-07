"""Operational statistics over data the app already records.

M4 chose Postgres tracing over Langfuse because a six-container observability
stack was out of proportion to this project. The same reasoning applies to the
dashboard: ``traces`` and ``usage_events`` already hold every number the M6
completion criteria ask for, so what was missing was a way to read them — not
another pipeline to collect them.

Access is a shared token from ``ADMIN_TOKEN``, and the route is **404 when the
token is unset**. A 401 would confirm the endpoint exists on every deployment
that never configured it; 404 says nothing. A wrong token on a configured
instance does get 401, because there the operator needs to tell "not
authorised" apart from "wrong path".
"""

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import Float, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import Document, Trace
from app.schemas import (
    AdminStats,
    DocumentStats,
    FailureReason,
    Percentiles,
    QueryStats,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_NOT_FOUND = HTTPException(
    status_code=http_status.HTTP_404_NOT_FOUND, detail="not found"
)

STAGES = ("embed", "retrieve", "rerank", "generate")


async def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    if not settings.admin_token:
        raise _NOT_FOUND
    if x_admin_token is None or not secrets.compare_digest(
        x_admin_token, settings.admin_token
    ):
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        )


def _percentiles(column):
    """p50 and p95 of a column, ignoring rows where the stage did not run.

    ``percentile_cont`` interpolates, which is what you want for a latency
    tail; NULLs are skipped by the aggregate, so a cache hit's missing
    ``generate_ms`` does not count as zero.
    """
    value = cast(column, Float)
    return (
        func.percentile_cont(0.5).within_group(value.asc()),
        func.percentile_cont(0.95).within_group(value.asc()),
    )


def _pair(p50, p95) -> Percentiles:
    return Percentiles(
        p50=None if p50 is None else round(p50),
        p95=None if p95 is None else round(p95),
    )


@router.get("/stats", response_model=AdminStats, dependencies=[Depends(require_admin)])
async def stats(
    hours: int = Query(default=24, ge=1, le=24 * 90),
    session: AsyncSession = Depends(get_session),
) -> AdminStats:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = Trace.created_at >= since

    counts = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Trace.cached.is_(True)),
                func.count().filter(Trace.refused.is_(True)),
                func.coalesce(func.sum(Trace.tokens_in), 0),
                func.coalesce(func.sum(Trace.tokens_out), 0),
            ).where(recent)
        )
    ).one()
    total, cached, refused, tokens_in, tokens_out = counts

    overall = (
        await session.execute(select(*_percentiles(Trace.total_ms)).where(recent))
    ).one()

    # Cache hits skip embedding, reranking and generation, so including them
    # would report stage latencies the uncached path never sees.
    live = recent & Trace.cached.is_(False)
    stage_columns = []
    for stage in STAGES:
        stage_columns.extend(_percentiles(getattr(Trace, f"{stage}_ms")))
    stage_row = (await session.execute(select(*stage_columns).where(live))).one()
    stages = {
        stage: _pair(stage_row[i * 2], stage_row[i * 2 + 1])
        for i, stage in enumerate(STAGES)
    }

    by_status = (
        await session.execute(
            select(Document.status, func.count()).group_by(Document.status)
        )
    ).all()
    documents = DocumentStats(**{status: n for status, n in by_status})

    # "UpstreamUnavailable: embedding server ..." and "ValueError: no
    # extractable text ..." are different operational problems; the part before
    # the colon separates them without leaking file names into the report.
    reason = func.split_part(Document.error, ":", 1)
    failures = (
        await session.execute(
            select(reason, func.count())
            .where(Document.status == "failed", Document.error.isnot(None))
            .group_by(reason)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()

    return AdminStats(
        window_hours=hours,
        queries=QueryStats(total=total, cached=cached, refused=refused),
        total_ms=_pair(*overall),
        stages=stages,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        documents=documents,
        failures=[FailureReason(reason=r, count=n) for r, n in failures],
    )
