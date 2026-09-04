"""Query endpoint: embed question -> retrieve -> generate grounded answer."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import Citation, QueryRequest, QueryResponse
from app.services import embeddings, generate, retrieve

router = APIRouter(tags=["query"])


@router.post("/query", response_model=QueryResponse)
async def query(
    body: QueryRequest,
    session: AsyncSession = Depends(get_session),
) -> QueryResponse:
    q_embedding = await embeddings.embed_query(body.question)
    chunks = await retrieve.search(
        session, q_embedding, document_id=body.document_id
    )
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
