"""Query endpoint.

Request path (M3 Phase 2 adds the first two and the cache):
    rate limit -> quota -> embed -> [semantic cache hit? return] ->
    retrieve -> rerank -> generate -> record usage -> store cache

Two retrieval paths, selectable per-request or by ``HYBRID_ENABLED``:
- hybrid (M2): dense+sparse -> RRF fusion -> cross-encoder rerank -> top-K.
- dense-only (M1): cosine top-K.

Grounding/refusal is applied in ``generate`` against ``MIN_SCORE``; on the
hybrid path that floor is applied to the reranker's relevance score, on the
dense path to cosine similarity.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from pgvector import SparseVector
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.deps import get_current_user
from app.models import User
from app.schemas import Citation, QueryRequest, QueryResponse
from app.services import cache, embeddings, generate, rerank, retrieve, usage
from app.services.ratelimit import query_limiter

router = APIRouter(tags=["query"])


def _too_many(detail: str) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": "60"},
    )


async def _retrieve_chunks(
    session: AsyncSession,
    body: QueryRequest,
    use_hybrid: bool,
    user_id: uuid.UUID,
    dense: list[float],
    sparse: SparseVector | None,
) -> list[retrieve.RetrievedChunk]:
    if not use_hybrid or sparse is None:
        return await retrieve.search(
            session, dense, user_id=user_id, document_id=body.document_id
        )

    candidates = await retrieve.hybrid_search(
        session, dense, sparse, user_id=user_id, document_id=body.document_id
    )
    if not candidates:
        return []

    ranked = await rerank.rerank(body.question, [c.content for c in candidates])
    top: list[retrieve.RetrievedChunk] = []
    for idx, score in ranked[: settings.rerank_top]:
        chunk = candidates[idx]
        chunk.score = score  # replace RRF score with reranker relevance
        top.append(chunk)
    return top


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

    use_hybrid = body.hybrid if body.hybrid is not None else settings.hybrid_enabled

    # Embed once: the dense vector serves both the cache probe and retrieval.
    sparse: SparseVector | None = None
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
        return QueryResponse(
            answer=hit.answer,
            refused=hit.refused,
            citations=[Citation(**c) for c in hit.citations],
        )

    chunks = await _retrieve_chunks(session, body, use_hybrid, user.id, dense, sparse)
    result = await generate.answer_question(body.question, chunks)

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

    return QueryResponse(
        answer=result.answer, refused=result.refused, citations=citations
    )
