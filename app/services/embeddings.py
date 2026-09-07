"""BGE-M3 embeddings via a Text Embeddings Inference (TEI)-style server.

Dense vectors come from the TEI-compatible ``/embed`` endpoint (M1 path). For
M2 hybrid retrieval we also need BGE-M3's lexical (sparse) weights, served by
``/embed_full`` which returns both in one call:

    POST /embed_full {"inputs": ["...", ...]}
    -> {"dense":  [[float, ...], ...],
        "sparse": [{"indices": [int, ...], "values": [float, ...]}, ...]}

Sparse weights are returned as index/value pairs over the BGE-M3 tokenizer
vocab and converted here to a pgvector ``SparseVector`` for storage/search.
"""

import httpx
from pgvector import SparseVector

from app.config import settings
from app.services.upstream import calling

# TEI truncates transparently, but we cap batch size to keep request bodies
# and GPU memory bounded on larger documents.
_BATCH_SIZE = 32
_TIMEOUT = httpx.Timeout(120.0)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Dense-embed a list of texts, preserving order (TEI ``/embed``)."""
    if not texts:
        return []

    url = settings.tei_url.rstrip("/") + "/embed"
    out: list[list[float]] = []
    with calling("embedding"):
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for start in range(0, len(texts), _BATCH_SIZE):
                batch = texts[start : start + _BATCH_SIZE]
                resp = await client.post(url, json={"inputs": batch})
                resp.raise_for_status()
                out.extend(resp.json())

    if len(out) != len(texts):
        raise RuntimeError(f"TEI returned {len(out)} embeddings for {len(texts)} inputs")
    return out


async def embed_query(text: str) -> list[float]:
    return (await embed_texts([text]))[0]


def _to_sparsevec(sparse: dict) -> SparseVector:
    indices = sparse.get("indices", [])
    values = sparse.get("values", [])
    mapping = {int(i): float(v) for i, v in zip(indices, values)}
    return SparseVector(mapping, settings.embed_sparse_dim)


async def embed_full(
    texts: list[str],
) -> tuple[list[list[float]], list[SparseVector]]:
    """Return (dense_vectors, sparse_vectors) for texts, order preserved.

    Uses ``/embed_full``. A server that does not answer raises
    ``UpstreamUnavailable`` so ingest can mark the document ``failed``.
    """
    if not texts:
        return [], []

    url = settings.tei_url.rstrip("/") + "/embed_full"
    dense: list[list[float]] = []
    sparse: list[SparseVector] = []
    with calling("embedding"):
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for start in range(0, len(texts), _BATCH_SIZE):
                batch = texts[start : start + _BATCH_SIZE]
                resp = await client.post(url, json={"inputs": batch})
                resp.raise_for_status()
                payload = resp.json()
                dense.extend(payload["dense"])
                sparse.extend(_to_sparsevec(s) for s in payload["sparse"])

    if len(dense) != len(texts) or len(sparse) != len(texts):
        raise RuntimeError(
            f"/embed_full returned {len(dense)} dense / {len(sparse)} sparse "
            f"for {len(texts)} inputs"
        )
    return dense, sparse


async def embed_query_full(text: str) -> tuple[list[float], SparseVector]:
    dense, sparse = await embed_full([text])
    return dense[0], sparse[0]
