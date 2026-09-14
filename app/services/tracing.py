"""Query tracing.

Records what each request did — per-stage latency, token spend, and the chunk
ids seen at every retrieval stage — so failures can be attributed. The RAG
Triad reasoning in the M4 spec only works if the stages are separable after the
fact: whether the right chunk was never retrieved, or was retrieved and then
reranked away, is invisible from the answer alone.

Tracing is best-effort. A failure to record must never fail the user's query,
so ``record`` swallows its own errors and reports them to the log instead.

M7 W7 adds a second, parallel destination for the same facts: OpenTelemetry
spans (``app/services/otel.py``). The two are not redundant. These rows are the
*product's* record — owner-scoped, queryable by ``/traces`` and
``/admin/stats``, and the source W4 pulls its L3 numbers from; the spans are the
*operator's* record, live, distributed, and carrying no raw user id at all. The
stage timings are shared because measuring them twice would let them disagree.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Trace, TraceChunk
from app.services import otel
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
# MCP answer_question (M7 W6). 0011 이 "W6 가 서버 생성 MCP 툴을 만들면 붙을
# 자리"라고 미리 적어 둔 값이 이것이고, 마이그레이션 0015 가 CHECK 제약에
# 같은 값을 더한다. 이 값이 W6 의 비교축을 **사후에 복원 가능하게** 만드는
# 유일한 장치다: mcp_search 와 mcp_answer 는 둘 다 에이전트가 부른 것이지만
# 생성이 서버에서 일어났는지 호출자에서 일어났는지가 정확히 여기서 갈린다.
# 리포트의 표가 사라져도 이 컬럼이 남아 있으면 같은 비교를 다시 낼 수 있다.
SOURCE_MCP_ANSWER = "mcp_answer"      # MCP answer_question (서버가 생성)
# 0011 이전에 쌓인 행. 서버 생성인 것은 확실하지만 둘 중 어느 엔드포인트였는지는
# 복원할 수 없어서(traces 에 conversation_id 가 없다) 모른다고 적은 값이다.
# 앱은 이 값을 절대 쓰지 않는다 — 마이그레이션의 백필에만 존재한다.
SOURCE_LEGACY = "legacy"

TRACE_SOURCES = frozenset(
    {
        SOURCE_QUERY,
        SOURCE_CONVERSATION,
        SOURCE_MCP_SEARCH,
        SOURCE_MCP_ANSWER,
        SOURCE_LEGACY,
    }
)
# W4/W5 가 에이전트 트래픽만 고를 때 쓰는 집합. mcp_ 접두사는 규약이지만
# LIKE 'mcp_%' 로 고르지 말 것 — LIKE 에서 _ 는 와일드카드다.
MCP_SOURCES = frozenset({SOURCE_MCP_SEARCH, SOURCE_MCP_ANSWER})
# 생성이 **서버 안에서** 일어난 소스. W6 의 비교축을 SQL 로 되살릴 때 쓰는
# 분할이다. mcp_search 만이 반대편(호출한 에이전트가 생성)이고, 그것이 이
# 집합에 없는 유일한 이유다 — 값이 하나 늘 때마다 어느 편인지 정해야 한다.
SERVER_GENERATED_SOURCES = frozenset(
    {SOURCE_QUERY, SOURCE_CONVERSATION, SOURCE_MCP_ANSWER}
)


class Stopwatch:
    """Accumulates per-stage elapsed milliseconds, and opens a span per stage."""

    def __init__(self) -> None:
        self._start = time.perf_counter()
        self.stages: dict[str, int] = {}

    def time(self, name: str) -> "_StageTimer":
        return _StageTimer(self, name)

    def total_ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)


class _StageTimer:
    """One stage: a millisecond count for the trace row, a span for the operator.

    The span is opened here rather than at the call sites so the W7 span
    structure (툴 호출 → 검색 → (생성)) needs no new call: every stage the
    pipeline already times becomes a child of whatever span is current, which on
    the MCP path is the SDK's ``tools/call`` span. ``otel.stage_span`` is a
    no-op when there is no live parent, so nothing changes for anyone who has
    not turned instrumentation on — see its docstring for why the parent check
    is what stops the HTTP paths emitting orphan root spans.
    """

    def __init__(self, watch: Stopwatch, name: str) -> None:
        self._watch = watch
        self._name = name
        self._t0 = 0.0
        self._span_cm = None

    async def __aenter__(self) -> "_StageTimer":
        self._t0 = time.perf_counter()
        self._span_cm = otel.stage_span(self._name)
        self._span_cm.__enter__()
        return self

    async def __aexit__(self, *exc) -> None:
        elapsed = int((time.perf_counter() - self._t0) * 1000)
        self._watch.stages[self._name] = self._watch.stages.get(self._name, 0) + elapsed
        # 예외를 그대로 넘긴다 — 스팬이 실패로 표시돼야 "리랭커가 죽어서 8.8초
        # 뒤 503" 같은 일이 트레이스에서 보인다. contextlib 는 제너레이터가
        # 같은 예외를 다시 올리면 False 를 돌려주므로 억제되지 않는다.
        self._span_cm.__exit__(*exc)


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
    # ⚠️ 아래 두 필드에는 대응하는 traces 컬럼이 없다. **관측 전용**이다 —
    # 스팬 속성으로만 나가고 DB 에는 기록되지 않는다. draft 에 태운 이유는 이
    # 값을 아는 곳(QueryRunner)과 스팬에 찍는 곳(record)이 떨어져 있고, 새
    # 인자로 잇는 것보다 이미 둘을 잇고 있는 객체에 싣는 편이 호출 지점을
    # 늘리지 않기 때문이다. 컬럼을 만들지 않은 것은 마이그레이션 하나를 관측
    # 속성 두 개에 쓰는 값이 아니고, 반환 청크 수는 trace_chunks 의 rerank
    # 단계에서, top_k 는 그 시점 설정에서 이미 복원되기 때문이다.
    top_k: int | None = None
    chunk_count: int = 0
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
    # 스팬 속성을 DB 쓰기 **전에** 찍는다. 기록이 실패해도(아래 except) 운영자
    # 쪽 증거는 남아야 하고, 무엇보다 이 함수가 소비자마다 정확히 한 번 불리는
    # 유일한 지점이라 "요청당 한 번"이 여기서 공짜로 보장된다.
    #
    # 어느 스팬에 찍히는지는 호출자가 정한다 — MCP 툴 안에서 부르면 SDK 가 연
    # tools/call 스팬이고, HTTP 경로에서는 활성 스팬이 없어 통째로 no-op 이다.
    otel.annotate_request(
        source=draft.source,
        # 원문 user_id 는 이 호출 안에서 해시가 된다. 밖으로 나가지 않는다.
        user_id=draft.user_id,
        top_k=draft.top_k,
        chunk_count=draft.chunk_count,
        tokens_in=draft.tokens_in,
        tokens_out=draft.tokens_out,
        total_ms=watch.total_ms(),
        hybrid=draft.hybrid,
        cached=draft.cached,
        refused=draft.refused,
    )
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
