"""M7 L2 CLI: score answers with the local judge, then check the judge itself.

    python -m eval.l2_run rubric                                  # 루브릭 전문·버전·해시
    python -m eval.l2_run score  --in /tmp/golden_records.json --out /tmp/l2.jsonl
    python -m eval.l2_run report --records /tmp/l2.jsonl
    python -m eval.l2_run labels --records /tmp/l2.jsonl --out /tmp/l2_labels.jsonl
    python -m eval.l2_run kappa  --records /tmp/l2.jsonl --labels /tmp/l2_labels.jsonl
    python -m eval.l2_run compare --base /tmp/l2_before.jsonl --head /tmp/l2_after.jsonl

``score`` needs the local judge server; every other subcommand needs nothing —
no database, no model, no network. 그래야 루브릭 검사·kappa 계산·비교 거부가
judge 없이도 검증된다(eval/report.py 가 DB 없이 회귀를 판정하는 것과 같은 이유).

**Everything this CLI prints about quality is marked unvalidated** until a
human has labelled a sample and ``kappa`` has produced a number at or above the
W6 adoption line. That mark is not decoration in the output: it is in the
stored records, and it is in the aggregate key names.
"""

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

from eval import l2
from eval.judge_local import JudgeUnavailable, LocalJudge, judge_one


def _load_items(path: str) -> list[dict]:
    """Read either a golden_run JSON array or a jsonl of the same records."""
    text = Path(path).read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _load_labels(path: str) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _fmt(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:5.2f}"


def _banner() -> None:
    print()
    print(l2.BANNER)
    print()


# --- rubric ---


def cmd_rubric(args: argparse.Namespace) -> int:
    print(f"rubric_version : {l2.RUBRIC_VERSION}")
    print(f"rubric_sha256  : {l2.rubric_sha256()}")
    print(f"scale          : {l2.SCALE_MIN}~{l2.SCALE_MAX} (순서형)")
    print(f"dimensions     : {', '.join(l2.DIMENSIONS)}")
    print()
    print(l2.rubric_text())
    _banner()
    return 0


# --- score ---


async def cmd_score(args: argparse.Namespace) -> int:
    items = _load_items(args.src)
    if args.limit:
        items = items[: args.limit]
    judge = LocalJudge(model=args.model) if args.model else LocalJudge()

    print(f"judge   : {judge.provider} · {judge.model} @ {judge.base_url}")
    print(f"rubric  : {l2.RUBRIC_VERSION} ({l2.rubric_sha256()[:12]})")
    print(f"문항    : {len(items)}")
    _banner()

    records: list[l2.L2Record] = []
    for i, item in enumerate(items, start=1):
        # 한 번에 하나씩. MPS 가 하나뿐이라 동시 추론은 이 저장소의 모든 지연
        # 측정을 오염시킨다(eval/judge_local.py 의 CONCURRENCY).
        try:
            record = await judge_one(item, judge, variant=args.variant)
        except (JudgeUnavailable, ValueError) as exc:
            print(f"\n[{i}/{len(items)}] 판정 실패: {exc}", file=sys.stderr)
            if records:
                l2.write_records(args.out, records)
                print(f"여기까지 {len(records)}건을 {args.out} 에 남겼다.", file=sys.stderr)
            return 2
        records.append(record)
        scores = " ".join(
            f"{v.dimension[:5]}={v.score}" for v in record.verdicts
        )
        print(
            f"[{i:2}/{len(items)}] {record.question_type:14} {scores:34} "
            f"{record.latency_ms:>6}ms  {str(record.question)[:30]}"
        )
        # 문항마다 저장한다. 중간에 죽어도 이미 치른 추론을 버리지 않는다
        # (eval/golden_run.py 와 같은 이유).
        l2.write_records(args.out, records)

    print(f"\n{len(records)} records -> {args.out}")
    _print_report(records)
    return 0


# --- report ---


def _print_report(records: list[l2.L2Record]) -> None:
    if not records:
        print("(레코드 없음)")
        return
    validation = records[0].validation
    agg = l2.aggregate(records)
    keys = [l2.metric_key(d, validation) for d in l2.DIMENSIONS]

    print(
        f"\n=== L2 ({records[0].judge_model} · 루브릭 {records[0].rubric_version} "
        f"· 문항 {len(records)}) ==="
    )
    header = "".join(f"{k.split('__')[0][:14]:>15}" for k in keys)
    print(f"{'group':16}{'n':>4}{header}")
    rows = {"ALL": (len(records), agg)}
    for name, by_type in l2.group_by_type(records).items():
        rows[name] = (sum(1 for r in records if r.question_type == name), by_type)
    for name, (count, values) in rows.items():
        cells = "".join(f"{_fmt(values.get(k)):>15}" for k in keys)
        print(f"{name:16}{count:>4}{cells}")
    print(f"\n척도 {l2.SCALE_MIN}~{l2.SCALE_MAX}. 집계 키: {', '.join(keys)}")

    low = [
        (r, v)
        for r in records
        for v in r.verdicts
        if v.score is not None and v.score <= 1
    ]
    if low:
        print(f"\n=== 낮은 판정 ({len(low)}건) ===")
        for record, verdict in low[:12]:
            print(f"  [{record.question_type}] {record.question_id[:40]}")
            print(f"    {verdict.dimension}={verdict.score}  {verdict.evidence[:90]}")
    _banner()


def cmd_report(args: argparse.Namespace) -> int:
    _print_report(l2.read_records(args.records))
    return 0


# --- labels ---


def cmd_labels(args: argparse.Namespace) -> int:
    """Emit the blank human-label file. 라벨링 UI 는 만들지 않는다 — 조인만 된다."""
    records = l2.read_records(args.records)
    if args.sample and args.sample < len(records):
        # 유형 고르게(W6). 전체에서 무작위로 뽑으면 single_fact 가 과반인
        # 데이터셋에서 no_answer 문항이 한두 건만 걸리고, 거부 정확도의 kappa
        # 는 표본이 없어 계산되지 않는다.
        rng = random.Random(args.seed)
        by_type: dict[str, list[l2.L2Record]] = {}
        for record in records:
            by_type.setdefault(record.question_type, []).append(record)
        for rows in by_type.values():
            rng.shuffle(rows)
        picked: list[l2.L2Record] = []
        while len(picked) < args.sample and any(by_type.values()):
            for name in sorted(by_type):
                if by_type[name] and len(picked) < args.sample:
                    picked.append(by_type[name].pop())
        records = picked

    rows = l2.label_rows(records)
    Path(args.out).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    types = sorted({r.question_type for r in records})
    print(f"{len(records)} 문항 · {len(rows)} 판정 -> {args.out}")
    print(f"유형: {', '.join(types)}")
    print(
        "\n각 행의 human_score 를 루브릭대로 채운다 "
        f"({l2.SCALE_MIN}~{l2.SCALE_MAX} 정수). judge_score 를 **보지 말고** "
        "먼저 채우는 것이 요점이다 — 보고 채우면 kappa 는 자기 자신과의 일치도가 된다."
    )
    print("루브릭 전문: python -m eval.l2_run rubric")
    print(
        f"채운 뒤: python -m eval.l2_run kappa --records {args.records} "
        f"--labels {args.out}"
    )
    return 0


# --- kappa ---


def cmd_kappa(args: argparse.Namespace) -> int:
    records = l2.read_records(args.records)
    labels = _load_labels(args.labels)
    try:
        result = l2.kappa_report(records, labels)
    except (l2.LabelMismatch, l2.KappaUndefined) as exc:
        print(f"kappa 를 낼 수 없다: {exc}", file=sys.stderr)
        return 2

    print(f"=== judge-사람 일치도 ({result['judge_model']}) ===")
    print(f"루브릭 {result['rubric_version']} ({str(result['rubric_sha256'])[:12]})")
    print(f"\n{'dimension':20}{'n':>5}{'none':>9}{'linear':>9}{'quadratic':>11}")
    for dimension, row in result["per_dimension"].items():
        k = row["kappa"]
        print(
            f"{dimension:20}{row['n']:>5}"
            f"{_fmt(k['none']):>9}{_fmt(k['linear']):>9}{_fmt(k['quadratic']):>11}"
        )
    pooled = result["pooled"]
    print(
        f"{'POOLED':20}{pooled['n']:>5}"
        f"{_fmt(pooled['kappa'].get('none')):>9}"
        f"{_fmt(pooled['kappa'].get('linear')):>9}"
        f"{_fmt(pooled['kappa'].get('quadratic')):>11}"
    )
    print(
        "\n헤드라인은 **이차 가중** kappa 다 — 이 루브릭의 점수가 순서형이라, "
        "3점을 2점으로 본 것과 3점을 0점으로 본 것을 같은 불일치로 세면 안 된다. "
        "가중 없는 값도 함께 찍는 이유는 이차 가중이 점수가 쏠린 표본에서 "
        "후하게 나오기 때문이다."
    )
    print(f"\n헤드라인 kappa(quadratic): {_fmt(result['headline_kappa_quadratic']).strip()}")
    print(f"판정: {result['verdict']}")

    headline = result["headline_kappa_quadratic"]
    if headline is None or headline < l2.KAPPA_ADOPT_AT:
        # 미달을 exit 0 으로 내보내면 CI 도 사람도 통과로 읽는다. 하네스가
        # 회귀에 non-zero 를 쓰는 것과 같은 판단이다.
        print(
            "\n아직 채택선 미만이다. 이 judge 의 L2 수치는 계속 unvalidated 이고, "
            "W6 의 대안은 'L2 는 샘플 사람 평가로 대체하고 L1·L3 만 자동화한다' 이다.",
            file=sys.stderr,
        )
        return 1
    print(
        "\n채택선을 넘었다. eval/l2.py 의 RUBRIC 을 잠그고(고치면 버전을 올려야 한다), "
        "Validation(status='validated', kappa=...) 으로 레코드를 다시 찍을 것."
    )
    return 0


# --- compare ---


def cmd_compare(args: argparse.Namespace) -> int:
    base = l2.read_records(args.base)
    head = l2.read_records(args.head)
    try:
        l2.check_comparable(base, head)
    except l2.IncomparableRubrics as exc:
        print(f"비교 거부: {exc}", file=sys.stderr)
        return 2

    validation = head[0].validation
    print(f"=== L2 변화 (루브릭 {head[0].rubric_version} · {head[0].judge_model}) ===")
    print(f"{'dimension':22}{'base':>9}{'head':>9}{'delta':>9}")
    a, b = l2.aggregate(base), l2.aggregate(head)
    for dimension in l2.DIMENSIONS:
        key = l2.metric_key(dimension, validation)
        x, y = a.get(key), b.get(key)
        if x is None or y is None:
            print(f"{dimension:22}{_fmt(x):>9}{_fmt(y):>9}{'  n/a':>9}")
            continue
        print(f"{dimension:22}{x:>9.2f}{y:>9.2f}{y - x:>+9.2f}")
    _banner()
    return 0


# --- parser ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eval.l2_run", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("rubric", help="루브릭 전문과 버전·해시를 찍는다")

    score = sub.add_parser("score", help="로컬 judge 로 답변을 채점한다")
    score.add_argument("--in", dest="src", default="/tmp/golden_records.json")
    score.add_argument("--out", default="/tmp/l2_records.jsonl")
    score.add_argument("--limit", type=int, default=0, help="앞 N문항만")
    score.add_argument("--model", default=None, help="judge 모델 재정의")
    score.add_argument(
        "--variant",
        default="server",
        help="이 답변을 만든 쪽. W6 의 생성 위치 비교가 붙을 자리다",
    )

    report = sub.add_parser("report", help="채점 결과 집계")
    report.add_argument("--records", default="/tmp/l2_records.jsonl")

    labels = sub.add_parser("labels", help="사람 라벨 서식을 뽑는다")
    labels.add_argument("--records", default="/tmp/l2_records.jsonl")
    labels.add_argument("--out", default="/tmp/l2_labels.jsonl")
    labels.add_argument("--sample", type=int, default=40, help="W6 권장 30~50")
    labels.add_argument("--seed", type=int, default=7)

    kappa = sub.add_parser("kappa", help="judge-사람 일치도. 미달이면 exit 1")
    kappa.add_argument("--records", default="/tmp/l2_records.jsonl")
    kappa.add_argument("--labels", default="/tmp/l2_labels.jsonl")

    compare = sub.add_parser("compare", help="두 채점 결과의 차이. 루브릭이 다르면 거부")
    compare.add_argument("--base", required=True)
    compare.add_argument("--head", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "rubric":
        return cmd_rubric(args)
    if args.cmd == "score":
        return asyncio.run(cmd_score(args))
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "labels":
        return cmd_labels(args)
    if args.cmd == "kappa":
        return cmd_kappa(args)
    if args.cmd == "compare":
        return cmd_compare(args)
    raise SystemExit(f"알 수 없는 명령 {args.cmd!r}")


if __name__ == "__main__":
    raise SystemExit(main())
