"""Query tracing.

Records what each request did — per-stage latency, token spend, and the chunk
ids seen at every retrieval stage — so failures can be attributed. The RAG
Triad reasoning in the M4 spec only works if the stages are separable after the
fact: whether the right chunk was never retrieved, or was retrieved and then
reranked away, is invisible from the answer alone.

Failures are traced too, and for the same reason: a query that ended in a 503
is the one an operator most needs to find later. Recording only the successes
made the table lie in the one direction that matters — during an embedding
outage ``/admin/stats`` showed *fewer* queries, not more.

Tracing is best-effort. A failure to record must never fail the user's query,
so ``record`` swallows its own errors and reports them to the log instead.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import NO_REQUEST_ID, request_id
from app.models import Trace, TraceChunk
from app.services.retrieve import RetrievedChunk

logger = logging.getLogger(__name__)

# Retrieval stages, in pipeline order.
STAGE_DENSE = "dense"
STAGE_SPARSE = "sparse"
STAGE_RRF = "rrf"
STAGE_RERANK = "rerank"

# nginx's convention for "the client hung up before we answered". Not a real
# HTTP status — nothing is ever sent with it — but this column is read by an
# operator scanning for trouble, and a disconnect filed as 500 would send them
# hunting a server fault that never happened.
CLIENT_CLOSED_REQUEST = 499

# ``error`` is grouped in the stats report, so it has to stay short and
# bounded. ``HTTPException.detail`` is typed ``Any`` and can be a dict, so a
# cap is not theoretical.
REASON_MAX = 200


@dataclass(frozen=True)
class Failure:
    """How a request ended, in the two fields ``traces`` stores."""

    status_code: int
    reason: str


def describe_failure(exc: BaseException) -> Failure:
    """Classify an exception into what is safe and useful to persist.

    The message of an unexpected exception is **not** kept. It is the part
    that carries the user's question, a file path, or an upstream response
    body, and it is also the part already written to the app log in full —
    where ``request_id`` now leads. What the database gets is the class name,
    which is what makes "eleven OperationalError" a readable line in a report.
    """
    if isinstance(exc, HTTPException):
        # The detail is what the caller already received in the response body,
        # so storing it leaks nothing new and groups well: these are a handful
        # of fixed phrases ("search unavailable", "not found").
        return Failure(exc.status_code, str(exc.detail)[:REASON_MAX])
    if isinstance(exc, asyncio.CancelledError):
        return Failure(CLIENT_CLOSED_REQUEST, "client disconnected")
    return Failure(500, type(exc).__name__[:REASON_MAX])


def current_request_id() -> str | None:
    """This request's id, or None outside a request.

    ``NO_REQUEST_ID`` ("-") is a *log* convention — it keeps a fixed-width
    column filled for startup and background lines. Writing it into a column
    would invent a row that matches ``WHERE request_id = '-'``, which is a
    bucket of unrelated requests rather than an answer.
    """
    current = request_id.get()
    return None if current == NO_REQUEST_ID else current


class Stopwatch:
    """Accumulates per-stage elapsed milliseconds."""

    def __init__(self) -> None:
        self._start = time.perf_counter()
        self.stages: dict[str, int] = {}

    def time(self, name: str) -> "_StageTimer":
        return _StageTimer(self, name)

    def total_ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)


class _StageTimer:
    def __init__(self, watch: Stopwatch, name: str) -> None:
        self._watch = watch
        self._name = name
        self._t0 = 0.0

    async def __aenter__(self) -> "_StageTimer":
        self._t0 = time.perf_counter()
        return self

    async def __aexit__(self, *exc) -> None:
        elapsed = int((time.perf_counter() - self._t0) * 1000)
        self._watch.stages[self._name] = self._watch.stages.get(self._name, 0) + elapsed


@dataclass
class TraceDraft:
    """Everything gathered about one request, written in a single call."""

    user_id: uuid.UUID
    question: str
    document_id: uuid.UUID | None = None
    hybrid: bool = False
    cached: bool = False
    refused: bool = False
    answer: str | None = None
    llm_model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    # Read once, when the draft is created, rather than at write time: the row
    # is written from an exception handler or from inside a streaming
    # generator, and the id has to be the one this request started with.
    request_id: str | None = field(default_factory=current_request_id)
    # Both stay None on a request that answered. See Trace.status_code.
    status_code: int | None = None
    error: str | None = None
    # stage -> ordered chunk ids (dense/sparse), or ordered chunks (rrf/rerank)
    stage_ids: dict[str, list[uuid.UUID]] = field(default_factory=dict)
    stage_chunks: dict[str, list[RetrievedChunk]] = field(default_factory=dict)


def _chunk_rows(trace_id: uuid.UUID, draft: TraceDraft) -> list[TraceChunk]:
    rows: list[TraceChunk] = []
    for stage, ids in draft.stage_ids.items():
        rows.extend(
            TraceChunk(trace_id=trace_id, chunk_id=cid, stage=stage, rank=rank)
            for rank, cid in enumerate(ids, start=1)
        )
    for stage, chunks in draft.stage_chunks.items():
        rows.extend(
            TraceChunk(
                trace_id=trace_id,
                chunk_id=chunk.chunk_id,
                stage=stage,
                rank=rank,
                score=chunk.score,
                page_from=chunk.page_from,
            )
            for rank, chunk in enumerate(chunks, start=1)
        )
    return rows


async def _rollback_quietly(session: AsyncSession) -> None:
    """Undo the half-written transaction without becoming the error itself.

    ``session.rollback()`` used to sit bare inside ``record``'s handler, so a
    rollback that threw (a connection already gone is the usual way) escaped
    the one function in this file that promises never to raise — turning a
    failed *trace* into a failed *query*. Same lesson ``_discard_unanswered``
    in routers/conversations.py wrote down: cleanup must never replace the
    error that caused it.

    ``except Exception``, not ``BaseException``: a cancellation arriving
    during the rollback still has to propagate, because the task it belongs
    to is being torn down either way.
    """
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001
        logger.exception("failed to roll back after a tracing failure")


async def record(
    session: AsyncSession, draft: TraceDraft, watch: Stopwatch
) -> uuid.UUID | None:
    """Persist a trace. Returns its id, or None if recording failed.

    Callers reach this from failure handlers, where the session may already be
    sitting on a transaction the original error aborted. That is what the
    handlers below are for: this returns None rather than adding a second
    exception on top of the first.
    """
    try:
        trace = Trace(
            user_id=draft.user_id,
            question=draft.question,
            document_id=draft.document_id,
            hybrid=draft.hybrid,
            cached=draft.cached,
            refused=draft.refused,
            answer=draft.answer,
            llm_model=draft.llm_model,
            tokens_in=draft.tokens_in,
            tokens_out=draft.tokens_out,
            embed_ms=watch.stages.get("embed"),
            retrieve_ms=watch.stages.get("retrieve"),
            rerank_ms=watch.stages.get("rerank"),
            generate_ms=watch.stages.get("generate"),
            total_ms=watch.total_ms(),
            request_id=draft.request_id,
            status_code=draft.status_code,
            error=draft.error,
        )
        session.add(trace)
        await session.flush()  # assign trace.id before children reference it
        session.add_all(_chunk_rows(trace.id, draft))
        await session.commit()
        return trace.id
    except Exception:
        # Observability must never take down the thing it observes.
        logger.exception("failed to record trace")
        await _rollback_quietly(session)
        return None
    except BaseException:
        # CancelledError is a BaseException, so the clause above never saw it:
        # a client hanging up mid-write left this session holding an open
        # transaction, which the next user of the session inherits. Cleaned up
        # here — but re-raised, never swallowed. Eating a cancellation would
        # make a torn-down request look like one that completed, and the
        # promise this module makes is "don't break the query", not "pretend
        # the query is still alive".
        await _rollback_quietly(session)
        raise
