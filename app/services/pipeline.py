"""Shared query pipeline.

``POST /query`` (one-shot) and ``POST /conversations/{id}/query`` (streaming,
persisted) must behave identically on everything that is not presentation:
rate limits, quota, scope ownership, the semantic cache, retrieval, usage
accounting and tracing. Keeping that in one place is what stops the two
endpoints from drifting — a limit enforced on one path but not the other is a
hole, not a difference.

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
from app.models import User
from app.services import cache, embeddings, ingest, rerank, retrieve, tracing, usage
from app.services.upstream import UpstreamUnavailable
from app.services.ratelimit import query_limiter
from app.services.tracing import (
    STAGE_DENSE,
    STAGE_RERANK,
    STAGE_RRF,
    STAGE_SPARSE,
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
    ) -> None:
        self.session = session
        self.user = user
        self.question = question
        self.document_id = document_id
        self.use_hybrid = hybrid if hybrid is not None else settings.hybrid_enabled
        self.watch = Stopwatch()
        self.draft = TraceDraft(
            user_id=user.id,
            question=question,
            document_id=document_id,
            hybrid=self.use_hybrid,
        )
        self.dense: list[float] = []
        self.sparse: SparseVector | None = None

    async def enforce_limits(self, client_ip: str) -> None:
        if not query_limiter.allow(f"user:{self.user.id}") or not query_limiter.allow(
            f"ip:{client_ip}"
        ):
            raise too_many("query rate limit exceeded")
        if await usage.query_quota_exceeded(self.session, self.user.id):
            raise too_many("daily query quota exceeded")

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
        # Counts against the quota but costs no LLM call.
        await usage.record(self.session, self.user.id, "query", cached=True)
        self.draft.cached = True
        self.draft.refused = hit.refused
        self.draft.answer = hit.answer
        await tracing.record(self.session, self.draft, self.watch)

    async def retrieve(self) -> Retrieval:
        if not self.use_hybrid or self.sparse is None:
            async with self.watch.time("retrieve"):
                hits = await retrieve.search(
                    self.session,
                    self.dense,
                    user_id=self.user.id,
                    document_id=self.document_id,
                )
            return Retrieval(hits, {}, {STAGE_DENSE: hits})

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
        for idx, score in ranked[: settings.rerank_top]:
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
        await usage.record(
            self.session,
            self.user.id,
            "query",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
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
        self.draft.stage_ids = found.stage_ids
        self.draft.stage_chunks = found.stage_chunks
        await tracing.record(self.session, self.draft, self.watch)
