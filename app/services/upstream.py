"""Failures of the model servers this service calls over HTTP.

The embedding and rerank servers are called from two places with different
needs: the query path, where the caller is waiting on an HTTP response, and
background indexing, where nobody is. So these clients raise a domain error
rather than an ``HTTPException`` — a status code stored in ``documents.error``
would be noise, since no response ever carries it. The query path translates
at its own boundary (``pipeline``); indexing records the failure and marks the
document ``failed``, which is what the UI already knows how to explain.
"""

from contextlib import contextmanager

import httpx


class UpstreamUnavailable(RuntimeError):
    """A model server we depend on did not answer usefully."""

    def __init__(self, service: str, cause: Exception):
        self.service = service
        self.cause = cause
        super().__init__(f"{service} server unavailable ({type(cause).__name__})")


@contextmanager
def calling(service: str):
    """Turn "the server did not answer" into ``UpstreamUnavailable``.

    Covers both ways that happens: no response at all (not running, refused,
    timed out) and a response that says the server itself is in trouble (5xx,
    or 429 when it is shedding load).

    Other 4xx statuses pass through untouched. A rejected request is this
    service sending something wrong, and dressing that up as "come back later"
    would hide the bug and invite an infinite retry.
    """
    try:
        yield
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status < 500 and status != httpx.codes.TOO_MANY_REQUESTS:
            raise
        raise UpstreamUnavailable(service, exc) from exc
    except httpx.RequestError as exc:
        raise UpstreamUnavailable(service, exc) from exc
