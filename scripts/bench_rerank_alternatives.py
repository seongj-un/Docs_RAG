"""Measure three previously-untested levers on reranking cost (M6 후속).

bge-reranker-v2-m3 is the dominant cost of a query on real content: 9.4s for
20 candidates at this DB's real median chunk length (1,295자), measured on the
host MPS server (see app/config.py's CAND_K comment). Cutting input length
(RERANK_MAX_CHARS) is already measured and rejected — see
eval/rerank_truncation.py. This script measures the three options that
weren't: int8 quantization, a smaller model, and (via --mode baseline vs.
onnx-bench) what a separate CPU-only serving path would cost.

Every mode scores the exact same fixed payload — 20 real chunks from the
document already in this DB (documents.filename LIKE '01.%'), windowed so
their median length matches the 1,295-char figure above — so numbers compare
across options. See .superpowers/sdd/rerank-alternatives-report.md for the
full writeup; this is the tool that produced it.

    # 후보 페이로드만 확인 (락 불필요)
    .venv/bin/python -m scripts.bench_rerank_alternatives --mode payload

    # int8: PyTorch 동적 양자화, CPU 전용 (락 불필요 — MPS 커널이 없다)
    .venv/bin/python -m scripts.bench_rerank_alternatives --mode quant

    # int8: onnxruntime 경로 (CPU 전용, 락 불필요) — export 먼저
    .venv/bin/python -m scripts.bench_rerank_alternatives --mode onnx-export
    .venv/bin/python -m scripts.bench_rerank_alternatives --mode onnx-bench \\
        --onnx-path scripts/.rerank_onnx/reranker_int8.onnx

    # 더 작은 모델 — --device mps 는 GPU 락이 필요하다(아래 참고)
    .venv/bin/python -m scripts.bench_rerank_alternatives --mode smaller --device cpu
    .venv/bin/python -m scripts.bench_rerank_alternatives --mode smaller --device mps

    # 기존 호스트 서버(:8081, 이미 상주) 재사용 — 락 필요
    .venv/bin/python -m scripts.bench_rerank_alternatives --mode baseline

    # 라벨 있는 코퍼스로 랭킹 품질(R@1/R@3/MRR) — 후보=코퍼스 전체 clause,
    # 검색 단계를 건너뛰어 리랭커 자체의 품질만 분리해서 잰다
    .venv/bin/python -m scripts.bench_rerank_alternatives --mode quality --model base --device cpu
    .venv/bin/python -m scripts.bench_rerank_alternatives --mode quality-onnx \\
        --onnx-path scripts/.rerank_onnx/reranker_int8.onnx

MPS 락 프로토콜: 이 GPU는 이 세션 혼자 쓰는 게 아니다. --device mps 나
--provider CoreMLExecutionProvider 로 실제 추론을 돌리는 구간은 반드시
mps-lock 규약(v2 — 실제 추론 프로세스의 PID를 적고, 시간이 아니라
kill -0 로 죽음을 확인했을 때만 회수)을 지켜 락을 잡고, 끝나면 즉시
푼다. --device cpu 와 --provider CPUExecutionProvider 는 이 GPU를 전혀
쓰지 않으므로 락이 필요 없다.
"""

import argparse
import asyncio
import gc
import statistics
import time
from pathlib import Path

N = 20
TARGET_MEDIAN = 1295
# 저장소에 커밋하지 않는 산출물(각각 ~1.3MB / ~570MB) — .gitignore 대상 경로.
ONNX_DIR = Path(__file__).parent / ".rerank_onnx"
CORPORA_FOR_QUALITY = ("longchunk", "wide", "hard", "simple")


async def fetch_payload() -> tuple[str, list[str]]:
    """실제 문서(01. 로 시작)에서 20개 청크를 뽑는다.

    연속된 20청크 창 중 중앙값이 1,295자에 가장 가까운 것을 고르는
    결정론적 규칙이라, DB 상태가 그대로면 모든 모드가 같은 20개 텍스트를
    받는다 — "같은 입력으로 비교한다"는 이 측정의 전제.
    """
    from sqlalchemy import select

    from app.db import SessionLocal, engine
    from app.models import Chunk, Document

    async with SessionLocal() as s:
        doc = (
            await s.execute(select(Document).where(Document.filename.like("01.%")))
        ).scalars().first()
        chunks = (
            await s.execute(
                select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.chunk_index)
            )
        ).scalars().all()
    await engine.dispose()

    best = None
    for start in range(0, len(chunks) - N + 1):
        window = chunks[start:start + N]
        med = statistics.median(len(c.content) for c in window)
        score = abs(med - TARGET_MEDIAN)
        if best is None or score < best[0]:
            best = (score, start, window)
    _, _, window = best
    query = "인공지능기본법의 개선 방향과 EU AI법·미국·일본의 규제 방식은 어떻게 비교되는가?"
    return query, [c.content for c in window]


def _stats(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "p50": statistics.median(ordered),
        "mean": statistics.fmean(ordered),
        "min": ordered[0],
        "max": ordered[-1],
    }


def _report(name: str, samples: list[float]) -> dict:
    s = _stats(samples)
    print(f"  {name:34} p50 {s['p50']*1000:8.0f}ms  mean {s['mean']*1000:8.0f}ms"
          f"  min {s['min']*1000:8.0f}ms  max {s['max']*1000:8.0f}ms  (n={len(samples)})")
    return s


def time_compute_score(reranker, query, texts, runs) -> list[float]:
    pairs = [[query, t] for t in texts]
    reranker.compute_score(pairs[:2], normalize=True)  # warm up — lazy init을 재는 데 넣지 않는다
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        reranker.compute_score(pairs, normalize=True)
        samples.append(time.perf_counter() - t0)
    return samples


def mode_payload(args) -> None:
    """고정 페이로드만 확인한다 (DB 조회뿐 — GPU도 CPU도 무겁지 않다)."""
    query, texts = asyncio.run(fetch_payload())
    lens = sorted(len(t) for t in texts)
    print(f"query: {query}")
    print(f"n={len(texts)}  min/median/max = {lens[0]}/{statistics.median(lens):.0f}/{lens[-1]}"
          f"  total_chars={sum(lens)}")


def mode_quant(args) -> None:
    """PyTorch 네이티브 동적 int8 양자화. CPU 전용 — MPS는 quantized 커널이
    없어서 이 경로로는 GPU에 못 올린다(따로 확인함: RuntimeError:
    Didn't find engine for operation quantized::linear_prepack NoQEngine,
    MPS로 옮겨도 동일). 락이 필요 없다.
    """
    import torch
    from FlagEmbedding import FlagReranker

    if args.threads:
        torch.set_num_threads(args.threads)
    print(f"torch={torch.__version__}  threads={torch.get_num_threads()}")

    query, texts = asyncio.run(fetch_payload())
    print(f"payload: n={len(texts)}  chars={sorted(len(t) for t in texts)}")

    t0 = time.perf_counter()
    reranker = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=False, devices="cpu")
    print(f"[load] fp32 cpu  {time.perf_counter()-t0:.1f}s")

    print("\n[fp32 cpu]")
    fp32_samples = time_compute_score(reranker, query, texts, args.runs)
    _report("bge-reranker-v2-m3 fp32 cpu", fp32_samples)
    fp32_scores = reranker.compute_score([[query, t] for t in texts], normalize=True)

    # 기본 활성 엔진이 'none'이라 quantize_dynamic이 NoQEngine으로 죽는다 —
    # supported_engines에는 qnnpack(ARM)이 있는데 활성 엔진으로 잡혀있지
    # 않을 뿐이다. 직접 골라줘야 한다.
    print(f"quantized backends available: {torch.backends.quantized.supported_engines}")
    torch.backends.quantized.engine = "qnnpack"
    t0 = time.perf_counter()
    reranker.model = torch.ao.quantization.quantize_dynamic(
        reranker.model, {torch.nn.Linear}, dtype=torch.qint8
    )
    print(f"\n[quantize] dynamic int8 (Linear layers, qnnpack)  {time.perf_counter()-t0:.1f}s")

    print("\n[int8 cpu]")
    int8_samples = time_compute_score(reranker, query, texts, args.runs)
    _report("bge-reranker-v2-m3 int8 cpu", int8_samples)
    int8_scores = reranker.compute_score([[query, t] for t in texts], normalize=True)

    # 이 실제 문서 20개짜리 하나에 대해서만: int8이 top-3 순서를 흔드는가?
    # (라벨 있는 코퍼스 전체 품질 스윕은 --mode quality/quality-onnx 참고 —
    # qnnpack 경로는 fp32보다 느려서(위 숫자) 코퍼스 전체를 도는 비용을
    # 들일 가치가 없다고 판단, 이 한 예제 점검으로 대신했다.)
    order_fp32 = sorted(range(len(texts)), key=lambda i: -fp32_scores[i])
    order_int8 = sorted(range(len(texts)), key=lambda i: -int8_scores[i])
    print(f"\ntop-3 fp32: {order_fp32[:3]}   top-3 int8: {order_int8[:3]}")
    print(f"score diff (mean abs): "
          f"{statistics.fmean(abs(a-b) for a, b in zip(fp32_scores, int8_scores)):.4f}")
    print(f"same top-1={order_fp32[0]==order_int8[0]}  "
          f"same top-3 set={set(order_fp32[:3])==set(order_int8[:3])}")

    del reranker
    gc.collect()


def mode_smaller(args) -> None:
    """bge-reranker-base를 고정 페이로드로 측정한다.

    --device mps 는 이 기기의 유일한 GPU를 쓰므로 mps-lock을 잡고
    실행할 것 — 이 스크립트는 락을 스스로 잡지 않는다(셸에서 잡는 게
    이 저장소의 규약이다, mps-lock.md 참고).
    """
    import torch
    from FlagEmbedding import FlagReranker

    if args.threads:
        torch.set_num_threads(args.threads)
    query, texts = asyncio.run(fetch_payload())
    print(f"payload: n={len(texts)}  chars={sorted(len(t) for t in texts)}")

    t0 = time.perf_counter()
    reranker = FlagReranker("BAAI/bge-reranker-base", use_fp16=False, devices=args.device)
    print(f"[load] bge-reranker-base {args.device}  {time.perf_counter()-t0:.1f}s")

    samples = time_compute_score(reranker, query, texts, args.runs)
    _report(f"bge-reranker-base {args.device}", samples)

    del reranker
    gc.collect()
    if args.device == "mps":
        torch.mps.empty_cache()


def mode_baseline(args) -> None:
    """호스트에 이미 떠 있는 서버(:8081, bge-reranker-v2-m3 fp32/MPS)를 같은
    고정 페이로드로 다시 때린다 — 모델을 또 로드하지 않고 지금 배포된
    그대로의 숫자를 같은 입력에 대해 확인한다. 실제 GPU 추론이므로
    mps-lock 필요. (이 세션에서는 :8081이 다른 쪽 작업으로 바빠 120초
    타임아웃을 두 번 다 넘겼다 — app/config.py에 이미 기록된 9.4초를
    기준선으로 쓰고, 이 모드는 그 재현 시도가 왜 실패했는지의 증거로
    보고서에 남겼다.)
    """
    import httpx

    query, texts = asyncio.run(fetch_payload())
    print(f"payload: n={len(texts)}  chars={sorted(len(t) for t in texts)}")

    url = args.url.rstrip("/") + "/rerank"
    samples = []
    with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
        client.post(url, json={"query": query, "texts": texts[:2]})  # warm up
        for _ in range(args.runs):
            t0 = time.perf_counter()
            resp = client.post(url, json={"query": query, "texts": texts})
            resp.raise_for_status()
            samples.append(time.perf_counter() - t0)
    _report("bge-reranker-v2-m3 fp32 mps (server, same payload)", samples)


def mode_quality(args) -> None:
    """라벨 있는 코퍼스 하나의 모든 clause를 후보로 랭킹해 R@1/R@3/MRR을 잰다.

    검색(retrieval) 단계를 아예 건너뛴다 — 후보 집합이 코퍼스의 clause
    전부라, 숫자를 바꿀 수 있는 건 리랭커 자체뿐이다. eval/candk_sweep.py·
    eval/rerank_truncation.py와 같은 지표(페이지 단위 R@k/MRR)를 쓰지만,
    모델 자체를 바꿔치기하므로(rerank.py는 RERANK_URL 하나로 고정된 서버만
    가리킨다) 그 스크립트들처럼 실제 앱 경로(HTTP)를 타지 않고 인프로세스로
    직접 점수를 매긴다.
    """
    import torch
    from FlagEmbedding import FlagReranker

    from eval import metrics
    from eval.corpora import load

    if args.threads:
        torch.set_num_threads(args.threads)

    model_id = {
        "v2m3": "BAAI/bge-reranker-v2-m3",
        "base": "BAAI/bge-reranker-base",
    }[args.model]
    reranker = FlagReranker(model_id, use_fp16=False, devices=args.device)
    if args.quantize:
        torch.backends.quantized.engine = "qnnpack"
        reranker.model = torch.ao.quantization.quantize_dynamic(
            reranker.model, {torch.nn.Linear}, dtype=torch.qint8
        )

    label = f"{args.model}{'+int8' if args.quantize else ''}/{args.device}"
    corpora = args.corpora.split(",") if args.corpora else list(CORPORA_FOR_QUALITY)
    for name in corpora:
        corpus = load(name)
        queries = corpus.QUERIES[:args.limit] if args.limit else corpus.QUERIES
        rows = []
        t0 = time.perf_counter()
        for qi, (question, gold, *_rest) in enumerate(queries):
            qt0 = time.perf_counter()
            pairs = [[question, clause] for clause in corpus.CLAUSES]
            scores = reranker.compute_score(pairs, normalize=True)
            if not isinstance(scores, list):
                scores = [scores]
            ranked = sorted(range(len(corpus.CLAUSES)), key=lambda i: -scores[i])
            ranked_pages = [i + 1 for i in ranked]  # page = index+1 (eval/corpora 관례)
            row = {"q": question, "gold": gold}
            row.update(metrics.score_all(ranked_pages, gold, 5))
            rows.append(row)
            print(f"    [{name} {qi+1}/{len(queries)}] {time.perf_counter()-qt0:.1f}s "
                  f"gold=p{gold} top1={ranked_pages[0]}", flush=True)
        elapsed = time.perf_counter() - t0
        agg = {k: statistics.fmean(r[k] for r in rows) for k in ("R@1", "R@3", "MRR")}
        med_len = statistics.median(len(c) for c in corpus.CLAUSES)
        print(f"[{label:16}] {name:10} clauses={len(corpus.CLAUSES):4} "
              f"median_len={med_len:5.0f}  R@1={agg['R@1']:.3f}  R@3={agg['R@3']:.3f}  "
              f"MRR={agg['MRR']:.3f}  ({elapsed:.1f}s for {len(queries)}q)")

    del reranker
    gc.collect()
    if args.device == "mps":
        torch.mps.empty_cache()


def _tokenize_pairs(tok, query, texts):
    return tok(
        [query] * len(texts), texts,
        padding=True, truncation="only_second", max_length=512,
        return_tensors="np",
    )


def mode_quality_onnx(args) -> None:
    """mode_quality와 같은 지표를 onnxruntime 세션으로 잰다.

    qnnpack 동적양자화는 이 기기에서 fp32보다 느렸다(mode_quant 결과) —
    그 경로로 코퍼스 전체를 도는 건 시간 대비 얻는 게 없어 건너뛰었다.
    반면 onnxruntime의 int8 커널은 같은 페이로드에서 fp32보다 6배 이상
    빨랐다(mode_onnx_bench 결과) — "int8이 품질을 얼마나 깎는가"는 실제로
    추천할 만한 이 경로로 재는 게 맞다. qnnpack에서 본 것과 품질이 같을
    거라고 넘겨짚지 않기 위해 별도 함수로 분리했다.
    """
    import numpy as np
    import onnxruntime as ort
    from transformers import AutoTokenizer

    from eval import metrics
    from eval.corpora import load

    tok = AutoTokenizer.from_pretrained("BAAI/bge-reranker-v2-m3")
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    sess = ort.InferenceSession(args.onnx_path, sess_options=so, providers=[args.provider])
    input_names = [i.name for i in sess.get_inputs()]
    label = f"onnx-{Path(args.onnx_path).stem}"

    corpora = args.corpora.split(",") if args.corpora else list(CORPORA_FOR_QUALITY)
    for name in corpora:
        corpus = load(name)
        queries = corpus.QUERIES[:args.limit] if args.limit else corpus.QUERIES
        rows = []
        t0 = time.perf_counter()
        for qi, (question, gold, *_rest) in enumerate(queries):
            qt0 = time.perf_counter()
            enc = _tokenize_pairs(tok, question, corpus.CLAUSES)
            feed = {n: enc[n].astype(np.int64) for n in input_names}
            logits = sess.run(None, feed)[0].squeeze(-1)
            scores = 1 / (1 + np.exp(-logits))
            ranked = sorted(range(len(corpus.CLAUSES)), key=lambda i: -scores[i])
            ranked_pages = [i + 1 for i in ranked]
            row = {"q": question, "gold": gold}
            row.update(metrics.score_all(ranked_pages, gold, 5))
            rows.append(row)
            print(f"    [{name} {qi+1}/{len(queries)}] {time.perf_counter()-qt0:.1f}s "
                  f"gold=p{gold} top1={ranked_pages[0]}", flush=True)
        elapsed = time.perf_counter() - t0
        agg = {k: statistics.fmean(r[k] for r in rows) for k in ("R@1", "R@3", "MRR")}
        med_len = statistics.median(len(c) for c in corpus.CLAUSES)
        print(f"[{label:20}] {name:10} clauses={len(corpus.CLAUSES):4} "
              f"median_len={med_len:5.0f}  R@1={agg['R@1']:.3f}  R@3={agg['R@3']:.3f}  "
              f"MRR={agg['MRR']:.3f}  ({elapsed:.1f}s for {len(queries)}q)")


def mode_onnx_export(args) -> None:
    """ONNX로 내보내고 onnxruntime의 동적 int8 양자화를 적용한다.

    CPU 전용, 락 불필요 — export도 정합성 확인도 GPU를 쓰지 않는다.
    torch의 quantized 엔진(qnnpack)과는 완전히 다른 커널 구현이라, 되든
    안 되든 int8에 대해 독립적인 두 번째 근거가 된다. export 직후 같은
    입력에 대해 onnx-fp32 vs torch-fp32 로짓을 대조해 변환 자체가 깨지지
    않았는지 확인한다 — 여기서 어긋나면 이후 속도·품질 숫자가 전부 무의미.
    """
    import numpy as np
    import onnxruntime as ort
    import torch
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    ONNX_DIR.mkdir(exist_ok=True)
    model_id = "BAAI/bge-reranker-v2-m3"
    tok = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForSequenceClassification.from_pretrained(model_id)
    model.eval()

    query, texts = asyncio.run(fetch_payload())
    enc = _tokenize_pairs(tok, query, texts[:2])
    input_names = ["input_ids", "attention_mask"]
    dynamic_axes = {
        "input_ids": {0: "batch", 1: "seq"},
        "attention_mask": {0: "batch", 1: "seq"},
        "logits": {0: "batch"},
    }
    inputs = [torch.tensor(enc["input_ids"]), torch.tensor(enc["attention_mask"])]
    if "token_type_ids" in enc:
        input_names.append("token_type_ids")
        dynamic_axes["token_type_ids"] = {0: "batch", 1: "seq"}
        inputs.append(torch.tensor(enc["token_type_ids"]))

    fp32_path = ONNX_DIR / "reranker_fp32.onnx"
    t0 = time.perf_counter()
    torch.onnx.export(
        model, tuple(inputs), str(fp32_path),
        input_names=input_names, output_names=["logits"],
        dynamic_axes=dynamic_axes, opset_version=17,
        dynamo=False,  # torch 2.14 기본(dynamo=True)은 onnxscript가 없으면 죽는다
    )
    print(f"[export] fp32 onnx -> {fp32_path}  "
          f"({fp32_path.stat().st_size/1e6:.0f}MB, {time.perf_counter()-t0:.1f}s)")

    int8_path = ONNX_DIR / "reranker_int8.onnx"
    t0 = time.perf_counter()
    quantize_dynamic(str(fp32_path), str(int8_path), weight_type=QuantType.QInt8)
    print(f"[export] int8 onnx  -> {int8_path}  "
          f"({int8_path.stat().st_size/1e6:.0f}MB, {time.perf_counter()-t0:.1f}s)")

    sess = ort.InferenceSession(str(fp32_path), providers=["CPUExecutionProvider"])
    feed = {name: enc[name].astype(np.int64) for name in input_names}
    onnx_logits = sess.run(None, feed)[0]
    with torch.no_grad():
        torch_logits = model(**{n: torch.tensor(enc[n]) for n in input_names}).logits.numpy()
    diff = float(abs(onnx_logits - torch_logits).max())
    print(f"[verify] max abs logit diff (onnx-fp32 vs torch-fp32, n=2 pairs): {diff:.6f}")

    del model
    gc.collect()


def mode_onnx_bench(args) -> None:
    """onnx 모델 하나를 지정한 provider로 로드해 고정 페이로드(20개)를 잰다.

    --provider CoreMLExecutionProvider 는 이 맥의 GPU/ANE로 갈 수 있으므로
    mps-lock을 잡고 실행할 것 — CPUExecutionProvider는 이 GPU를 쓰지
    않으므로 필요 없다. (실측: CoreML EP는 세션 생성에는 성공하지만
    — 그래프 1126개 노드 중 541개만 CoreML이 맡고 나머지는 CPU로 쪼개진다
    — 이 페이로드로 실제 추론을 돌리면 "Unable to compute the prediction"
    런타임 오류로 죽는다. export를 고정 shape로 다시 하는 등 추가 작업
    없이는 이 경로를 못 쓴다는 뜻 — 시도했고, 안 됐고, 왜인지는 기록해
    둔다.)
    """
    import numpy as np
    import onnxruntime as ort
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained("BAAI/bge-reranker-v2-m3")
    query, texts = asyncio.run(fetch_payload())

    providers = args.provider.split(",")
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    t0 = time.perf_counter()
    sess = ort.InferenceSession(args.onnx_path, sess_options=so, providers=providers)
    print(f"[load] onnx session  {time.perf_counter()-t0:.1f}s")
    print(f"requested providers: {providers}")
    print(f"session actual providers: {sess.get_providers()}")
    input_names = [i.name for i in sess.get_inputs()]

    def run_once():
        enc = _tokenize_pairs(tok, query, texts)
        feed = {name: enc[name].astype(np.int64) for name in input_names}
        return sess.run(None, feed)[0]

    out = run_once()  # warm up
    samples = []
    for _ in range(args.runs):
        t0 = time.perf_counter()
        out = run_once()
        samples.append(time.perf_counter() - t0)
    label = f"onnx {Path(args.onnx_path).stem} [{','.join(sess.get_providers())}]"
    _report(label, samples)
    scores = 1 / (1 + np.exp(-out.squeeze(-1)))
    order = sorted(range(len(texts)), key=lambda i: -scores[i])
    print(f"top-3 idx: {order[:3]}  scores(head): {[round(float(s), 4) for s in scores[:5]]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True,
                         choices=("payload", "quant", "smaller", "baseline", "quality",
                                  "quality-onnx", "onnx-export", "onnx-bench"))
    parser.add_argument("--runs", type=int, default=3,
                         help="bench_cpu_serving.py 기본(5)보다 적다 — 실제 청크 길이(최대"
                              " 1,295자대)에서는 한 런이 훨씬 비싸다")
    parser.add_argument("--threads", type=int, default=4,
                         help="docker-compose.yml의 CPU 컨테이너와 같은 기본값(OMP_NUM_THREADS)")
    parser.add_argument("--device", default="cpu", choices=("cpu", "mps"))
    parser.add_argument("--url", default="http://127.0.0.1:8081")
    parser.add_argument("--model", default="v2m3", choices=("v2m3", "base"))
    parser.add_argument("--quantize", action="store_true",
                         help="quality 모드에서 qnnpack 동적 int8을 적용한다")
    parser.add_argument("--corpora", default="",
                         help="쉼표로 구분(예: longchunk,wide) — 비우면 4개 전부")
    parser.add_argument("--limit", type=int, default=0,
                         help="quality* 모드: 질의 수 제한(스모크 테스트용)")
    parser.add_argument("--onnx-path", default=str(ONNX_DIR / "reranker_fp32.onnx"))
    parser.add_argument("--provider", default="CPUExecutionProvider")
    args = parser.parse_args()

    {
        "payload": mode_payload,
        "quant": mode_quant,
        "smaller": mode_smaller,
        "baseline": mode_baseline,
        "quality": mode_quality,
        "quality-onnx": mode_quality_onnx,
        "onnx-export": mode_onnx_export,
        "onnx-bench": mode_onnx_bench,
    }[args.mode](args)


if __name__ == "__main__":
    main()
