"""Operational statistics over data the app already records.

M4 chose Postgres tracing over Langfuse because a six-container observability
stack was out of proportion to this project. The same reasoning applies to the
dashboard: ``traces`` and ``documents`` already hold every number the M6
completion criteria ask for, so what was missing was a way to read them — not
another pipeline to collect them.

This used to name ``usage_events`` instead of ``documents``, which was simply
wrong: nothing here has ever read that table — the imports are ``Document``
and ``Trace``, and so is every query below. It mattered because the sentence
was also the argument for *not* building a collection pipeline, and an
argument that cites a table the code never opens is one nobody can check. The
spend numbers this reports come from ``traces.tokens_*``; ``usage_events`` is
the quota ledger, and it is ``/usage`` that reads it.

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
                # A failed query is one that never answered, and it is the
                # only thing in this report that rises during an outage —
                # everything else falls, which is what made an outage read as
                # an idle hour back when failures were not recorded at all.
                func.count().filter(Trace.status_code.isnot(None)),
                func.coalesce(func.sum(Trace.tokens_in), 0),
                func.coalesce(func.sum(Trace.tokens_out), 0),
            ).where(recent)
        )
    ).one()
    total, cached, refused, failed, tokens_in, tokens_out = counts

    # Latency is measured over requests that answered. A failure has a
    # duration too, but it is not the same quantity: a refused connection to
    # a dead embedder returns in single-digit milliseconds, so folding those
    # in would make p50 *improve* during an outage — the second way this
    # report used to describe a broken hour as a healthy one. Failures are
    # counted above and named below instead. Same reasoning as the cache-hit
    # exclusion right after this.
    answered = recent & Trace.status_code.is_(None)
    overall = (
        await session.execute(select(*_percentiles(Trace.total_ms)).where(answered))
    ).one()

    # Cache hits used to be excluded here, on the premise that they skip every
    # stage. They do not: the cache is *semantic*, so the question has to be
    # embedded before it can be looked up (``pipeline.embed`` runs, then
    # ``cache.lookup`` takes that vector). A hit therefore carries a real
    # ``embed_ms`` and nothing else -- the other three timers never open, and
    # ``percentile_cont`` skips their NULLs on its own. So the filter's only
    # effect was to throw away genuine embedding measurements, and to do it
    # exactly where there are most of them: the better the cache works, the
    # thinner the embed sample got. The premise was written in four places at
    # once -- here, the schema, the README and this test's fixture -- which is
    # why agreeing with itself kept it alive for so long.
    stage_columns = []
    for stage in STAGES:
        stage_columns.extend(_percentiles(getattr(Trace, f"{stage}_ms")))
    stage_row = (await session.execute(select(*stage_columns).where(answered))).one()
    stages = {
        stage: _pair(stage_row[i * 2], stage_row[i * 2 + 1])
        for i, stage in enumerate(STAGES)
    }

    # Not filtered by the window, on purpose — see DocumentStats. This is the
    # state of the corpus now; every other number here is an event count.
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
            .where(
                Document.status == "failed",
                Document.error.isnot(None),
                # This list ignored the window entirely, so ``hours=1`` served
                # up indexing failures from months ago under a response whose
                # first field reads ``window_hours: 1`` — an operator checking
                # whether a deploy broke ingestion saw a wall of old failures
                # and had no way to tell they were old. ``updated_at``, not
                # ``created_at``: the row is stamped when the status becomes
                # ``failed`` (services/ingest.py), so it is the instant the
                # failure happened rather than the instant the file arrived,
                # and for a document that sat in the queue those differ.
                Document.updated_at >= since,
            )
            .group_by(reason)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()

    # Grouped by status *and* reason rather than either alone: "503 search
    # unavailable" and "404 not found" are one operator action apart, and the
    # status by itself does not say which 503 it was.
    query_failures = (
        await session.execute(
            select(Trace.status_code, Trace.error, func.count())
            .where(recent, Trace.status_code.isnot(None))
            .group_by(Trace.status_code, Trace.error)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()

    return AdminStats(
        window_hours=hours,
        queries=QueryStats(
            total=total, cached=cached, refused=refused, failed=failed
        ),
        total_ms=_pair(*overall),
        stages=stages,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        documents=documents,
        failures=[FailureReason(reason=r, count=n) for r, n in failures],
        query_failures=[
            FailureReason(
                # The reason is written, never NULL — but this report must not
                # be the place that finds out otherwise.
                reason=f"{status} {error}" if error else str(status),
                count=n,
            )
            for status, error, n in query_failures
        ],
    )
