"""Chunk read endpoint.

Exists for the M5 evidence modal. A citation carries a 240-character snippet,
which is right for inline display but not for "이 답변의 근거" — the modal shows
the chunk the answer was grounded in, so it needs the whole thing.

Owner-scoped through the chunk's document: a chunk belonging to someone else
answers 404, never 403, so the API never confirms that it exists.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.models import Chunk, Document, User
from app.schemas import ChunkOut

router = APIRouter(prefix="/chunks", tags=["chunks"])


@router.get("/{chunk_id}", response_model=ChunkOut)
async def get_chunk(
    chunk_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Chunk:
    result = await session.execute(
        select(Chunk)
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.id == chunk_id, Document.user_id == user.id)
    )
    chunk = result.scalars().first()
    if chunk is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="not found"
        )
    return chunk
