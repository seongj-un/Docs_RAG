"""pgvector cosine retrieval over chunks.

M1 is dense-only. The optional ``document_id`` filter narrows search to one
document; when omitted, search spans all documents (single-user in M1).
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Chunk


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
