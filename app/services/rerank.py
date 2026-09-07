"""Cross-encoder reranking via a TEI-style ``/rerank`` endpoint.

Model: bge-reranker-v2-m3 (same family/infra as the BGE-M3 embedder). Given a
query and candidate texts, it scores each (query, text) pair jointly — more
accurate than the bi-encoder cosine used for first-stage retrieval — and we
keep only the top ``RERANK_TOP``.

Wire format mirrors TEI:
    POST /rerank {"query": "...", "texts": ["...", ...]}
    -> [{"index": i, "score": s}, ...]
"""

import httpx

from app.config import settings
from app.services.upstream import calling

_TIMEOUT = httpx.Timeout(120.0)


async def rerank(query: str, texts: list[str]) -> list[tuple[int, float]]:
    """Return ``(original_index, score)`` pairs sorted by score, best first.

    ``original_index`` refers to the position in the input ``texts`` list, so
    callers can map scores back to their candidate objects.

    ``RERANK_MAX_CHARS`` shortens what is scored, not what is returned: the
    indices still address the caller's full-length candidates. Truncation
    lives here rather than at the call site so evaluation measures the same
    input production sends.
    """
    if not texts:
        return []

    limit = settings.rerank_max_chars
    if limit:
        texts = [text[:limit] for text in texts]

    url = settings.rerank_url.rstrip("/") + "/rerank"
    with calling("rerank"):
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json={"query": query, "texts": texts})
            resp.raise_for_status()
            data = resp.json()

    ranked = [(int(d["index"]), float(d["score"])) for d in data]
    ranked.sort(key=lambda t: t[1], reverse=True)
    return ranked
