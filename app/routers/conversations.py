"""Conversation threads and streaming query.

M5 introduces chat history: ``POST /query`` answers and forgets, but the
sidebar needs threads the user can reopen. Each turn is persisted as two
messages (the question, then the answer with its footnote mapping).

Turns are answered **independently** — prior messages are not fed back into
retrieval or generation. That is the M5 decision: multi-turn needs pronoun and
follow-up resolution, which is query rewriting, which belongs with that work
rather than here.

The query endpoint streams over SSE. Events, in order:
- ``meta``   scope and retrieved-chunk count, before generation starts
- ``token``  text fragments as the model produces them
- ``done``   final answer, refusal flag, and citations
- ``error``  something failed mid-stream (the HTTP status is already 200 by
             then, so failures have to be reported in-band)

Citations ride on ``done`` rather than ``meta``: whether the answer is a
refusal is only known at the end, and a refusal must not carry citations.
"""

import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import SessionLocal, get_session
from app.deps import get_current_user
from app.models import Conversation, Message, User
from app.routers.query import to_citations
from app.schemas import (
    ConversationCreate,
    ConversationDetail,
    ConversationQuery,
    ConversationSummary,
)
from app.services import generate, llm
from app.services.pipeline import NOT_FOUND, QueryRunner

router = APIRouter(prefix="/conversations", tags=["conversations"])

TITLE_MAX = 60


def _title_from(question: str) -> str:
    """First question becomes the thread title, trimmed on a word boundary."""
    text = " ".join(question.split())
    if len(text) <= TITLE_MAX:
        return text
    return text[:TITLE_MAX].rsplit(" ", 1)[0] + "…"


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _owned(session: AsyncSession, conversation_id: uuid.UUID, user: User):
    """Fetch a conversation only if the caller owns it (else 404, not 403)."""
    row = (
        await session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user.id
            )
        )
    ).scalars().first()
    if row is None:
        raise NOT_FOUND
    return row


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Conversation]:
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


@router.post("", status_code=http_status.HTTP_201_CREATED,
             response_model=ConversationSummary)
async def create_conversation(
    body: ConversationCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Conversation:
    if body.scope_document_id is not None:
        # Validate the scope now rather than at first question, so a bad
        # document id fails where the user set it.
        from app.services import ingest

        owned = await ingest.get_document(
            session, body.scope_document_id, user_id=user.id
        )
        if owned is None:
            raise NOT_FOUND

    conversation = Conversation(
        user_id=user.id,
        title=body.title,
        scope_document_id=body.scope_document_id,
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Conversation:
    result = await session.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.id == conversation_id, Conversation.user_id == user.id)
    )
    conversation = result.scalars().first()
    if conversation is None:
        raise NOT_FOUND
    return conversation


@router.delete("/{conversation_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    conversation = await _owned(session, conversation_id, user)
    await session.delete(conversation)  # messages cascade
    await session.commit()


async def _stream_turn(
    conversation_id: uuid.UUID, user: User, body: ConversationQuery, client_ip: str
) -> AsyncIterator[str]:
    """Run one turn, yielding SSE frames.

    Opens its own session: the request-scoped one is closed when the response
    starts streaming, so the generator would outlive it.
    """
    async with SessionLocal() as session:
        try:
            conversation = await _owned(session, conversation_id, user)
            # Absent means "whatever this thread is scoped to"; present means
            # the caller is choosing, and null is a real choice — every
            # document. Reading None as "unset" would leave a thread that was
            # created against one document with no way back to the corpus.
            scope = (
                body.document_id
                if "document_id" in body.model_fields_set
                else conversation.scope_document_id
            )

            runner = QueryRunner(
                session, user, body.question, document_id=scope, hybrid=body.hybrid
            )
            await runner.enforce_limits(client_ip)
            await runner.resolve_scope()

            session.add(
                Message(
                    conversation_id=conversation.id,
                    role="user",
                    content=body.question,
                )
            )
            if not conversation.title:
                conversation.title = _title_from(body.question)
            await session.commit()

            await runner.embed()

            hit = await runner.cached_answer()
            if hit is not None:
                await runner.record_cache_hit(hit)
                yield _sse("meta", {"scope": str(scope) if scope else None,
                                    "cached": True, "chunks": 0})
                yield _sse("token", {"text": hit.answer})
                await _persist_answer(
                    session, conversation.id, hit.answer, hit.refused,
                    hit.citations, scope,
                )
                yield _sse("done", {"answer": hit.answer, "refused": hit.refused,
                                    "citations": hit.citations, "cached": True})
                return

            found = await runner.retrieve()
            yield _sse("meta", {"scope": str(scope) if scope else None,
                                "cached": False, "chunks": len(found.chunks)})

            grounded = generate.grounded_chunks(
                found.chunks, runner.grounding_floor
            )
            if not grounded:
                # Refusal is decided before generation; nothing to stream.
                answer, refused = generate.REFUSAL_TEXT, True
                yield _sse("token", {"text": answer})
                await runner.finalize(found=found, answer=answer, refused=True,
                                      citations=[], tokens_in=0, tokens_out=0)
                await _persist_answer(
                    session, conversation.id, answer, True, [], scope
                )
                yield _sse("done", {"answer": answer, "refused": True,
                                    "citations": [], "cached": False})
                return

            prompt = generate.build_user_prompt(body.question, grounded)
            final: llm.Generation | None = None
            async with runner.watch.time("generate"):
                async for piece in llm.generate_stream(generate.SYSTEM_PROMPT, prompt):
                    if isinstance(piece, llm.Generation):
                        final = piece
                    else:
                        yield _sse("token", {"text": piece})

            text = (final.text if final else "").strip()
            refused = (not text) or (generate.REFUSAL_TEXT in text)
            answer = text or generate.REFUSAL_TEXT
            citations = (
                [] if refused
                else [c.model_dump(mode="json")
                      for c in to_citations(generate.citations_for(grounded))]
            )

            await runner.finalize(
                found=found,
                answer=answer,
                refused=refused,
                citations=citations,
                tokens_in=final.tokens_in if final else 0,
                tokens_out=final.tokens_out if final else 0,
            )
            await _persist_answer(
                session, conversation.id, answer, refused, citations, scope
            )
            yield _sse("done", {"answer": answer, "refused": refused,
                                "citations": citations, "cached": False})

        except HTTPException as exc:
            # The response is already 200 by the time streaming starts, so the
            # status has to be carried in the event instead.
            yield _sse("error", {"status": exc.status_code, "detail": exc.detail})
        except Exception as exc:  # noqa: BLE001 - the client needs *something*
            yield _sse("error", {"status": 500, "detail": f"{type(exc).__name__}"})


async def _persist_answer(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    answer: str,
    refused: bool,
    citations: list[dict],
    scope: uuid.UUID | None,
) -> None:
    """Store the answer along with the scope it was given under.

    The scope is what makes a refusal explainable when the thread is reopened:
    "nothing in this document" and "nothing in any of your documents" are
    different answers, and only the scope tells them apart.
    """
    session.add(
        Message(
            conversation_id=conversation_id,
            role="assistant",
            content=answer,
            citations=citations or None,
            refused=refused,
            scope_document_id=scope,
        )
    )
    await session.commit()


@router.post("/{conversation_id}/query")
async def conversation_query(
    conversation_id: uuid.UUID,
    body: ConversationQuery,
    request: Request,
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    client_ip = request.client.host if request.client else "unknown"
    return StreamingResponse(
        _stream_turn(conversation_id, user, body, client_ip),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # keep proxies from buffering the stream
        },
    )
