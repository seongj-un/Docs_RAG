"""Query endpoint.

Request path:
    rate limit -> quota -> embed -> [semantic cache hit? return] ->
    retrieve -> rerank -> generate -> record usage -> store cache -> trace

Two retrieval paths, selectable per-request or by ``HYBRID_ENABLED``:
- hybrid (M2): dense+sparse -> RRF fusion -> cross-encoder rerank -> top-K.
- dense-only (M1): cosine top-K.

Grounding/refusal is applied in ``generate`` against ``MIN_SCORE``; on the
hybrid path that floor is applied to the reranker's relevance score, on the
dense path to cosine similarity.

Every request is traced (M4): per-stage latency plus the chunk ids each stage
saw, which is what makes retrieval-vs-generation failures separable later.
"""

import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from pgvector import SparseVector
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.deps import get_current_user
from app.models import User
from app.schemas import Citation, QueryRequest, QueryResponse
from app.services import (
    cache,
    embeddings,
    generate,
    ingest,
    rerank,
    retrieve,
    tracing,
    usage,
)
from app.services.ratelimit import query_limiter
from app.services.tracing import (
    STAGE_DENSE,
    STAGE_RERANK,
    STAGE_RRF,
    STAGE_SPARSE,
    Stopwatch,
    TraceDraft,
)

router = APIRouter(tags=["query"])


def _too_many(detail: str) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": "60"},
    )


@dataclass
class _Retrieval:
    chunks: list[retrieve.RetrievedChunk]
    stage_ids: dict[str, list[uuid.UUID]]
    stage_chunks: dict[str, list[retrieve.RetrievedChunk]]


async def _retrieve_chunks(
    session: AsyncSession,
    body: QueryRequest,
    use_hybrid: bool,
    user_id: uuid.UUID,
    dense: list[float],
    sparse: SparseVector | None,
    watch: Stopwatch,
) -> _Retrieval:
    if not use_hybrid or sparse is None:
        async with watch.time("retrieve"):
            hits = await retrieve.search(
                session, dense, user_id=user_id, document_id=body.document_id
            )
        return _Retrieval(hits, {}, {STAGE_DENSE: hits})

    async with watch.time("retrieve"):
        fused = await retrieve.hybrid_search(
            session, dense, sparse, user_id=user_id, document_id=body.document_id
        )

    stage_ids = {STAGE_DENSE: fused.dense_ids, STAGE_SPARSE: fused.sparse_ids}
    if not fused.candidates:
        return _Retrieval([], stage_ids, {})

    async with watch.time("rerank"):
        ranked = await rerank.rerank(
            body.question, [c.content for c in fused.candidates]
        )
    top: list[retrieve.RetrievedChunk] = []
    for idx, score in ranked[: settings.rerank_top]:
        chunk = fused.candidates[idx]
        chunk.score = score  # replace RRF score with reranker relevance
        top.append(chunk)

    return _Retrieval(
        top, stage_ids, {STAGE_RRF: fused.candidates, STAGE_RERANK: top}
    )


@router.post("/query", response_model=QueryResponse)
async def query(
    body: QueryRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> QueryResponse:
    client_ip = request.client.host if request.client else "unknown"
    if not query_limiter.allow(f"user:{user.id}") or not query_limiter.allow(
        f"ip:{client_ip}"
    ):
        raise _too_many("query rate limit exceeded")

    if await usage.query_quota_exceeded(session, user.id):
        raise _too_many("daily query quota exceeded")

    # A scope the caller does not own is 404 — same rule as the documents API,
    # so a foreign document is indistinguishable from a missing one. Checked up
    # front: it also avoids spending an embedding on an unusable scope, and
    # keeps the cache from being keyed to a document that does not exist.
    if body.document_id is not None:
        owned = await ingest.get_document(session, body.document_id, user_id=user.id)
        if owned is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND, detail="not found"
            )

    use_hybrid = body.hybrid if body.hybrid is not None else settings.hybrid_enabled
    watch = Stopwatch()
    draft = TraceDraft(
        user_id=user.id,
        question=body.question,
        document_id=body.document_id,
        hybrid=use_hybrid,
    )

    # Embed once: the dense vector serves both the cache probe and retrieval.
    sparse: SparseVector | None = None
    async with watch.time("embed"):
        if use_hybrid:
            dense, sparse = await embeddings.embed_query_full(body.question)
        else:
            dense = await embeddings.embed_query(body.question)

    hit = await cache.lookup(
        session,
        user_id=user.id,
        document_id=body.document_id,
        question_embedding=dense,
    )
    if hit is not None:
        # Counts against the quota but costs no LLM call.
        await usage.record(session, user.id, "query", cached=True)
        draft.cached = True
        draft.refused = hit.refused
        draft.answer = hit.answer
        await tracing.record(session, draft, watch)
        return QueryResponse(
            answer=hit.answer,
            refused=hit.refused,
            citations=[Citation(**c) for c in hit.citations],
        )

    found = await _retrieve_chunks(
        session, body, use_hybrid, user.id, dense, sparse, watch
    )

    async with watch.time("generate"):
        # The hybrid path's scores are reranker sigmoids, not cosine, so it
        # carries its own floor.
        result = await generate.answer_question(
            body.question,
            found.chunks,
            min_score=settings.rerank_min_score if use_hybrid else None,
        )

    citations = [
        Citation(
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            page_from=c.page_from,
            page_to=c.page_to,
            snippet=c.snippet,
        )
        for c in result.citations
    ]

    await usage.record(
        session,
        user.id,
        "query",
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
    )
    await cache.store(
        session,
        user_id=user.id,
        document_id=body.document_id,
        question=body.question,
        question_embedding=dense,
        answer=result.answer,
        refused=result.refused,
        citations=[c.model_dump(mode="json") for c in citations],
    )

    draft.refused = result.refused
    draft.answer = result.answer
    draft.llm_model = settings.llm_model
    draft.tokens_in = result.tokens_in
    draft.tokens_out = result.tokens_out
    draft.stage_ids = found.stage_ids
    draft.stage_chunks = found.stage_chunks
    await tracing.record(session, draft, watch)

    return QueryResponse(
        answer=result.answer, refused=result.refused, citations=citations
    )
