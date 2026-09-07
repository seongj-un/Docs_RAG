"""Score evaluation records with the LLM judge and report the four metrics.

Consumes the JSON produced by ``eval.golden_run`` — so scoring never re-runs
the pipeline, and a run can be re-scored without holding the embedding server
open.

    python -m eval.judge_run --in /tmp/golden_records.json --out /tmp/golden_scored.json
    python -m eval.judge_run --in a.json --compare b.json   # before/after

Only an LLM key is required; Postgres and the embedding server are not.
"""

import argparse
import asyncio
import json
from pathlib import Path

from eval.judge import THRESHOLDS, diagnose, judge_record
from eval.throttle import Throttle

METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _aggregate(rows: list[dict]) -> dict[str, float | None]:
    return {m: _mean([r["scores"].get(m) for r in rows]) for m in METRICS}


def _fmt(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:5.3f}"


def _print_table(title: str, rows: list[dict]) -> None:
    if not rows:
        return
    agg = _aggregate(rows)
    print(f"\n=== {title} (n={len(rows)}) ===")
    print(f"{'metric':20}{'score':>8}{'임계':>8}  판정")
    for metric in METRICS:
        value = agg[metric]
        threshold = THRESHOLDS[metric]
        if value is None:
            verdict = "측정 불가"
        else:
            verdict = "OK" if value >= threshold else "⚠ 미달"
        print(f"{metric:20}{_fmt(value):>8}{threshold:>8.2f}  {verdict}")


def _print_diagnosis(rows: list[dict]) -> None:
    print("\n=== 임계 미달 케이스 진단 ===")
    flagged = []
    for row in rows:
        scores = row["scores"]
        below = [
            m for m in METRICS
            if scores.get(m) is not None and scores[m] < THRESHOLDS[m]
        ]
        if below:
            flagged.append((row, below, diagnose(scores)))
    if not flagged:
        print("  (없음 — 모든 문항이 임계 이상)")
        return
    for row, below, cause in flagged:
        print(f"\n  [{row['type']}] {row['question'][:44]}")
        print(f"    미달: {', '.join(f'{m}={row['scores'][m]:.2f}' for m in below)}")
        print(f"    진단: {cause}")
        detail = row.get("detail", {})
        for claim in detail.get("unsupported_claims", [])[:2]:
            print(f"    근거없는 주장: {claim[:60]}")
        for fact in detail.get("missing_facts", [])[:2]:
            print(f"    컨텍스트에 없는 사실: {fact[:60]}")


def _print_comparison(before: list[dict], after: list[dict]) -> None:
    print("\n=== 개선 전후 비교 ===")
    a, b = _aggregate(before), _aggregate(after)
    print(f"{'metric':20}{'before':>9}{'after':>9}{'delta':>9}")
    for metric in METRICS:
        x, y = a[metric], b[metric]
        if x is None or y is None:
            print(f"{metric:20}{_fmt(x):>9}{_fmt(y):>9}{'  n/a':>9}")
            continue
        delta = y - x
        arrow = "▲" if delta > 0.001 else ("▼" if delta < -0.001 else "=")
        print(f"{metric:20}{x:>9.3f}{y:>9.3f}{delta:>+8.3f} {arrow}")


async def score_all(records: list[dict], rpm: int) -> list[dict]:
    throttle = Throttle(rpm)
    print(f"[pace] {rpm} req/min -> ~{len(records) / max(rpm, 1):.1f}min\n")
    scored: list[dict] = []
    for i, record in enumerate(records, start=1):
        result = await judge_record(record, throttle)
        row = dict(record)
        row["scores"] = {
            "faithfulness": result.faithfulness,
            "answer_relevancy": result.answer_relevancy,
            "context_precision": result.context_precision,
            "context_recall": result.context_recall,
        }
        row["detail"] = result.detail
        scored.append(row)
        # Short labels: context_precision and context_recall both start with
        # "cont", so truncation alone would make the two indistinguishable.
        labels = {
            "faithfulness": "faith",
            "answer_relevancy": "rel",
            "context_precision": "prec",
            "context_recall": "rec",
        }
        summary = " ".join(
            f"{labels[m]}={_fmt(row['scores'][m]).strip()}" for m in METRICS
        )
        print(f"[{i:2}/{len(records)}] {summary}  {record['question'][:32]}")
    return scored


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="src", default="/tmp/golden_records.json")
    parser.add_argument("--out", default="/tmp/golden_scored.json")
    parser.add_argument("--rpm", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--compare",
        help="already-scored JSON to compare against (skips judging)",
    )
    args = parser.parse_args()

    records = json.loads(Path(args.src).read_text(encoding="utf-8"))

    if args.compare:
        after = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        _print_comparison(records, after)
        return

    if args.limit:
        records = records[: args.limit]

    scored = await score_all(records, args.rpm)
    Path(args.out).write_text(
        json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n{len(scored)} scored -> {args.out}")

    answerable = [r for r in scored if r["type"] != "unanswerable"]
    _print_table("ALL (답변 가능 문항)", answerable)
    for qtype in sorted({r["type"] for r in answerable}):
        _print_table(qtype, [r for r in answerable if r["type"] == qtype])

    refusals = [r for r in scored if r["type"] == "unanswerable"]
    if refusals:
        correct = sum(1 for r in refusals if r["refused"])
        print(f"\n=== 거부 문항 (n={len(refusals)}) ===")
        print(f"  올바로 거부: {correct}/{len(refusals)}")
        print(f"  answer_relevancy 평균: {_fmt(_mean([r['scores']['answer_relevancy'] for r in refusals]))}")

    _print_diagnosis(answerable)


if __name__ == "__main__":
    asyncio.run(main())
