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

# 트레이스를 남긴 소비자. 스키마의 CHECK 제약(마이그레이션 0011)이 같은 목록을
# 들고 있으므로, 여기에 값을 더하려면 마이그레이션도 함께 가야 한다 — 그
# 번거로움이 의도다. 소스를 늘리는 것은 검토를 거친 결정이어야 하고, 오타가
# 조용히 새 소스를 만들어내면("mcp_serach") 이 컬럼의 존재 이유가 사라진다.
#
# 이 값은 "생성이 어디서 일어났는가"가 아니라 **소비자**를 적는다. 생성 위치는
# 소비자에서 유도된다(query·conversation = 서버 생성, mcp_search = 호출한
# 에이전트가 생성). 반대로 "server"/"client" 만 적으면 W4 가 필요로 하는 두
# HTTP 엔드포인트의 구분이 사라진다 — 유도할 수 있는 쪽을 유도하게 둔다.
SOURCE_QUERY = "query"                # POST /query
SOURCE_CONVERSATION = "conversation"  # POST /conversations/{id}/query
SOURCE_MCP_SEARCH = "mcp_search"      # MCP search_documents (에이전트가 생성)
# 0011 이전에 쌓인 행. 서버 생성인 것은 확실하지만 둘 중 어느 엔드포인트였는지는
# 복원할 수 없어서(traces 에 conversation_id 가 없다) 모른다고 적은 값이다.
# 앱은 이 값을 절대 쓰지 않는다 — 마이그레이션의 백필에만 존재한다.
SOURCE_LEGACY = "legacy"

TRACE_SOURCES = frozenset(
    {SOURCE_QUERY, SOURCE_CONVERSATION, SOURCE_MCP_SEARCH, SOURCE_LEGACY}
)
# W4/W5 가 에이전트 트래픽만 고를 때 쓰는 집합. mcp_ 접두사는 규약이지만
# LIKE 'mcp_%' 로 고르지 말 것 — LIKE 에서 _ 는 와일드카드다.
MCP_SOURCES = frozenset({SOURCE_MCP_SEARCH})


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
    # 기본값이 없는 것이 의도다. 새 소비자가 빠뜨리면 TypeError 로 즉시 터진다 —
    # 기본값을 주면 그 소비자의 트레이스가 조용히 남의 소스로 집계된다.
    source: str
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
            source=draft.source,
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
