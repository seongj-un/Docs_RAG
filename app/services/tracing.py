"""Query tracing.

Records what each request did — per-stage latency, token spend, and the chunk
ids seen at every retrieval stage — so failures can be attributed. The RAG
Triad reasoning in the M4 spec only works if the stages are separable after the
fact: whether the right chunk was never retrieved, or was retrieved and then
reranked away, is invisible from the answer alone.

Tracing is best-effort. A failure to record must never fail the user's query,
so ``record`` swallows its own errors and reports them to the log instead.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Trace, TraceChunk
from app.services.retrieve import RetrievedChunk

logger = logging.getLogger(__name__)

# Retrieval stages, in pipeline order.
STAGE_DENSE = "dense"
STAGE_SPARSE = "sparse"
STAGE_RRF = "rrf"
STAGE_RERANK = "rerank"


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


async def record(
    session: AsyncSession, draft: TraceDraft, watch: Stopwatch
) -> uuid.UUID | None:
    """Persist a trace. Returns its id, or None if recording failed."""
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
        )
        session.add(trace)
        await session.flush()  # assign trace.id before children reference it
        session.add_all(_chunk_rows(trace.id, draft))
        await session.commit()
        return trace.id
    except Exception:
        # Observability must never take down the thing it observes.
        logger.exception("failed to record trace")
        await session.rollback()
        return None
