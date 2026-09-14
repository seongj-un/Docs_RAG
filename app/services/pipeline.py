"""Shared query pipeline.

``POST /query`` (one-shot), ``POST /conversations/{id}/query`` (streaming,
persisted) and the MCP ``search_documents`` tool (M7 W2) must behave
identically on everything that is not presentation: rate limits, quota, scope
ownership, the semantic cache, retrieval, usage accounting and tracing.
Keeping that in one place is what stops the callers from drifting — a limit
enforced on one path but not the other is a hole, not a difference.

The MCP tool is the third consumer and the first one that stops before
generation: it hands chunks to a calling agent that writes the answer itself.
That is why ``finalize`` has a generation-less sibling (``finalize_search``)
rather than the tool closing out its own quota and trace — the part the tool
skips is generation, not the accounting, and accounting that lives in the
caller is accounting that drifts.

The runner is a single request's worth of state, so the callers pass the
question once instead of threading a dozen arguments through every step.
"""

import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

from fastapi import HTTPException
from fastapi import status as http_status
from pgvector import SparseVector
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import VERIFICATION_REQUIRED
from app.models import User
from app.services import cache, embeddings, ingest, rerank, retrieve, tracing, usage
from app.services.upstream import UpstreamUnavailable
from app.services.ratelimit import query_limiter
from app.services.tracing import (
    STAGE_DENSE,
    STAGE_RERANK,
    STAGE_RRF,
    STAGE_SPARSE,
    TRACE_SOURCES,
    Stopwatch,
    TraceDraft,
)


@dataclass
class Retrieval:
    chunks: list[retrieve.RetrievedChunk] = field(default_factory=list)
    stage_ids: dict[str, list[uuid.UUID]] = field(default_factory=dict)
    stage_chunks: dict[str, list[retrieve.RetrievedChunk]] = field(default_factory=dict)


def too_many(detail: str) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": "60"},
    )


NOT_FOUND = HTTPException(
    status_code=http_status.HTTP_404_NOT_FOUND, detail="not found"
)


@contextmanager
def _reachable():
    """Report a model server being down as 503, not as this service crashing.

    Both query paths run through here, so neither can report an outage
    differently from the other. The clients raise a domain error precisely so
    the translation happens once, at the boundary that has a response to put
    it in — background indexing has none, and records the failure instead.

    No ``Retry-After``: nothing here knows when the server comes back, and
    unlike a provider quota it usually takes someone restarting it.
    """
    try:
        yield
    except UpstreamUnavailable as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="search unavailable",
        ) from exc


class QueryRunner:
    """One question, from limits through to tracing."""

    def __init__(
        self,
        session: AsyncSession,
        user: User,
        question: str,
        *,
        document_id: uuid.UUID | None,
        hybrid: bool | None,
        source: str,
    ) -> None:
        # ``source`` 에 기본값을 두지 않는다. 네 번째 소비자가 생겼을 때
        # 빠뜨리면 TypeError 로 즉시 터지고, 무엇을 적을지 결정하려면
        # tracing.TRACE_SOURCES 를 보게 된다 — 기본값을 주면 그 소비자의
        # 트레이스가 말없이 /query 것으로 집계된다. 이 컬럼의 존재 이유가
        # "어느 소비자인가"이므로, 틀린 값보다 컴파일 시점의 실패가 낫다.
        if source not in TRACE_SOURCES:
            raise ValueError(f"unknown trace source: {source!r}")
        self.session = session
        self.user = user
        self.question = question
        self.document_id = document_id
        self.use_hybrid = hybrid if hybrid is not None else settings.hybrid_enabled
        self.watch = Stopwatch()
        self.draft = TraceDraft(
            user_id=user.id,
            question=question,
            source=source,
            document_id=document_id,
            hybrid=self.use_hybrid,
        )
        self.dense: list[float] = []
        self.sparse: SparseVector | None = None
        # enforce_limits 가 예약에 성공하면 채워지고, record_cache_hit 나
        # finalize 가 그 예약을 진짜 기록으로 바꾸는 순간 다시 None 이 된다.
        # 요청이 둘 중 어느 쪽에도 이르지 못하고 끝나면 이 값이 곧
        # release_reservation 이 지워야 할 대상이다.
        self._reservation_id: uuid.UUID | None = None

    async def enforce_limits(self, client_ip: str) -> None:
        if not query_limiter.allow(f"user:{self.user.id}") or not query_limiter.allow(
            f"ip:{client_ip}"
        ):
            raise too_many("query rate limit exceeded")

        # 여기서부터 커밋까지가 "세고 나서 쓴다"를 원자로 만드는 구간이다.
        # 잠금 없이는 동시 요청들이 모두 같은(터지기 전) 집계를 읽고 모두
        # 통과해, 위의 분당 버킷이 막는 "얼마나 빨리"와 다른 문제 —
        # "얼마나 많이"가 새어나간다. 잠금은 트랜잭션 범위라 이 커밋(또는
        # release_reservation 의 롤백)이 곧 해제이므로, 로컬 쿼리 몇 번
        # 뒤에는 곧바로 풀린다 — 뒤이은 embed/retrieve/rerank/generate 는
        # 잠금 없이 실행된다. LLM 호출은 수십 초가 걸릴 수 있어서, 그
        # 동안 커넥션을 붙잡고 이 사용자의 다른 요청까지 세우는 것은
        # 스펙이 명시적으로 금지한 트레이드오프다.
        try:
            await usage.acquire_quota_lock(self.session, self.user.id, "query")
            # 미인증 계정은 다른 한도를 다른 창으로 센다 — 하루가 아니라
            # 계정 수명 전체. 그래야 재가입으로 초기화되지 않는다.
            if not self.user.email_verified:
                if await usage.unverified_query_exceeded(self.session, self.user.id):
                    raise VERIFICATION_REQUIRED
            elif await usage.query_quota_exceeded(self.session, self.user.id):
                raise too_many("daily query quota exceeded")

            self._reservation_id = await usage.reserve(
                self.session, self.user.id, "query"
            )
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise

    async def resolve_scope(self) -> None:
        """A scope the caller does not own is 404, same as the documents API.

        Checked before any embedding: it avoids spending on an unusable scope
        and keeps the cache from being keyed to a document that does not exist.
        """
        if self.document_id is None:
            return
        owned = await ingest.get_document(
            self.session, self.document_id, user_id=self.user.id
        )
        if owned is None:
            raise NOT_FOUND

    async def embed(self) -> None:
        """Embed once; the dense vector serves the cache probe and retrieval."""
        async with self.watch.time("embed"):
            with _reachable():
                if self.use_hybrid:
                    self.dense, self.sparse = await embeddings.embed_query_full(
                        self.question
                    )
                else:
                    self.dense = await embeddings.embed_query(self.question)

    async def cached_answer(self) -> cache.CachedAnswer | None:
        return await cache.lookup(
            self.session,
            user_id=self.user.id,
            document_id=self.document_id,
            question_embedding=self.dense,
        )

    async def record_cache_hit(self, hit: cache.CachedAnswer) -> None:
        # Counts against the quota but costs no LLM call. Turns the
        # reservation enforce_limits already made into the real record rather
        # than inserting a second row for the same request.
        await usage.commit_reservation(self.session, self._reservation_id, cached=True)
        self._reservation_id = None
        self.draft.cached = True
        self.draft.refused = hit.refused
        self.draft.answer = hit.answer
        await tracing.record(self.session, self.draft, self.watch)

    async def retrieve(self, *, limit: int | None = None) -> Retrieval:
        """Retrieve the context for this question.

        ``limit`` caps how many chunks come back, defaulting to the configured
        context size (``RERANK_TOP`` on the hybrid path, ``TOP_K`` on the dense
        one). Only the MCP tool passes it: an agent asking for more evidence is
        a legitimate request the HTTP endpoints have no way to express, because
        their context size is a tuned property of *our* prompt, not a caller's
        choice. The cap is applied to the returned context only — the per-stage
        records keep everything each stage actually saw, so a trace still shows
        a chunk that retrieval found and the cap cut off.
        """
        # 관측 전용(M7 W7). 이 값이 정해지는 유일한 자리라 여기서 적는다 —
        # 스팬 속성으로만 나가고 traces 에는 컬럼이 없다(tracing.TraceDraft).
        self.draft.top_k = limit or (
            settings.rerank_top if self.use_hybrid else settings.top_k
        )

        if not self.use_hybrid or self.sparse is None:
            async with self.watch.time("retrieve"):
                hits = await retrieve.search(
                    self.session,
                    self.dense,
                    user_id=self.user.id,
                    document_id=self.document_id,
                )
            # 여기서 자르는 것은 반환 컨텍스트뿐이다. SQL 의 LIMIT 을 대신
            # 건드리지 않는 이유는 dense 경로의 TOP_K(8)가 이미 작아서 아낄
            # 것이 없고, retrieve.search() 의 호출 모양을 바꾸면 그 시그니처에
            # 기대고 있는 기존 테스트들이 깨지기 때문이다.
            return Retrieval(hits[:limit] if limit else hits, {}, {STAGE_DENSE: hits})

        async with self.watch.time("retrieve"):
            fused = await retrieve.hybrid_search(
                self.session,
                self.dense,
                self.sparse,
                user_id=self.user.id,
                document_id=self.document_id,
            )

        stage_ids = {STAGE_DENSE: fused.dense_ids, STAGE_SPARSE: fused.sparse_ids}
        if not fused.candidates:
            return Retrieval([], stage_ids, {})

        async with self.watch.time("rerank"):
            with _reachable():
                ranked = await rerank.rerank(
                    self.question, [c.content for c in fused.candidates]
                )
        top: list[retrieve.RetrievedChunk] = []
        for idx, score in ranked[: limit or settings.rerank_top]:
            chunk = fused.candidates[idx]
            chunk.score = score  # replace RRF score with reranker relevance
            top.append(chunk)

        return Retrieval(
            top, stage_ids, {STAGE_RRF: fused.candidates, STAGE_RERANK: top}
        )

    @property
    def grounding_floor(self) -> float | None:
        """The hybrid path scores are reranker sigmoids, not cosine."""
        return settings.rerank_min_score if self.use_hybrid else None

    async def finalize(
        self,
        *,
        found: Retrieval,
        answer: str,
        refused: bool,
        citations: list[dict],
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        """Record usage, populate the cache, and write the trace."""
        # Turns the reservation enforce_limits made into the real record —
        # the row already exists, so this fills in what was not known yet at
        # reservation time instead of inserting a second one.
        await usage.commit_reservation(
            self.session,
            self._reservation_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        self._reservation_id = None
        await cache.store(
            self.session,
            user_id=self.user.id,
            document_id=self.document_id,
            question=self.question,
            question_embedding=self.dense,
            answer=answer,
            refused=refused,
            citations=citations,
        )

        self.draft.refused = refused
        self.draft.answer = answer
        self.draft.llm_model = settings.llm_model
        self.draft.tokens_in = tokens_in
        self.draft.tokens_out = tokens_out
        self.draft.chunk_count = len(found.chunks)  # 관측 전용
        self.draft.stage_ids = found.stage_ids
        self.draft.stage_chunks = found.stage_chunks
        await tracing.record(self.session, self.draft, self.watch)

    async def finalize_search(self, *, found: Retrieval) -> None:
        """Close out a request that stopped at retrieval (the MCP tool).

        Same accounting as ``finalize`` minus the two things that only exist
        because of generation:

        *Semantic cache.* ``cache.store`` keys an **answer** to a question
        embedding. There is no answer here — the calling agent writes it — so
        there is nothing to store, and a probe would be worse than useless: a
        hit would hand the agent a previous answer's prose when it asked for
        chunks. The MCP path therefore neither reads nor writes the cache,
        which is also why this runner never calls ``cached_answer``.

        *Token spend.* ``tokens_in``/``tokens_out`` stay 0 because no model was
        called. They are a real 0, not a missing measurement.

        The trace is written exactly as the HTTP paths write theirs, because
        W4/W5 pull the L3 numbers (툴 선택 정확도, 호출 수) out of these rows —
        not recording here would mean re-instrumenting later against traffic
        that is already gone. Which consumer wrote a row is ``traces.source``
        (마이그레이션 0011), set from the ``source`` this runner was built
        with — so selecting MCP traffic is ``WHERE source = 'mcp_search'``
        and stays true no matter what a fourth consumer does.
        """
        await usage.commit_reservation(self.session, self._reservation_id)
        self._reservation_id = None
        self.draft.chunk_count = len(found.chunks)  # 관측 전용
        self.draft.stage_ids = found.stage_ids
        self.draft.stage_chunks = found.stage_chunks
        await tracing.record(self.session, self.draft, self.watch)

    async def release_reservation(self) -> None:
        """Undo the quota reservation if the work it stood for never finished.

        Callers must reach this from every failure path between
        ``enforce_limits`` and whichever of ``record_cache_hit``/``finalize``/
        ``finalize_search`` the request was headed for — embedding a 503,
        retrieval raising, the LLM call failing partway through a stream — or
        the reservation commits the request never earned. Safe to call
        unconditionally from an ``except``/``finally``: once one of those three
        has run, ``_reservation_id`` is already ``None`` and this is a no-op,
        which is what lets every caller call it blindly on any exception
        without first working out which of them ran. It is equally a no-op when
        ``enforce_limits`` itself is what failed, since no reservation stands.
        """
        if self._reservation_id is None:
            return
        await usage.release_reservation(self.session, self._reservation_id)
        self._reservation_id = None
