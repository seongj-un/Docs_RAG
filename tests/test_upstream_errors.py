"""A model server that is down must not look like this service crashing.

The embedding and rerank clients are shared by the query path (which owes the
caller a response) and background indexing (which owes nobody one), so the
domain error and its translation are tested separately.
"""

import asyncio
import types

import httpx
import pytest
from fastapi import HTTPException

from app.services import embeddings, pipeline, rerank
from app.services.upstream import UpstreamUnavailable, calling


def _raise(exc: Exception, service: str = "embedding") -> None:
    with calling(service):
        raise exc


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://127.0.0.1:8081/embed")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(code, request=request)
    )


def test_server_not_running_is_unavailable():
    with pytest.raises(UpstreamUnavailable) as caught:
        _raise(httpx.ConnectError("connection refused"))
    assert caught.value.service == "embedding"


def test_timeout_is_unavailable():
    with pytest.raises(UpstreamUnavailable):
        _raise(httpx.ReadTimeout("too slow"))


@pytest.mark.parametrize("code", [500, 502, 503, 429])
def test_server_side_statuses_are_unavailable(code):
    with pytest.raises(UpstreamUnavailable):
        _raise(_status_error(code))


@pytest.mark.parametrize("code", [400, 404, 422])
def test_client_side_statuses_stay_loud(code):
    """A rejected request is our bug; "come back later" would hide it."""
    with pytest.raises(httpx.HTTPStatusError):
        _raise(_status_error(code))


def test_unrelated_errors_pass_through():
    with pytest.raises(ValueError):
        _raise(ValueError("not a transport problem"))


def test_pipeline_translates_to_503():
    with pytest.raises(HTTPException) as caught:
        with pipeline._reachable():
            raise UpstreamUnavailable("rerank", httpx.ConnectError("refused"))
    assert caught.value.status_code == 503
    assert caught.value.detail == "search unavailable"
    # Nothing here knows when the server returns, so no invented Retry-After.
    assert caught.value.headers is None


def _client_raising(exc: Exception):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            raise exc

    return FakeClient


def test_embed_texts_reports_a_down_server(monkeypatch):
    monkeypatch.setattr(
        httpx, "AsyncClient", _client_raising(httpx.ConnectError("refused"))
    )
    with pytest.raises(UpstreamUnavailable) as caught:
        asyncio.run(embeddings.embed_texts(["질문"]))
    assert caught.value.service == "embedding"


def test_rerank_reports_a_down_server(monkeypatch):
    monkeypatch.setattr(
        httpx, "AsyncClient", _client_raising(httpx.ConnectError("refused"))
    )
    with pytest.raises(UpstreamUnavailable) as caught:
        asyncio.run(rerank.rerank("질문", ["후보"]))
    assert caught.value.service == "rerank"


def test_indexing_records_the_failure_as_text_the_ui_can_branch_on():
    """web/lib/failure.ts splits on "no extractable text"; everything else is
    reported as a server problem. The message must not start claiming the PDF
    is at fault when the embedder is simply down."""
    message = f"{UpstreamUnavailable('embedding', httpx.ConnectError('x'))}"
    assert "no extractable text" not in message
    assert "embedding" in message
