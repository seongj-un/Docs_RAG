"""RERANK_MAX_CHARS shortens what is scored, not what is returned.

The measured effect of this setting is in eval/rerank_truncation.py; these
tests only pin the mechanics, including the property the sweep depends on —
that indices still address the caller's full-length candidates.
"""

import asyncio

import httpx
import pytest

from app.config import settings
from app.services import rerank


def _capturing_client(sent: list[dict]):
    """An httpx.AsyncClient stand-in that records the request body."""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            sent.append(json)
            scores = [
                {"index": i, "score": 1.0 / (i + 1)} for i in range(len(json["texts"]))
            ]
            return httpx.Response(
                200, json=scores, request=httpx.Request("POST", url)
            )

    return FakeClient


@pytest.fixture
def sent(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(httpx, "AsyncClient", _capturing_client(captured))
    original = settings.rerank_max_chars
    yield captured
    settings.rerank_max_chars = original


def test_zero_means_no_truncation(sent):
    settings.rerank_max_chars = 0
    texts = ["가" * 900, "나" * 50]
    asyncio.run(rerank.rerank("질문", texts))
    assert sent[0]["texts"] == texts


def test_limit_truncates_each_candidate(sent):
    settings.rerank_max_chars = 128
    long, short = "가" * 900, "나" * 50
    asyncio.run(rerank.rerank("질문", [long, short]))
    assert [len(t) for t in sent[0]["texts"]] == [128, 50]


def test_indices_still_address_the_untruncated_candidates(sent):
    """The sweep maps scores back to full chunks; truncation must not shift that."""
    settings.rerank_max_chars = 10
    texts = ["가" * 900, "나" * 900, "다" * 900]
    ranked = asyncio.run(rerank.rerank("질문", texts))
    assert sorted(i for i, _ in ranked) == [0, 1, 2]
    assert ranked[0][0] == 0  # highest score, by the fake's ordering


def test_caller_list_is_not_mutated(sent):
    settings.rerank_max_chars = 16
    texts = ["가" * 900]
    asyncio.run(rerank.rerank("질문", texts))
    assert len(texts[0]) == 900


def test_empty_input_never_calls_the_server(sent):
    settings.rerank_max_chars = 128
    assert asyncio.run(rerank.rerank("질문", [])) == []
    assert sent == []
