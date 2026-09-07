"""Semantic cache for answers.

A new question that is near-identical in embedding space to a past one reuses
that answer, skipping retrieval, reranking, and the LLM call entirely — the
largest single cost saving available, since generation dominates.

Two safety properties matter more than the hit rate:
- **Scope.** Entries are keyed by (user_id, document_id). A lookup filters on
  both, so a hit can never cross tenants and never answers from a different
  document scope than the caller asked about.
- **Invalidation.** Entries cascade from users and documents, so deleting a
  document drops answers derived from it. Corpus-wide entries (document_id
  NULL) are dropped explicitly when the user's corpus changes, since no foreign
  key covers "all my documents".
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import QueryCache


@dataclass
class CachedAnswer:
    answer: str
    refused: bool
    citations: list[dict]
    similarity: float


async def lookup(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    document_id: uuid.UUID | None,
    question_embedding: list[float],
) -> CachedAnswer | None:
    """Return the nearest cached answer above the similarity threshold."""
    if not settings.semantic_cache_enabled:
        return None

    distance = QueryCache.question_embedding.cosine_distance(question_embedding)
    stmt = (
        select(QueryCache, distance.label("distance"))
        .where(QueryCache.user_id == user_id)
        .order_by(distance)
        .limit(1)
    )
    # NULL means "across all my documents"; it must not match a scoped entry.
    stmt = stmt.where(
        QueryCache.document_id.is_(None)
        if document_id is None
        else QueryCache.document_id == document_id
    )

    row = (await session.execute(stmt)).first()
    if row is None:
        return None

    entry, distance_value = row
    similarity = 1.0 - float(distance_value)
    if similarity < settings.semantic_cache_threshold:
        return None
    return CachedAnswer(
        answer=entry.answer,
        refused=entry.refused,
        citations=list(entry.citations or []),
        similarity=similarity,
    )


async def store(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    document_id: uuid.UUID | None,
    question: str,
    question_embedding: list[float],
    answer: str,
    refused: bool,
    citations: list[dict],
) -> None:
    if not settings.semantic_cache_enabled:
        return
    session.add(
        QueryCache(
            user_id=user_id,
            document_id=document_id,
            question=question,
            question_embedding=question_embedding,
            answer=answer,
            refused=refused,
            citations=citations,
        )
    )
    await session.commit()


async def invalidate_corpus_wide(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Drop this user's corpus-wide (document_id NULL) entries.

    Called when their document set changes: those answers were computed over a
    corpus that no longer exists. Document-scoped entries need no help here —
    they cascade with the document.
    """
    await session.execute(
        delete(QueryCache).where(
            QueryCache.user_id == user_id, QueryCache.document_id.is_(None)
        )
    )
    await session.commit()
