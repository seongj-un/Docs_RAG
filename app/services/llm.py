"""Gemini generation client (google-genai).

Thin wrapper so ``generate.py`` stays provider-agnostic. The client is created
lazily so importing this module never requires an API key (imports run at app
startup and in tests without secrets).
"""

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
