"""Retrieval over chunks: M1 dense cosine + M2 dense/sparse RRF hybrid.

The optional ``document_id`` filter narrows search to one document; when
omitted, search spans all documents (single-user in M1/M2).
"""

import uuid
from dataclasses import dataclass

from pgvector import SparseVector
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Chunk
from app.services.fusion import reciprocal_rank_fusion


@dataclass
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    page_from: int | None
    page_to: int | None
    content: str
    score: float


async def search(
    session: AsyncSession,
    query_embedding: list[float],
    document_id: uuid.UUID | None = None,
    top_k: int | None = None,
) -> list[RetrievedChunk]:
    top_k = top_k or settings.top_k

    # cosine distance in [0, 2]; similarity score = 1 - distance.
    distance = Chunk.embedding.cosine_distance(query_embedding)
    stmt = select(Chunk, distance.label("distance"))
    if document_id is not None:
        stmt = stmt.where(Chunk.document_id == document_id)
    stmt = stmt.order_by(distance).limit(top_k)

    result = await session.execute(stmt)
    retrieved: list[RetrievedChunk] = []
    for chunk, dist in result.all():
        retrieved.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                page_from=chunk.page_from,
                page_to=chunk.page_to,
                content=chunk.content,
                score=1.0 - float(dist),
            )
        )
    return retrieved


def _to_retrieved(chunk: Chunk, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        page_from=chunk.page_from,
        page_to=chunk.page_to,
        content=chunk.content,
        score=score,
    )


async def hybrid_search(
    session: AsyncSession,
    dense_embedding: list[float],
    sparse_embedding: SparseVector,
    document_id: uuid.UUID | None = None,
    cand_k: int | None = None,
) -> list[RetrievedChunk]:
    """Dense + sparse first-stage retrieval fused with RRF.

    Runs a dense cosine top-N and a sparse inner-product top-N, then fuses the
    two rankings by chunk id via Reciprocal Rank Fusion. Returns up to ``cand_k``
    fused candidates (``score`` = RRF score) for the reranker to reorder. Rows
    without a sparse vector (un-backfilled M1 rows) are simply absent from the
    sparse ranking.
    """
    cand_k = cand_k or settings.cand_k

    dense_dist = Chunk.embedding.cosine_distance(dense_embedding)
    dense_stmt = select(Chunk)
    if document_id is not None:
        dense_stmt = dense_stmt.where(Chunk.document_id == document_id)
    dense_stmt = dense_stmt.order_by(dense_dist).limit(cand_k)

    sparse_neg_ip = Chunk.sparse_embedding.max_inner_product(sparse_embedding)
    sparse_stmt = select(Chunk).where(Chunk.sparse_embedding.isnot(None))
    if document_id is not None:
        sparse_stmt = sparse_stmt.where(Chunk.document_id == document_id)
    sparse_stmt = sparse_stmt.order_by(sparse_neg_ip).limit(cand_k)

    dense_rows = list((await session.execute(dense_stmt)).scalars().all())
    sparse_rows = list((await session.execute(sparse_stmt)).scalars().all())

    by_id: dict[uuid.UUID, Chunk] = {r.id: r for r in dense_rows}
    by_id.update({r.id: r for r in sparse_rows})

    fused = reciprocal_rank_fusion(
        [[r.id for r in dense_rows], [r.id for r in sparse_rows]],
        k=settings.rrf_k,
    )
    return [_to_retrieved(by_id[cid], score) for cid, score in fused[:cand_k]]
