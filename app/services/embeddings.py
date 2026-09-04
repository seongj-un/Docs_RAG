"""BGE-M3 embeddings via a Text Embeddings Inference (TEI) server.

M1 uses dense vectors only. BGE-M3 also produces sparse vectors, which M2 will
consume for the hybrid (BM25 + vector) retriever.
"""

import httpx

from app.config import settings

# TEI truncates transparently, but we cap batch size to keep request bodies
# and GPU memory bounded on larger documents.
_BATCH_SIZE = 32
_TIMEOUT = httpx.Timeout(60.0)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts, preserving order.

    Calls the TEI ``/embed`` endpoint in batches. Raises on a non-2xx response
    so the caller (ingest) can mark the document ``failed``.
    """
    if not texts:
        return []

    url = settings.tei_url.rstrip("/") + "/embed"
    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for start in range(0, len(texts), _BATCH_SIZE):
            batch = texts[start : start + _BATCH_SIZE]
            resp = await client.post(url, json={"inputs": batch})
            resp.raise_for_status()
            vectors = resp.json()
            out.extend(vectors)

    if len(out) != len(texts):
        raise RuntimeError(
            f"TEI returned {len(out)} embeddings for {len(texts)} inputs"
        )
    return out


async def embed_query(text: str) -> list[float]:
    vectors = await embed_texts([text])
    return vectors[0]
