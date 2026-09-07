"""Gemini generation client (google-genai).

Thin wrapper so ``generate.py`` stays provider-agnostic. The client is created
lazily so importing this module never requires an API key (imports run at app
startup and in tests without secrets).
"""

from collections.abc import AsyncIterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache

from fastapi import HTTPException, status as http_status
from google import genai
from google.genai import errors as genai_errors

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


def _retry_after(exc: genai_errors.APIError) -> str | None:
    """The provider's own retry delay, when it sends one.

    Per-minute exhaustion comes back with a RetryInfo of a few seconds; the
    daily cap sends none, because the answer is "tomorrow". Passing along only
    what the provider actually said beats inventing a number.
    """
    try:
        for detail in exc.details["error"]["details"]:
            delay = detail.get("retryDelay")
            if detail.get("@type", "").endswith("RetryInfo") and delay:
                return str(int(float(delay.rstrip("s"))))
    except Exception:  # noqa: BLE001 - shape is the provider's to change
        pass
    return None


@contextmanager
def _provider_errors():
    """Report the provider's refusals as the provider's, not as our crash.

    Two upstream conditions are routine and neither is a bug here: the free
    tier running out of budget (429) and the model being overloaded (503).
    Both used to reach the caller as a bare "Internal Server Error", which
    says the wrong thing — it blames this service and offers no next step.
    Mapped through, they land on paths both routers already handle (the SSE
    path carries the status in-band) and on copy the UI can phrase usefully.

    Anything else is left alone: an unrecognised failure should still be
    loud rather than dressed up as a temporary one.
    """
    try:
        yield
    except genai_errors.ClientError as exc:
        if exc.code != http_status.HTTP_429_TOO_MANY_REQUESTS:
            raise
        raise _retryable(exc, http_status.HTTP_429_TOO_MANY_REQUESTS,
                         "model quota exceeded") from exc
    except genai_errors.ServerError as exc:
        raise _retryable(exc, http_status.HTTP_503_SERVICE_UNAVAILABLE,
                         "model unavailable") from exc


def _retryable(
    exc: genai_errors.APIError, status_code: int, detail: str
) -> HTTPException:
    after = _retry_after(exc)
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers={"Retry-After": after} if after else None,
    )


async def generate(
    system_prompt: str, user_prompt: str, *, model: str | None = None
) -> Generation:
    """Generate a completion, with token usage when the provider reports it.

    ``model`` overrides the configured production model — used by evaluation,
    which runs on a lighter model with a larger free-tier budget.
    """
    client = _client()
    with _provider_errors():
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
    parts: list[str] = []
    tokens_in = tokens_out = 0
    # The guard spans consumption too: a rejection can arrive when the first
    # chunk is pulled rather than when the stream is opened.
    with _provider_errors():
        stream = await client.aio.models.generate_content_stream(
            model=model or settings.llm_model,
            contents=user_prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.0,
            ),
        )
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
