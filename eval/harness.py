"""M7 L1 evaluation harness: run a golden set, store the result, diff two runs.

    python -m eval.harness --dataset eval/datasets/synthetic_golden.jsonl
    python -m eval.harness run --dataset <path> --config hybrid+rerank --k 10
    python -m eval.harness ab --dataset eval/datasets/spec_golden.jsonl
    python -m eval.harness diff --base latest~1 --head latest --dataset <path>
    python -m eval.harness validate --dataset <path>
    python -m eval.harness list --dataset <path>

``ab`` 는 W5 의 네 실험(청킹 단위 · 헤딩 경로 접두사 · 하이브리드 · 리랭커)을
**누적으로 하나씩** 켜면서 사다리를 오른다. 한꺼번에 켜고 "좋아졌다"고 쓰면
어느 것이 얼마나 기여했는지 말할 수 없다는 것이 Notion W5 의 명시적 지시다.
지표 옆에 재인덱싱 시간·인덱스 크기·질의 지연을 같이 찍는다 — W8 케이스
스터디가 "왜 이 조합인가"를 비용까지 포함해 설명해야 하기 때문이다.

L1 only: chunk-level recall@k / MRR / nDCG@10 against gold spans resolved at
run time (``eval/gold.py``). **No LLM is called** — the numbers are
deterministic and the run costs nothing but retrieval, which is what makes it
runnable on every commit. Answer quality (L2) is a separate, judged pass.

``run`` needs Postgres and the embedding/rerank servers. ``validate`` needs
neither, and ``diff`` needs Postgres only when it resolves a reference; with
``--base-json``/``--head-json`` it runs on files alone. 그래서 CI 는 DB 도
모델 서버도 없이 데이터셋과 회귀 판정을 검사할 수 있다.

``diff`` exits non-zero when anything regressed — that is the whole point of
the harness, so it is the default rather than a flag.
"""

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
import uuid
from importlib import import_module
from pathlib import Path

from sqlalchemy import func, select

from eval import datasets, gold, indexing, metrics, report as report_lib, store
from eval.run import CONFIGS

from app.config import settings
from app.constants import EVAL_USER_EMAIL, EVAL_USER_ID, UNUSABLE_PASSWORD_HASH
from app.db import SessionLocal, engine
from app.models import Chunk, Document, User
from app.services import chunking, embeddings, rerank, retrieve

DEFAULT_PROVIDER = "eval.datasets.synthetic"
DEFAULT_CONFIG = "hybrid+rerank"
DEFAULT_K = 10

# W5 의 네 실험을 **누적으로 하나씩** 켠 사다리. Notion 의 명시적 지시다:
# "네 실험을 한꺼번에 켜놓고 '좋아졌다'고 쓰면 기여도를 못 말한다."
#
# A 열(고정 700토큰 · 접두사 없음 · dense only · 리랭커 off)에서 출발해 B 열의
# 값을 한 칸씩 켠다. 각 칸의 기여도는 바로 위 칸과의 차이로 읽는다.
#
# 앞의 두 칸은 **인덱스**를 바꾸므로 재인덱싱이 필요하고, 뒤의 두 칸은 질의
# 시점만 바꾸므로 같은 인덱스를 다시 쓴다 — 러너가 그 사실을 알고 재인덱싱을
# 건너뛴다. 건너뛰는 것이 빨라서가 아니라, 같은 인덱스라는 것이 사실이기
# 때문이다. 괜히 다시 만들면 세 칸의 인덱스 크기가 미세하게 달라 보인다.
AB_LADDER: tuple[tuple[str, str, bool, str], ...] = (
    ("A0-baseline", chunking.STRATEGY_FIXED, False, "dense"),
    ("A1-section", chunking.STRATEGY_SECTION, False, "dense"),
    ("A2-heading-prefix", chunking.STRATEGY_SECTION, True, "dense"),
    ("A3-hybrid", chunking.STRATEGY_SECTION, True, "hybrid"),
    ("A4-rerank", chunking.STRATEGY_SECTION, True, "hybrid+rerank"),
)

# W1 이 정한 유형 비중(70문항 기준). 데이터셋을 강제하지는 않고 validate 가
# 실제 분포와 나란히 찍기만 한다 — 합성 셋은 애초에 이 비율을 못 맞추고,
# 못 맞춘다는 사실 자체가 리포트에 남아야 할 정보다.
W1_TARGET_RATIO = {
    "single_fact": 0.30,
    "multi_doc": 0.25,
    "rule_exception": 0.20,
    "no_answer": 0.15,
    "freshness": 0.10,
}


def git_sha() -> str | None:
    """Short commit of the working tree, marked when it has uncommitted edits.

    커밋 해시를 지표와 같은 행에 남기는 것이 W3 의 본질이다. 다만 작업 트리가
    더러우면 그 해시는 실제로 돈 코드가 아니다 — 표식을 붙여야 나중에 "이
    커밋에서 0.947 이었다"는 기록을 믿을 수 있다.
    """
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def load_provider(path: str):
    module = import_module(path)
    for attr in ("names", "pages_for"):
        if not hasattr(module, attr):
            raise SystemExit(f"provider {path!r} 에 {attr}() 가 없다")
    return module


# --- indexing ---


async def _ensure_eval_user(session) -> None:
    if await session.get(User, EVAL_USER_ID) is not None:
        return
    session.add(
        User(
            id=EVAL_USER_ID,
            email=EVAL_USER_EMAIL,
            password_hash=UNUSABLE_PASSWORD_HASH,
        )
    )
    await session.commit()


def _doc_filename(dataset_name: str, doc: str) -> str:
    return f"m7_{dataset_name}_{doc}.pdf"


async def index_dataset(
    dataset: datasets.Dataset, provider, *, reindex: bool
) -> dict[str, uuid.UUID]:
    """Make the eval account own exactly this dataset's documents.

    Anything else it owns is deleted first. 검색은 문서를 지정하지 않고 코퍼스
    전체를 훑는다 — 질문이 올바른 *문서* 를 찾는지까지 재기 위해서다. 그래서
    이 계정이 가진 것이 곧 코퍼스이고, 지난 데이터셋의 잔재가 남아 있으면
    이번 지표는 그 잔재에 좌우된다.
    """
    wanted = {doc: _doc_filename(dataset.name, doc) for doc in dataset.docs}

    async with SessionLocal() as session:
        await _ensure_eval_user(session)
        owned = (
            (await session.execute(select(Document).where(Document.user_id == EVAL_USER_ID)))
            .scalars()
            .all()
        )
        keep = set(wanted.values())
        existing = {doc.filename: doc for doc in owned if doc.filename in keep}

        # 지우기 전에 먼저 거절한다. 순서를 뒤집으면 --no-reindex 로 잘못 부른
        # 한 번이 다른 데이터셋의 인덱스를 날려버리고 나서 실패한다.
        if not reindex:
            missing = [name for name in wanted.values() if name not in existing]
            if missing:
                raise SystemExit(
                    f"--no-reindex 인데 문서가 없다: {missing} — 먼저 한 번 인덱싱할 것."
                )
            unfinished = [name for name, doc in existing.items() if doc.status != "ready"]
            if unfinished:
                raise SystemExit(f"--no-reindex 인데 인덱싱이 끝나지 않은 문서: {unfinished}")

        for doc in owned:
            if doc.filename not in keep or reindex:
                await session.delete(doc)
        await session.commit()

        if not reindex:
            return {doc: existing[filename].id for doc, filename in wanted.items()}

    doc_ids: dict[str, uuid.UUID] = {}
    for doc, filename in wanted.items():
        pages = provider.pages_for(doc)
        doc_ids[doc] = await indexing.index_pages(
            pages, f"/tmp/{filename}", filename, owner_id=EVAL_USER_ID
        )
    return doc_ids


async def index_size(session) -> tuple[int, int]:
    """(chunk count, content bytes) of the evaluation corpus as indexed.

    인덱스 크기를 실제로 지배하는 것은 청크 **개수**다 — 청크마다 1024차원
    float 밀집벡터(약 4KB)와 희소벡터가 붙는다. 본문 바이트는 그 옆의 참고
    값이다. pg_total_relation_size 를 쓰지 않는 이유는 chunks 테이블이 다른
    사용자의 문서와 한 테이블을 쓰기 때문이다 — 그 숫자는 이 코퍼스를
    설명하지 않는다.
    """
    row = (
        await session.execute(
            select(func.count(Chunk.id), func.coalesce(func.sum(func.octet_length(Chunk.content)), 0))
            .select_from(Chunk)
            .join(Document, Document.id == Chunk.document_id)
            .where(Document.user_id == EVAL_USER_ID)
        )
    ).one()
    return int(row[0]), int(row[1])


# --- gold resolution ---


async def resolve_gold(
    session, dataset: datasets.Dataset, provider, doc_ids: dict[str, uuid.UUID]
) -> dict[str, tuple[list[set[uuid.UUID]], list[tuple[str, int]]]]:
    """Gold chunk sets and gold (doc, page) pairs for every scored question."""
    placed: dict[str, tuple[str, list[gold.PlacedChunk]]] = {}
    for doc, doc_id in doc_ids.items():
        pages = provider.pages_for(doc)
        chunks = [
            gold.SourceChunk(c.id, c.page_from, c.content)
            for c in await indexing.load_chunks(session, doc_id)
        ]
        doc_text, _ = gold.build_document_text(pages)
        placed[doc] = (doc_text, gold.place_chunks(pages, chunks))

    resolved: dict[str, tuple[list[set[uuid.UUID]], list[tuple[str, int]]]] = {}
    for question in dataset.scored():
        sets: list[set[uuid.UUID]] = []
        gold_pages: list[tuple[str, int]] = []
        for span in question.gold_spans:
            doc_text, chunks = placed[span.doc]
            sets.append(
                gold.resolve_span(
                    span.snippet, doc_text, chunks, where=f" ({question.id}/{span.doc})"
                )
            )
            gold_pages += [
                (span.doc, page)
                for page in gold.pages_of(span.snippet, provider.pages_for(span.doc))
            ]
        resolved[question.id] = (sets, gold_pages)
    return resolved


# --- retrieval ---


async def retrieve_chunks(
    session, question: str, config: str, k: int
) -> list[retrieve.RetrievedChunk]:
    """Top-k chunks for one question under one retrieval configuration."""
    if config == "dense":
        embedding = await embeddings.embed_query(question)
        return await retrieve.search(
            session, embedding, user_id=EVAL_USER_ID, top_k=k
        )

    dense, sparse = await embeddings.embed_query_full(question)
    fused = await retrieve.hybrid_search(
        session, dense, sparse, user_id=EVAL_USER_ID
    )
    if config == "hybrid":
        return fused.candidates[:k]

    if not fused.candidates:
        return []
    ranked = await rerank.rerank(question, [c.content for c in fused.candidates])
    out = []
    for idx, score in ranked[:k]:
        chunk = fused.candidates[idx]
        chunk.score = score
        out.append(chunk)
    return out


# --- commands ---


async def run_once(args: argparse.Namespace) -> list[report_lib.RunReport]:
    """Index, score every requested config, and store — the body of ``run``.

    ``cmd_ab`` 가 이 함수를 사다리 칸마다 부른다. 칸마다 별도의 실행으로
    저장돼야 diff 와 list 가 평소처럼 다룰 수 있기 때문이고, A/B 를 위해 저장
    경로를 따로 만들면 그 경로만 검증이 안 된 채로 남는다.
    """
    dataset = datasets.load(args.dataset)
    provider = load_provider(args.provider)
    unknown = [doc for doc in dataset.docs if doc not in provider.names()]
    if unknown:
        raise SystemExit(f"provider {args.provider} 가 모르는 문서: {unknown}")

    ks = tuple(sorted({1, 3, 5, args.k}))
    keys = metrics.metric_keys(ks, ndcg_k=args.k)

    reindexed = not args.no_reindex
    started = time.perf_counter()
    doc_ids = await index_dataset(dataset, provider, reindex=reindexed)
    # --no-reindex 면 재인덱싱이 일어나지 않았다. 0 을 적으면 "0초 걸렸다"는
    # 거짓이 되므로 비워 둔다.
    reindex_seconds = round(time.perf_counter() - started, 3) if reindexed else None

    questions = dataset.scored()
    if args.split != "all":
        questions = [q for q in questions if q.split == args.split]
    if not questions:
        raise SystemExit(f"split={args.split} 에 채점 가능한 문항이 없다")

    reports: list[report_lib.RunReport] = []
    async with SessionLocal() as session:
        index_chunks, index_bytes = await index_size(session)
        resolved = await resolve_gold(session, dataset, provider, doc_ids)

        doc_of = {doc_id: name for name, doc_id in doc_ids.items()}
        for config in args.config:
            rows: list[report_lib.QuestionResult] = []
            latencies: list[float] = []
            for question in questions:
                gold_sets, gold_pages = resolved[question.id]
                asked = time.perf_counter()
                hits = await retrieve_chunks(session, question.question, config, args.k)
                latencies.append((time.perf_counter() - asked) * 1000)
                ranked_ids = [h.chunk_id for h in hits]
                ranked_pages = [(doc_of.get(h.document_id, "?"), h.page_from) for h in hits]
                scored = metrics.score_chunks(
                    ranked_ids,
                    gold_sets,
                    ranked_pages=ranked_pages,
                    gold_pages=gold_pages,
                    ks=ks,
                    ndcg_k=args.k,
                )
                rank = metrics.first_gold_rank(ranked_ids, gold_sets)
                rows.append(
                    report_lib.QuestionResult(
                        question_id=question.id,
                        question_type=question.type,
                        split=question.split,
                        first_gold_rank=rank,
                        metrics=scored,
                        retrieved_chunk_ids=[str(i) for i in ranked_ids],
                        gold_chunk_ids=[str(i) for s in gold_sets for i in sorted(s, key=str)],
                    )
                )
                if args.verbose:
                    mark = "✓" if scored[f"R@{ks[0]}"] else (
                        "~" if scored[f"R@{args.k}"] else "✗"
                    )
                    print(
                        f"[{config:14}] {mark} {question.type:14} "
                        f"순위 {rank if rank else '-':>3} | {question.question[:34]}"
                    )

            report = report_lib.RunReport(
                dataset_name=dataset.name,
                dataset_sha256=dataset.sha256,
                config=config,
                k=args.k,
                git_sha=git_sha(),
                label=args.label,
                chunk_strategy=settings.chunk_strategy,
                heading_prefix=settings.chunk_heading_prefix,
                reindex_seconds=reindex_seconds,
                index_chunks=index_chunks,
                index_bytes=index_bytes,
                latency_ms_p50=round(statistics.median(latencies), 1) if latencies else None,
                latency_ms_mean=round(statistics.fmean(latencies), 1) if latencies else None,
                num_questions=len(dataset),
                num_scored=len(rows),
                metrics=report_lib.aggregate(rows, keys),
                metrics_by_type=report_lib.group_by(rows, "question_type", keys),
                metrics_by_split=report_lib.group_by(rows, "split", keys),
                results=rows,
            )
            reports.append(report)
            report_lib.print_run(report, keys)

        if not args.no_store:
            for report in reports:
                run_id = await store.save(session, report)
                print(f"[store] {report.config} -> run {run_id}")
        else:
            print("\n[store] --no-store: 이번 실행은 저장하지 않았다.")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([r.to_dict() for r in reports], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[json] {args.json_out}")
    return reports


async def cmd_run(args: argparse.Namespace) -> int:
    await run_once(args)
    await engine.dispose()
    return 0


# --- A/B ---


def print_ladder(reports: list[report_lib.RunReport], k: int) -> None:
    """The cumulative ladder, one row per rung, with cost beside quality.

    recall 과 precision 을 같은 표에 둔다. Notion 함정 1번이 "recall 만 올리고
    precision 을 버리면 컨텍스트가 늘어 비용·지연이 나빠진다"이고, 두 칸이
    떨어져 있으면 사람 눈은 올라간 쪽만 본다. Δ 열은 **바로 위 칸과의 차이**다 —
    기여도를 칸마다 따로 말할 수 있어야 이 사다리를 도는 의미가 있다.
    """
    print(f"\n=== 누적 A/B 사다리 (k={k}) ===")
    head = (
        f"{'arm':20}{'chunking':18}{f'R@{k}':>8}{'Δ':>8}"
        f"{f'P@{k}':>8}{'Δ':>8}{'MRR':>8}{'청크':>7}{'재색인s':>9}{'지연ms':>9}"
    )
    print(head)
    previous: report_lib.RunReport | None = None
    for report in reports:
        recall = report.metrics.get(f"R@{k}", 0.0)
        precision = report.metrics.get(f"P@{k}", 0.0)
        d_r = recall - previous.metrics.get(f"R@{k}", 0.0) if previous else 0.0
        d_p = precision - previous.metrics.get(f"P@{k}", 0.0) if previous else 0.0
        chunking_label = (
            f"{report.chunk_strategy}{'+prefix' if report.heading_prefix else ''}"
        )
        print(
            f"{(report.label or report.config)[:19]:20}{chunking_label:18}"
            f"{recall:8.3f}{(f'{d_r:+.3f}' if previous else '-'):>8}"
            f"{precision:8.3f}{(f'{d_p:+.3f}' if previous else '-'):>8}"
            f"{report.metrics.get('MRR', 0.0):8.3f}"
            f"{(report.index_chunks if report.index_chunks is not None else '-'):>7}"
            f"{(f'{report.reindex_seconds:.1f}' if report.reindex_seconds is not None else '(재사용)'):>9}"
            f"{(f'{report.latency_ms_p50:.0f}' if report.latency_ms_p50 is not None else '-'):>9}"
        )
        previous = report
    print(
        "\nΔ 는 바로 위 칸과의 차이다. 한 칸에서 recall 이 오르는데 precision 이 "
        "같이 내려가면\n그 칸은 컨텍스트를 넓혀서 recall 을 산 것이고, 비용·지연 "
        "열과 함께 읽어야 한다."
    )


async def cmd_ab(args: argparse.Namespace) -> int:
    """Walk ``AB_LADDER``, turning one experiment on per rung."""
    rungs = [rung for rung in AB_LADDER if not args.only or rung[0] in args.only]
    if not rungs:
        raise SystemExit(f"--only 에 해당하는 칸이 없다: {args.only}")

    original = (settings.chunk_strategy, settings.chunk_heading_prefix)
    reports: list[report_lib.RunReport] = []
    previous_chunking: tuple[str, bool] | None = None
    try:
        for name, strategy, prefix, config in rungs:
            settings.chunk_strategy = strategy
            settings.chunk_heading_prefix = prefix
            # 청킹이 그대로면 인덱스도 그대로다. 다시 만들면 시간만 쓰는 것이
            # 아니라, 같은 인덱스인데 크기가 미세하게 달라 보여 사다리를
            # 읽는 사람을 헷갈리게 한다.
            reuse = previous_chunking == (strategy, prefix)
            print(f"\n\n########## {name} · {strategy}"
                  f"{'+prefix' if prefix else ''} · {config}"
                  f"{' · 인덱스 재사용' if reuse else ' · 재인덱싱'} ##########")
            rung_args = argparse.Namespace(
                dataset=args.dataset,
                config=[config],
                k=args.k,
                split=args.split,
                provider=args.provider,
                label=name,
                no_store=args.no_store,
                no_reindex=reuse,
                json_out=None,
                verbose=args.verbose,
            )
            reports += await run_once(rung_args)
            previous_chunking = (strategy, prefix)
    finally:
        settings.chunk_strategy, settings.chunk_heading_prefix = original
        await engine.dispose()

    print_ladder(reports, args.k)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([r.to_dict() for r in reports], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[json] {args.json_out}")
    return 0


def _load_json_run(path: str, config: str | None) -> report_lib.RunReport:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    runs = raw if isinstance(raw, list) else [raw]
    for entry in runs:
        if config is None or entry["config"] == config:
            return report_lib.RunReport.from_dict(entry)
    raise SystemExit(f"{path} 에 config={config} 인 실행이 없다")


async def cmd_diff(args: argparse.Namespace) -> int:
    dataset_name = Path(args.dataset).stem if args.dataset else None

    if args.base_json and args.head_json:
        base = _load_json_run(args.base_json, args.config)
        head = _load_json_run(args.head_json, args.config)
    else:
        async with SessionLocal() as session:
            base = await store.resolve(
                session, args.base, dataset_name=dataset_name, config=args.config
            )
            head = await store.resolve(
                session, args.head, dataset_name=dataset_name, config=args.config
            )
        await engine.dispose()

    result = report_lib.diff(
        base, head, tolerance=args.tolerance, strict=not args.allow_dataset_change
    )
    report_lib.print_diff(result)
    # 회귀가 있으면 non-zero. CI 가 이 값 하나로 커밋을 막는다.
    return 1 if result.has_regression else 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Check a dataset against the format spec and the documents it points at.

    DB 도 모델 서버도 필요 없다. 이게 CI 가 매 커밋 돌릴 수 있는 검사이고,
    문서가 바뀌었는데 골든셋이 따라가지 못한 상황을 바로 그 커밋에서 잡는다 —
    W1 이 "고치지 말고 CI 에서 깨지게 두라"고 적은 함정이다.
    """
    dataset = datasets.load(args.dataset)
    provider = load_provider(args.provider)
    problems: list[str] = []

    for question in dataset.questions:
        for span in question.gold_spans:
            try:
                pages = provider.pages_for(span.doc)
            except KeyError as exc:
                problems.append(f"{question.id}: {exc}")
                continue
            count = gold.match_count(span.snippet, pages)
            if count == 0:
                problems.append(
                    f"{question.id}: 스니펫이 {span.doc} 에서 사라졌다 — "
                    f"{span.snippet[:40]!r}"
                )
            elif count > gold.MAX_MATCHES:
                problems.append(
                    f"{question.id}: 스니펫이 {span.doc} 안 {count}곳에 걸린다 "
                    f"(상한 {gold.MAX_MATCHES}) — {span.snippet[:40]!r}"
                )

    total = len(dataset)
    print(f"{dataset.name}: {total}문항, sha256 {dataset.sha256[:12]}")
    print(f"{'유형':16}{'개수':>6}{'비율':>8}{'W1 목표':>9}")
    counts = dataset.type_counts()
    for qtype, target in W1_TARGET_RATIO.items():
        share = counts[qtype] / total
        print(f"{qtype:16}{counts[qtype]:>6}{share:>8.0%}{target:>9.0%}")
    splits = dataset.split_counts()
    print(
        f"split: tune {splits['tune']} ({splits['tune'] / total:.0%}) / "
        f"holdout {splits['holdout']} ({splits['holdout'] / total:.0%})"
    )
    print(f"문서: {', '.join(dataset.docs)}")
    print(f"L1 채점 대상: {len(dataset.scored())}문항 "
          f"(no_answer {counts['no_answer']}문항은 L2 거부 정확도로 채점)")

    if problems:
        print(f"\n✗ {len(problems)}건")
        for line in problems:
            print(f"  {line}")
        return 1
    print("\n✓ 모든 gold 스니펫이 문서에서 고유하게 잡힌다")
    return 0


async def cmd_list(args: argparse.Namespace) -> int:
    dataset_name = Path(args.dataset).stem if args.dataset else None
    async with SessionLocal() as session:
        runs = await store.recent(
            session, dataset_name=dataset_name, config=args.config, limit=args.limit
        )
    await engine.dispose()
    if not runs:
        print("저장된 실행이 없다.")
        return 0
    print(f"{'run':38}{'when':26}{'config':16}{'git':14}{'R@5':>7}{'MRR':>7}")
    for run in runs:
        print(
            f"{run.run_id:38}{(run.created_at or '')[:25]:26}{run.config:16}"
            f"{(run.git_sha or '-'):14}"
            f"{run.metrics.get('R@5', 0.0):7.3f}{run.metrics.get('MRR', 0.0):7.3f}"
        )
    return 0


# --- CLI ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eval.harness", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="데이터셋을 돌려 L1 지표를 낸다")
    run.add_argument("--dataset", required=True)
    run.add_argument(
        "--config", action="append", choices=CONFIGS,
        help=f"검색 구성 (반복 가능, 기본 {DEFAULT_CONFIG})",
    )
    run.add_argument("--k", type=int, default=DEFAULT_K, help="cutoff (기본 10)")
    run.add_argument(
        "--split", default="tune", choices=("tune", "holdout", "all"),
        help="기본 tune — holdout 은 W8 최종 측정에서만 연다",
    )
    run.add_argument("--provider", default=DEFAULT_PROVIDER)
    run.add_argument("--label", default=None, help="이 실행에 붙일 이름")
    run.add_argument("--no-store", action="store_true", help="Postgres 에 저장하지 않는다")
    run.add_argument(
        "--no-reindex", action="store_true",
        help="이미 인덱싱된 문서를 그대로 쓴다 (반복 실행용)",
    )
    run.add_argument("--json-out", default=None, help="실행 결과를 JSON 파일로도 쓴다")
    run.add_argument("-v", "--verbose", action="store_true")

    ab = sub.add_parser(
        "ab", help="W5 의 네 실험을 누적으로 하나씩 켜면서 돌린다 (Notion 지시)"
    )
    ab.add_argument("--dataset", required=True)
    ab.add_argument("--k", type=int, default=DEFAULT_K)
    ab.add_argument(
        "--split", default="tune", choices=("tune", "holdout", "all"),
        help="기본 tune — holdout 은 W8 최종 측정에서만 연다",
    )
    ab.add_argument("--provider", default=DEFAULT_PROVIDER)
    ab.add_argument(
        "--only", action="append", choices=[rung[0] for rung in AB_LADDER],
        help="사다리의 특정 칸만 (반복 가능). 기본은 전부",
    )
    ab.add_argument("--no-store", action="store_true")
    ab.add_argument("--json-out", default=None)
    ab.add_argument("-v", "--verbose", action="store_true")

    diff = sub.add_parser("diff", help="두 실행 사이의 지표 변화와 뒤집힌 문항")
    diff.add_argument("--base", default="latest~1")
    diff.add_argument("--head", default="latest")
    diff.add_argument("--dataset", default=None, help="참조를 좁히는 데이터셋 경로")
    diff.add_argument("--config", default=DEFAULT_CONFIG, choices=CONFIGS)
    diff.add_argument("--base-json", default=None)
    diff.add_argument("--head-json", default=None)
    diff.add_argument(
        "--tolerance", type=float, default=0.0,
        help="집계 지표가 이만큼까지 떨어지는 것은 회귀로 보지 않는다 (기본 0)",
    )
    diff.add_argument("--allow-dataset-change", action="store_true")

    validate = sub.add_parser("validate", help="포맷과 gold 스니펫을 검사한다 (DB 불필요)")
    validate.add_argument("--dataset", required=True)
    validate.add_argument("--provider", default=DEFAULT_PROVIDER)

    listing = sub.add_parser("list", help="저장된 실행 목록")
    listing.add_argument("--dataset", default=None)
    listing.add_argument("--config", default=None, choices=CONFIGS)
    listing.add_argument("--limit", type=int, default=20)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `--dataset ...` 를 바로 쓰면 run 으로 친다. 매일 치는 명령이 하나뿐인데
    # 하위 명령을 매번 적게 하는 것은 쓰는 사람을 괴롭히는 것 말고 하는 일이 없다.
    if argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv.insert(0, "run")

    args = build_parser().parse_args(argv)
    if args.command == "run":
        args.config = args.config or [DEFAULT_CONFIG]
        return asyncio.run(cmd_run(args))
    if args.command == "ab":
        return asyncio.run(cmd_ab(args))
    if args.command == "diff":
        return asyncio.run(cmd_diff(args))
    if args.command == "validate":
        return cmd_validate(args)
    return asyncio.run(cmd_list(args))


if __name__ == "__main__":
    raise SystemExit(main())
