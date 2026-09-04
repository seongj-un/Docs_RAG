"""Gemini generation client (google-genai).

Thin wrapper so ``generate.py`` stays provider-agnostic. The client is created
lazily so importing this module never requires an API key (imports run at app
startup and in tests without secrets).
"""

from functools import lru_cache

from google import genai

from app.config import settings


@lru_cache(maxsize=1)
def _client() -> genai.Client:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=settings.gemini_api_key)


async def generate(system_prompt: str, user_prompt: str) -> str:
    """Generate a completion. Returns the model's text output."""
    client = _client()
    resp = await client.aio.models.generate_content(
        model=settings.llm_model,
        contents=user_prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.0,
        ),
    )
    return (resp.text or "").strip()
