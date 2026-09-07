"""Measure BGE-M3 and bge-reranker-v2-m3 on local hardware (M6 decision 2).

The M6 spec frames CPU serving as a cost option. On this machine it is more
than that: Docker on Apple Silicon has no GPU passthrough (`runc` only, no
NVIDIA runtime, and Metal is not visible inside the Linux VM), so
``docker compose up`` — an M6 completion criterion — can only serve these
models on CPU. This measures whether that is fast enough to live with.

Measured against what the pipeline actually does, not synthetic inputs:
- query embedding: one short question, on the critical path of every request
- batch embedding: CHUNK-sized texts, what indexing a document costs
- reranking: query x CAND_K candidates, the heaviest per-query step

Device is selectable because the answer differs by deployment target:
``cpu`` is what a container (and a VPS) gets; ``mps`` is what a native
process on this Mac can use, and is reported only as a reference point.

    python -m scripts.bench_cpu_serving --device cpu
"""

import argparse
import gc
import statistics
import time

# Realistic inputs: a user question, and chunk-length passages matching the
# ~700-token chunks the indexer produces.
QUESTION = "임대차 보증금은 얼마이고 언제까지 반환해야 하나요?"
CHUNK = (
    "제7조 (위약금) 계약 기간 중 중도 해지 시 위약금은 일백이십오만원(1,250,000원)"
    "으로 한다. 임차인이 계약 기간 만료 전에 해지를 통보하는 경우 3개월 전까지 "
    "서면으로 통보하여야 하며, 통보가 지연된 경우 그 기간만큼 차임을 추가로 "
    "부담한다. 임대인이 계약을 해지하는 경우에도 동일한 기준을 적용한다. "
) * 3


def _stats(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "p50": statistics.median(ordered),
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "mean": statistics.fmean(ordered),
        "min": ordered[0],
        "max": ordered[-1],
    }


def _report(name: str, samples: list[float], note: str = "") -> dict:
    s = _stats(samples)
    print(
        f"  {name:34} p50 {s['p50']*1000:8.0f}ms   p95 {s['p95']*1000:8.0f}ms"
        f"   (n={len(samples)}) {note}"
    )
    return s


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="cpu", choices=("cpu", "mps"))
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--cand-k", type=int, default=50,
                        help="candidates per query (matches CAND_K)")
    parser.add_argument("--batch", type=int, default=32,
                        help="indexing batch size (matches embeddings._BATCH_SIZE)")
    parser.add_argument("--threads", type=int, default=0,
                        help="torch CPU threads (0 = library default)")
    parser.add_argument("--skip-index", action="store_true",
                        help="skip the indexing benchmark")
    args = parser.parse_args()

    import torch
    from FlagEmbedding import BGEM3FlagModel, FlagReranker

    if args.threads:
        torch.set_num_threads(args.threads)
    print(f"device={args.device}  torch={torch.__version__}  "
          f"threads={torch.get_num_threads()}")
    if args.device == "mps" and not torch.backends.mps.is_available():
        raise SystemExit("MPS not available on this machine")

    results: dict[str, dict] = {}

    # --- load cost: matters for scale-to-zero cold starts ---
    t0 = time.perf_counter()
    embedder = BGEM3FlagModel(
        "BAAI/bge-m3", use_fp16=False, devices=args.device
    )
    embed_load = time.perf_counter() - t0
    print(f"\n[load] BGE-M3            {embed_load:6.1f}s")

    t0 = time.perf_counter()
    reranker = FlagReranker(
        "BAAI/bge-reranker-v2-m3", use_fp16=False, devices=args.device
    )
    rerank_load = time.perf_counter() - t0
    print(f"[load] bge-reranker-v2-m3 {rerank_load:6.1f}s")
    print(f"[load] 합계               {embed_load + rerank_load:6.1f}s"
          "   ← scale-to-zero 콜드스타트 하한\n")

    # Warm up: the first call pays lazy init that would skew p50.
    embedder.encode([QUESTION], return_dense=True, return_sparse=True)
    reranker.compute_score([[QUESTION, CHUNK]], normalize=True)

    print("[측정] 질의 경로 (요청마다 발생)")
    samples = []
    for _ in range(args.runs):
        t0 = time.perf_counter()
        embedder.encode([QUESTION], return_dense=True, return_sparse=True)
        samples.append(time.perf_counter() - t0)
    results["query_embed"] = _report("질문 임베딩 (dense+sparse) x1", samples)

    for n in (10, args.cand_k):
        pairs = [[QUESTION, CHUNK] for _ in range(n)]
        samples = []
        for _ in range(args.runs):
            t0 = time.perf_counter()
            reranker.compute_score(pairs, normalize=True)
            samples.append(time.perf_counter() - t0)
        results[f"rerank_{n}"] = _report(f"리랭킹 {n}쌍", samples)

    if args.skip_index:
        print()
        q = results["query_embed"]["p50"]
        r = results[f"rerank_{args.cand_k}"]["p50"]
        print(f"[합산] 임베딩 {q*1000:.0f}ms + 리랭킹({args.cand_k}쌍) {r*1000:.0f}ms"
              f" = {(q+r)*1000:.0f}ms")
        return

    print("\n[측정] 인덱싱 경로 (업로드 시 1회)")
    batch = [CHUNK] * args.batch
    samples = []
    for _ in range(max(2, args.runs // 2)):
        t0 = time.perf_counter()
        embedder.encode(batch, return_dense=True, return_sparse=True)
        samples.append(time.perf_counter() - t0)
    s = _report(f"청크 {args.batch}건 임베딩", samples)
    per_chunk = s["p50"] / args.batch
    print(f"    → 청크당 {per_chunk*1000:.0f}ms, "
          f"40페이지 문서(≈40청크) 약 {per_chunk*40:.1f}초")

    print("\n[합산] 질의 1건의 모델 시간 (생성 제외)")
    q = results["query_embed"]["p50"]
    r = results[f"rerank_{args.cand_k}"]["p50"]
    print(f"    임베딩 {q*1000:.0f}ms + 리랭킹({args.cand_k}쌍) {r*1000:.0f}ms"
          f" = {(q+r)*1000:.0f}ms")
    print(f"    CAND_K를 20으로 줄이면 리랭킹 약 "
          f"{results[f'rerank_{args.cand_k}']['p50']*20/args.cand_k*1000:.0f}ms 예상")

    del embedder, reranker
    gc.collect()


if __name__ == "__main__":
    main()
