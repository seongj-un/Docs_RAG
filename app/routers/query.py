"""One-shot query endpoint.

Request path:
    rate limit -> quota -> scope check -> embed -> [cache hit? return] ->
    retrieve -> rerank -> generate -> record usage -> store cache -> trace

All of that lives in ``services/pipeline.QueryRunner``, shared with the
conversation endpoint so the two cannot drift apart on limits or isolation.
This route only adds the non-streaming response shape.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.models import User
from app.schemas import Citation, QueryRequest, QueryResponse
from app.services import generate
from app.services.pipeline import QueryRunner

logger = logging.getLogger(__name__)

router = APIRouter(tags=["query"])


def _log_failure(exc: BaseException) -> None:
    """Say on the server why this query failed.

    This endpoint used to re-raise in silence, so a failed one-shot query left
    **nothing**: no trace row, and not one line of app log. The streaming
    endpoint learned this lesson already (routers/conversations.py) — "the
    access log shows 200, and afterwards there is no way to answer 'why did
    that fail?'" — and the same repository then left the other path exactly
    where it had been. Here the status does reach the access log, but the
    *reason* did not reach anything; a 503 from a dead rerank server and a 503
    from a dead embedder are one line apart in the code and identical in the
    log. No identifier is spelled out because the request id is stamped on
    every record (app/logging.py), which is what makes this line joinable to
    Caddy's, uvicorn's and the trace row.

    Three levels, because they are three different events: a refused request,
    a bug, and a client that left.
    """
    if isinstance(exc, HTTPException):
        logger.warning("query failed: %s %s", exc.status_code, exc.detail)
    elif isinstance(exc, asyncio.CancelledError):
        # No traceback: nobody hung up in error, and an exc_info here would
        # bury the real crashes below in noise from every closed tab.
        logger.info("query abandoned before it finished")
    else:
        logger.exception("query crashed")


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
    try:
        await runner.enforce_limits(
            request.client.host if request.client else "unknown"
        )
    except BaseException as exc:
        # Logged, but deliberately not traced — the two decisions are
        # separate. A rejection here has its own line because the streaming
        # endpoint has had one all along and "which limit was it" is the part
        # the access log cannot show: the three refusals this raises (burst
        # limit, daily quota, unverified account) are all one status apart in
        # the body and identical in the log. It gets no ``traces`` row for the
        # reason ``QueryRunner.record_failure`` sets out: nothing was admitted
        # and nothing ran, and a row per rejection would let a client stuck in
        # a retry loop turn a cheap refusal into unbounded database writes.
        _log_failure(exc)
        raise
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
    except BaseException as exc:
        # 순서가 곧 우선순위다. 로그는 동기이고 던지지 않으므로 가장 먼저
        # — 뒤의 두 줄이 어떻게 되든 서버에는 한 줄이 남는다. 그다음이
        # 예약 해제: 사용자의 쿼터가 걸린 정확성 문제라 최선 노력인 기록이
        # 이걸 밀어내면 안 된다. 트레이스 기록은 마지막이고, 덤으로
        # release_reservation 이 먼저 롤백을 해준 덕에 (원래 실패가 망가뜨린)
        # 트랜잭션이 정리된 세션에서 insert 하게 된다.
        _log_failure(exc)
        await runner.release_reservation()
        await runner.record_failure(exc)
        raise
