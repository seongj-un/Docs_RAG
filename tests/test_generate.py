"""Refusal-guardrail tests for generation (no LLM/infra required)."""

import asyncio
import uuid

from app.config import settings
from app.services import generate
from app.services.retrieve import RetrievedChunk


def _chunk(score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        page_from=1,
        page_to=1,
        content="some grounded content",
        score=score,
    )


def test_no_chunks_refuses_without_calling_llm():
    result = asyncio.run(generate.answer_question("질문?", []))
    assert result.refused is True
    assert result.citations == []
    assert result.answer == generate.REFUSAL_TEXT


def test_below_min_score_refuses_without_calling_llm():
    weak = _chunk(score=settings.min_score - 0.05)
    result = asyncio.run(generate.answer_question("질문?", [weak]))
    assert result.refused is True
    assert result.citations == []


def test_grounded_chunks_invoke_llm(monkeypatch):
    async def fake_generate(system_prompt: str, user_prompt: str) -> str:
        assert "컨텍스트" in user_prompt
        return "답변입니다 [p.1]"

    monkeypatch.setattr(generate.llm, "generate", fake_generate)

    strong = _chunk(score=settings.min_score + 0.5)
    result = asyncio.run(generate.answer_question("질문?", [strong]))
    assert result.refused is False
    assert result.answer == "답변입니다 [p.1]"
    assert len(result.citations) == 1


def test_llm_refusal_text_drops_citations(monkeypatch):
    async def fake_generate(system_prompt: str, user_prompt: str) -> str:
        return generate.REFUSAL_TEXT

    monkeypatch.setattr(generate.llm, "generate", fake_generate)

    strong = _chunk(score=settings.min_score + 0.5)
    result = asyncio.run(generate.answer_question("질문?", [strong]))
    assert result.refused is True
    assert result.citations == []
