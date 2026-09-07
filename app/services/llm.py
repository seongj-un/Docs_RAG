"""Gemini generation client (google-genai).

Thin wrapper so ``generate.py`` stays provider-agnostic. The client is created
lazily so importing this module never requires an API key (imports run at app
startup and in tests without secrets).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache

from google import genai

from app.config import settings


@dataclass
class Generation:
    """Model output plus the token counts used for usage accounting."""

    text: str
    tokens_in: int = 0
    tokens_out: int = 0


@lru_cache(maxsize=1)
def _client() -> genai.Client:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=settings.gemini_api_key)


async def generate(
    system_prompt: str, user_prompt: str, *, model: str | None = None
) -> Generation:
    """Generate a completion, with token usage when the provider reports it.

    ``model`` overrides the configured production model — used by evaluation,
    which runs on a lighter model with a larger free-tier budget.
    """
    client = _client()
    resp = await client.aio.models.generate_content(
        model=model or settings.llm_model,
        contents=user_prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.0,
        ),
    )
    meta = getattr(resp, "usage_metadata", None)
    return Generation(
        text=(resp.text or "").strip(),
        tokens_in=getattr(meta, "prompt_token_count", 0) or 0,
        tokens_out=getattr(meta, "candidates_token_count", 0) or 0,
    )


async def generate_stream(
    system_prompt: str, user_prompt: str, *, model: str | None = None
) -> AsyncIterator[str | Generation]:
    """Yield text chunks as they arrive, then a final ``Generation``.

    The terminal value carries the full text and token usage, which only the
    last chunk reports — callers stream the strings and use the ``Generation``
    for persistence and accounting.
    """
    client = _client()
    stream = await client.aio.models.generate_content_stream(
        model=model or settings.llm_model,
        contents=user_prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.0,
        ),
    )

    parts: list[str] = []
    tokens_in = tokens_out = 0
    async for chunk in stream:
        meta = getattr(chunk, "usage_metadata", None)
        if meta is not None:
            tokens_in = getattr(meta, "prompt_token_count", 0) or tokens_in
            tokens_out = getattr(meta, "candidates_token_count", 0) or tokens_out
        text = getattr(chunk, "text", None)
        if text:
            parts.append(text)
            yield text

    yield Generation(
        text="".join(parts).strip(), tokens_in=tokens_in, tokens_out=tokens_out
    )
