"""Answer generation with grounding, citations, and a refusal guardrail.

Flow:
1. Drop chunks below MIN_SCORE. If nothing survives, refuse *without* calling
   the LLM (cheap, deterministic no-context refusal).
2. Otherwise assemble a labelled context and ask the LLM to answer only from it,
   citing pages as ``[p.N]`` and refusing when the context lacks the answer.
3. Detect the refusal sentence in the output and surface ``refused`` + drop
   citations so callers never show sources for a "not found" answer.
"""

import uuid
from dataclasses import dataclass

from app.config import settings
from app.services import llm
from app.services.retrieve import RetrievedChunk

REFUSAL_TEXT = "제공된 문서에서 찾을 수 없습니다."

SYSTEM_PROMPT = (
    "당신은 문서 기반 질의응답 어시스턴트입니다.\n"
    "규칙:\n"
    "- 제공된 컨텍스트 안의 정보로만 답한다.\n"
    f'- 근거가 없으면 지어내지 말고 "{REFUSAL_TEXT}" 라고만 답한다.\n'
    "- 각 핵심 주장에는 근거 페이지를 [p.N] 형식으로 표기한다.\n"
    "- 한국어로 답한다."
)


@dataclass
class Citation:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    page_from: int | None
    page_to: int | None
    snippet: str


@dataclass
class Answer:
    answer: str
    refused: bool
    citations: list[Citation]


def _snippet(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _build_context(chunks: list[RetrievedChunk]) -> str:
    blocks = []
    for c in chunks:
        page = c.page_from if c.page_from == c.page_to else f"{c.page_from}-{c.page_to}"
        blocks.append(f"(chunk_id={c.chunk_id}, p.{page})\n{c.content}")
    return "\n\n---\n\n".join(blocks)


def _citations(chunks: list[RetrievedChunk]) -> list[Citation]:
    return [
        Citation(
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            page_from=c.page_from,
            page_to=c.page_to,
            snippet=_snippet(c.content),
        )
        for c in chunks
    ]


async def answer_question(question: str, chunks: list[RetrievedChunk]) -> Answer:
    grounded = [c for c in chunks if c.score >= settings.min_score]

    if not grounded:
        return Answer(answer=REFUSAL_TEXT, refused=True, citations=[])

    context = _build_context(grounded)
    user_prompt = f"질문: {question}\n\n컨텍스트:\n{context}"
    output = await llm.generate(SYSTEM_PROMPT, user_prompt)

    refused = (not output) or (REFUSAL_TEXT in output)
    if refused:
        return Answer(answer=output or REFUSAL_TEXT, refused=True, citations=[])

    return Answer(answer=output, refused=False, citations=_citations(grounded))
