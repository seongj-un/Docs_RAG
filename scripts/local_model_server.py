"""Serve BGE-M3 and bge-reranker-v2-m3 locally on this machine's GPU.

Replaces the Colab notebook + cloudflared tunnel the project has been using:
that URL expires with every session, and it was re-issued seven times during
development. On Apple Silicon the models run on Metal (MPS), which measured
2.4x faster than CPU for reranking and 2.5x for embedding — fast enough that
the whole stack can run locally with no external dependency.

Implements exactly the wire contract ``app/services/embeddings.py`` and
``rerank.py`` already speak, so nothing in the application changes — only the
``TEI_URL`` / ``RERANK_URL`` values in ``.env``:

    POST /embed       {"inputs": [str]}            -> [[float]]
    POST /embed_full  {"inputs": [str]}            -> {"dense": [[float]],
                                                       "sparse": [{indices,values}]}
    POST /rerank      {"query": str, "texts": [str]} -> [{"index", "score"}]

Models load at startup and stay resident (~4.5s), so no request pays for it.

    pip install -r requirements-bench.txt
    python -m scripts.local_model_server            # http://127.0.0.1:8081
"""

import argparse
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("local_model_server")

EMBED_MODEL = "BAAI/bge-m3"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

_state: dict = {}


def pick_device(requested: str) -> str:
    """Resolve 'auto' to the fastest device this machine actually has."""
    import torch

    if requested != "auto":
        return requested
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _as_list(inputs) -> list[str]:
    return [inputs] if isinstance(inputs, str) else list(inputs)


def _sparse_payload(lexical_weights) -> list[dict]:
    """BGE-M3 lexical weights -> the index/value pairs the client expects."""
    out = []
    for weights in lexical_weights:
        indices, values = [], []
        for token_id, weight in weights.items():
            value = float(weight)
            if value <= 0:
                continue
            indices.append(int(token_id))
            values.append(value)
        out.append({"indices": indices, "values": values})
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    from FlagEmbedding import BGEM3FlagModel, FlagReranker

    device = _state["device"]
    # fp16 on MPS is not consistently faster and can change scores; the
    # benchmark numbers this server is sized against were taken at fp32.
    logger.info("loading %s on %s ...", EMBED_MODEL, device)
    _state["embedder"] = BGEM3FlagModel(EMBED_MODEL, use_fp16=False, devices=device)
    logger.info("loading %s on %s ...", RERANK_MODEL, device)
    _state["reranker"] = FlagReranker(RERANK_MODEL, use_fp16=False, devices=device)

    # Warm up so the first real request does not pay lazy initialisation.
    _state["embedder"].encode(["워밍업"], return_dense=True, return_sparse=True)
    _state["reranker"].compute_score([["워밍업", "워밍업 문장"]], normalize=True)
    logger.info("ready on %s", device)
    yield
    _state.clear()


app = FastAPI(title="Local model server (BGE-M3 + reranker)", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "device": _state.get("device")}


# The model calls block; off the event loop they go, or /health stops
# answering for the 7s a full rerank takes — and callers probe /health to
# decide whether this server is up at all.
@app.post("/embed")
async def embed(req: Request):
    texts = _as_list((await req.json())["inputs"])
    out = await run_in_threadpool(
        _state["embedder"].encode,
        texts, return_dense=True, return_sparse=False, return_colbert_vecs=False,
    )
    return [vec.tolist() for vec in out["dense_vecs"]]


@app.post("/embed_full")
async def embed_full(req: Request):
    texts = _as_list((await req.json())["inputs"])
    out = await run_in_threadpool(
        _state["embedder"].encode,
        texts, return_dense=True, return_sparse=True, return_colbert_vecs=False,
    )
    return {
        "dense": [vec.tolist() for vec in out["dense_vecs"]],
        "sparse": _sparse_payload(out["lexical_weights"]),
    }


@app.post("/rerank")
async def rerank(req: Request):
    body = await req.json()
    query, texts = body["query"], body["texts"]
    if not texts:
        return []
    scores = await run_in_threadpool(
        _state["reranker"].compute_score,
        [[query, text] for text in texts], normalize=True,
    )
    if not isinstance(scores, list):
        scores = [scores]
    return [{"index": i, "score": float(s)} for i, s in enumerate(scores)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--device", default="auto",
                        choices=("auto", "mps", "cuda", "cpu"))
    args = parser.parse_args()

    _state["device"] = pick_device(args.device)
    print(f"device: {_state['device']}  ->  http://{args.host}:{args.port}")
    print("TEI_URL / RERANK_URL 을 위 주소로 설정하세요.")
    # info 로 둔다. warning 이면 액세스 로그가 사라져서, 요청이 도착이나
    # 했는지조차 알 수 없다 — 실제로 그 상태에서 장애를 진단하지 못한 적이 있다.
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
