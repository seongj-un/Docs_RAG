"""Upstream provider failures must not be reported as this service crashing.

The payloads below are the ones the live API actually returned: a free-tier
daily cap and an overloaded model. Both used to reach the caller as a bare
500 "Internal Server Error".
"""

import asyncio
import types

import pytest
from fastapi import HTTPException
from google.genai import errors as genai_errors

from app.services import llm

QUOTA_BODY = {
    "error": {
        "code": 429,
        "message": "You exceeded your current quota.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": "13.463356142s",
            }
        ],
    }
}
OVERLOAD_BODY = {
    "error": {
        "code": 503,
        "message": "This model is currently experiencing high demand.",
        "status": "UNAVAILABLE",
    }
}


def _raise(exc: Exception) -> None:
    with llm._provider_errors():
        raise exc


def test_quota_becomes_429_with_the_providers_own_delay():
    with pytest.raises(HTTPException) as caught:
        _raise(genai_errors.ClientError(429, QUOTA_BODY))
    assert caught.value.status_code == 429
    assert caught.value.detail == "model quota exceeded"
    # 13.46s rounded down — a header value the client can actually parse.
    assert caught.value.headers == {"Retry-After": "13"}


def test_overloaded_model_becomes_503():
    with pytest.raises(HTTPException) as caught:
        _raise(genai_errors.ServerError(503, OVERLOAD_BODY))
    assert caught.value.status_code == 503
    assert caught.value.detail == "model unavailable"
    # No RetryInfo in the payload, so no invented number.
    assert caught.value.headers is None


def test_other_client_errors_stay_loud():
    """A bad request is our bug; dressing it as "try later" would hide it."""
    bad = genai_errors.ClientError(400, {"error": {"code": 400, "status": "INVALID"}})
    with pytest.raises(genai_errors.ClientError):
        _raise(bad)


def _chunk(text: str):
    return types.SimpleNamespace(text=text, usage_metadata=None)


def _fake_client(chunks, error):
    async def generate_content_stream(**kwargs):
        async def gen():
            for chunk in chunks:
                yield chunk
            if error is not None:
                raise error

        return gen()

    return types.SimpleNamespace(
        aio=types.SimpleNamespace(
            models=types.SimpleNamespace(
                generate_content_stream=generate_content_stream
            )
        )
    )


def test_stream_failing_after_the_first_chunk_is_mapped(monkeypatch):
    """Rejection can arrive while draining the stream, not when opening it."""
    error = genai_errors.ServerError(503, OVERLOAD_BODY)
    monkeypatch.setattr(llm, "_client", lambda: _fake_client([_chunk("부분 ")], error))

    async def drain():
        out = []
        async for piece in llm.generate_stream("sys", "user"):
            out.append(piece)
        return out

    with pytest.raises(HTTPException) as caught:
        asyncio.run(drain())
    assert caught.value.status_code == 503


def test_stream_without_failure_still_yields_text_then_generation(monkeypatch):
    monkeypatch.setattr(
        llm, "_client", lambda: _fake_client([_chunk("답"), _chunk("변")], None)
    )

    async def drain():
        return [piece async for piece in llm.generate_stream("sys", "user")]

    pieces = asyncio.run(drain())
    assert pieces[:-1] == ["답", "변"]
    assert isinstance(pieces[-1], llm.Generation)
    assert pieces[-1].text == "답변"
