"""One-shot query endpoint.

Request path:
    rate limit -> quota -> scope check -> embed -> [cache hit? return] ->
    retrieve -> rerank -> generate -> record usage -> store cache -> trace

All of that lives in ``services/pipeline.QueryRunner``, shared with the
conversation endpoint so the two cannot drift apart on limits or isolation.
This route only adds the non-streaming response shape.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.models import User
from app.schemas import Citation, QueryRequest, QueryResponse
from app.services import generate
from app.services.pipeline import QueryRunner

router = APIRouter(tags=["query"])


def to_citations(citations) -> list[Citation]:
    return [
        Citation(
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            page_from=c.page_from,
            page_to=c.page_to,
            snippet=c.snippet,
        )
        for c in citations
    ]


@router.post("/query", response_model=QueryResponse)
async def query(
    body: QueryRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> QueryResponse:
    runner = QueryRunner(
        session, user, body.question, document_id=body.document_id, hybrid=body.hybrid
    )
    await runner.enforce_limits(request.client.host if request.client else "unknown")
    # enforce_limits already committed a quota reservation for this request.
    # Everything below can fail (a document scope that turns out unowned, the
    # embedding server being down, the LLM call erroring) after that
    # reservation exists but before it is turned into a real record, and a
    # query that did not happen must not spend the quota it would have used.
    try:
        await runner.resolve_scope()
        await runner.embed()

        hit = await runner.cached_answer()
        if hit is not None:
            await runner.record_cache_hit(hit)
            return QueryResponse(
                answer=hit.answer,
                refused=hit.refused,
                citations=[Citation(**c) for c in hit.citations],
            )

        found = await runner.retrieve()
        async with runner.watch.time("generate"):
            result = await generate.answer_question(
                body.question, found.chunks, min_score=runner.grounding_floor
            )

        citations = to_citations(result.citations)
        await runner.finalize(
            found=found,
            answer=result.answer,
            refused=result.refused,
            citations=[c.model_dump(mode="json") for c in citations],
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
        )
        return QueryResponse(
            answer=result.answer, refused=result.refused, citations=citations
        )
    except BaseException:
        await runner.release_reservation()
        raise
