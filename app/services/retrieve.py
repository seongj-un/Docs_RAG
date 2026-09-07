"""Retrieval over chunks: M1 dense cosine + M2 dense/sparse RRF hybrid.

**Tenant isolation lives here.** Both entry points take ``user_id`` as a
required keyword-only argument and join through ``documents`` to filter on the
owner, so a caller cannot accidentally search the whole corpus: omitting the
argument is a TypeError, and passing it positionally is impossible.

The optional ``document_id`` narrows to a single document *within* what the
user already owns; it never widens access.
"""

import uuid
from dataclasses import dataclass

from pgvector import SparseVector
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Chunk, Document
from app.services.fusion import reciprocal_rank_fusion


@dataclass
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    page_from: int | None
    page_to: int | None
    content: str
    score: float


def _owned_chunks(user_id: uuid.UUID, document_id: uuid.UUID | None):
    """Base SELECT restricted to chunks of documents owned by ``user_id``."""
    stmt = select(Chunk).join(Document, Chunk.document_id == Document.id).where(
        Document.user_id == user_id
    )
    if document_id is not None:
        stmt = stmt.where(Chunk.document_id == document_id)
    return stmt


def _to_retrieved(chunk: Chunk, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        page_from=chunk.page_from,
        page_to=chunk.page_to,
        content=chunk.content,
        score=score,
    )


async def search(
    session: AsyncSession,
    query_embedding: list[float],
    *,
    user_id: uuid.UUID,
    document_id: uuid.UUID | None = None,
    top_k: int | None = None,
) -> list[RetrievedChunk]:
    """Dense cosine top-k over the user's own chunks."""
    top_k = top_k or settings.top_k

    distance = Chunk.embedding.cosine_distance(query_embedding)
    stmt = (
        _owned_chunks(user_id, document_id)
        .add_columns(distance.label("distance"))
        .order_by(distance)
        .limit(top_k)
    )

    result = await session.execute(stmt)
    # cosine distance in [0, 2]; similarity score = 1 - distance.
    return [_to_retrieved(chunk, 1.0 - float(dist)) for chunk, dist in result.all()]


async def hybrid_search(
    session: AsyncSession,
    dense_embedding: list[float],
    sparse_embedding: SparseVector,
    *,
    user_id: uuid.UUID,
    document_id: uuid.UUID | None = None,
    cand_k: int | None = None,
) -> list[RetrievedChunk]:
    """Dense + sparse first-stage retrieval fused with RRF, scoped to the user.

    Runs a dense cosine top-N and a sparse inner-product top-N over the user's
    own chunks, then fuses the two rankings by chunk id via Reciprocal Rank
    Fusion. Returns up to ``cand_k`` fused candidates (``score`` = RRF score)
    for the reranker to reorder. Rows without a sparse vector (un-backfilled M1
    rows) are simply absent from the sparse ranking.
    """
    cand_k = cand_k or settings.cand_k

    dense_stmt = (
        _owned_chunks(user_id, document_id)
        .order_by(Chunk.embedding.cosine_distance(dense_embedding))
        .limit(cand_k)
    )
    sparse_stmt = (
        _owned_chunks(user_id, document_id)
        .where(Chunk.sparse_embedding.isnot(None))
        .order_by(Chunk.sparse_embedding.max_inner_product(sparse_embedding))
        .limit(cand_k)
    )

    dense_rows = list((await session.execute(dense_stmt)).scalars().all())
    sparse_rows = list((await session.execute(sparse_stmt)).scalars().all())

    by_id: dict[uuid.UUID, Chunk] = {r.id: r for r in dense_rows}
    by_id.update({r.id: r for r in sparse_rows})

    fused = reciprocal_rank_fusion(
        [[r.id for r in dense_rows], [r.id for r in sparse_rows]],
        k=settings.rrf_k,
    )
    return [_to_retrieved(by_id[cid], score) for cid, score in fused[:cand_k]]
