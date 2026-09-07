"""Query endpoint.

Two retrieval paths, selectable per-request or by ``HYBRID_ENABLED``:
- hybrid (M2): embed question (dense+sparse) -> RRF fusion -> cross-encoder
  rerank -> top-K -> grounded generation.
- dense-only (M1): embed question (dense) -> cosine top-K -> grounded generation.

Grounding/refusal is applied in ``generate`` against ``MIN_SCORE``; on the
hybrid path that floor is applied to the reranker's relevance score, on the
dense path to cosine similarity.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.deps import get_current_user
from app.models import User
from app.schemas import Citation, QueryRequest, QueryResponse
from app.services import embeddings, generate, rerank, retrieve

router = APIRouter(tags=["query"])


async def _retrieve_chunks(
    session: AsyncSession,
    body: QueryRequest,
    use_hybrid: bool,
    user_id: uuid.UUID,
) -> list[retrieve.RetrievedChunk]:
    if not use_hybrid:
        q_embedding = await embeddings.embed_query(body.question)
        return await retrieve.search(
            session, q_embedding, user_id=user_id, document_id=body.document_id
        )

    dense, sparse = await embeddings.embed_query_full(body.question)
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
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> QueryResponse:
    use_hybrid = body.hybrid if body.hybrid is not None else settings.hybrid_enabled

    chunks = await _retrieve_chunks(session, body, use_hybrid, user_id=user.id)
    result = await generate.answer_question(body.question, chunks)

    return QueryResponse(
        answer=result.answer,
        refused=result.refused,
        citations=[
            Citation(
                chunk_id=c.chunk_id,
                document_id=c.document_id,
                page_from=c.page_from,
                page_to=c.page_to,
                snippet=c.snippet,
            )
            for c in result.citations
        ],
    )
