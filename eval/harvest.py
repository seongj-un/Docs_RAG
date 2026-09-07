"""Harvest failure cases into golden-set candidates.

Closes the M4 loop: *"실패한 답변은 전부 테스트 케이스가 된다."* A failure that
is only ever looked at once teaches nothing; folded back into the golden set it
becomes a regression guard, and the evaluation data becomes the product's
memory.

Two sources:
- **scored records** (default) — the eval run's own output. Safe: these are our
  own fixtures.
- **traces** (``--from-traces``) — real queries users asked. Off by default:
  harvesting them copies user-written questions into a repository fixture, so
  it takes an explicit flag and prints a warning. Review before committing.

Candidates are emitted with the labels a human must supply left blank
(``reference``, ``gold_pages``); everything the reviewer needs to fill them —
what was asked, what came back, which pages were retrieved — is included. The
labelling stays manual on purpose: a golden set labelled by the system under
test cannot detect that system's own blind spots.

    python -m eval.harvest --scored /tmp/scored_after.json --out /tmp/candidates.json
"""

import argparse
import asyncio
import json
from pathlib import Path

from eval.corpora import golden
from eval.judge import THRESHOLDS, diagnose

# Trace-sourced candidates carry no reference answer, so they are always
# "needs labelling"; scored ones at least know which question they came from.
NEEDS_LABEL = ""


def _normalize(question: str) -> str:
    return " ".join(question.split()).strip().lower()


def existing_questions() -> set[str]:
    return {_normalize(item["q"]) for item in golden.QUESTIONS}


def failures_from_scored(records: list[dict]) -> list[dict]:
    """Records that failed a threshold, or refused something answerable."""
    out = []
    for record in records:
        scores = record.get("scores", {})
        below = [
            metric
            for metric, threshold in THRESHOLDS.items()
            if scores.get(metric) is not None and scores[metric] < threshold
        ]
        wrongly_refused = record["type"] != "unanswerable" and record.get("refused")
        if not below and not wrongly_refused:
            continue
        reasons = []
        if wrongly_refused:
            reasons.append("답변 가능한데 거부")
        reasons += [f"{m}={scores[m]:.2f} < {THRESHOLDS[m]}" for m in below]
        out.append({
            "question": record["question"],
            "source": "scored",
            "reasons": reasons,
            "diagnosis": diagnose(scores),
            "observed_answer": record.get("answer"),
            "retrieved_pages": record.get("retrieved_pages", []),
            "document": record.get("document"),
            # Already labelled upstream — carried over so a reviewer can see
            # what the expected answer was when this case regressed.
            "reference": record.get("reference", NEEDS_LABEL),
            "gold_pages": record.get("gold_pages", []),
            "type": record.get("type", NEEDS_LABEL),
        })
    return out


async def failures_from_traces(limit: int) -> list[dict]:
    """Refused production queries, newest first."""
    from sqlalchemy import select

    from app.db import SessionLocal, engine
    from app.models import Trace

    try:
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(Trace)
                    .where(Trace.refused.is_(True), Trace.cached.is_(False))
                    .order_by(Trace.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [{
                "question": row.question,
                "source": "trace",
                "reasons": ["프로덕션 질의가 거부됨"],
                "diagnosis": "미판정 (트레이스에는 기준답변이 없음)",
                "observed_answer": row.answer,
                "retrieved_pages": [],
                "document": NEEDS_LABEL,
                "reference": NEEDS_LABEL,
                "gold_pages": [],
                "type": NEEDS_LABEL,
            } for row in rows]
    finally:
        await engine.dispose()


def dedupe(candidates: list[dict], known: set[str]) -> tuple[list[dict], int]:
    """Drop candidates already in the golden set or repeated within the batch."""
    seen = set(known)
    fresh = []
    dropped = 0
    for candidate in candidates:
        key = _normalize(candidate["question"])
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        fresh.append(candidate)
    return fresh, dropped


def render_snippet(candidates: list[dict]) -> str:
    """A paste-ready block for eval/corpora/golden.py."""
    lines = ["# --- harvested candidates: fill reference / gold_pages / type ---"]
    for candidate in candidates:
        lines.append(
            "    {"
            f'"q": {candidate["question"]!r}, '
            f'"doc": {candidate["document"] or "TODO"!r}, '
            f'"gold_pages": {candidate["gold_pages"] or []}, '
        )
        lines.append(
            f'     "reference": {candidate["reference"] or "TODO"!r}, '
            f'"type": {candidate["type"] or "TODO"!r}}},'
        )
        lines.append(f"    # 왜: {'; '.join(candidate['reasons'])} -> {candidate['diagnosis']}")
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scored", help="scored records JSON from eval.judge_run")
    parser.add_argument(
        "--from-traces",
        action="store_true",
        help="also harvest refused production queries (contains user data)",
    )
    parser.add_argument("--trace-limit", type=int, default=50)
    parser.add_argument("--out", default="/tmp/golden_candidates.json")
    args = parser.parse_args()

    candidates: list[dict] = []
    if args.scored:
        records = json.loads(Path(args.scored).read_text(encoding="utf-8"))
        found = failures_from_scored(records)
        print(f"[scored] {len(records)}건 중 실패 {len(found)}건")
        candidates += found

    if args.from_traces:
        print(
            "[warn] 트레이스 수확은 실제 사용자가 작성한 질문을 저장소 픽스처로\n"
            "       옮깁니다. 커밋 전에 반드시 검토하세요."
        )
        found = await failures_from_traces(args.trace_limit)
        print(f"[traces] 거부된 프로덕션 질의 {len(found)}건")
        candidates += found

    if not args.scored and not args.from_traces:
        parser.error("--scored 또는 --from-traces 중 하나는 필요합니다")

    fresh, dropped = dedupe(candidates, existing_questions())
    print(f"[dedupe] 골든셋에 이미 있거나 중복 → {dropped}건 제외, 신규 {len(fresh)}건")

    Path(args.out).write_text(
        json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n{len(fresh)} candidates -> {args.out}")

    if fresh:
        print("\n=== 라벨링 필요 (eval/corpora/golden.py 에 붙여넣기) ===")
        print(render_snippet(fresh))
    else:
        print("\n신규 후보 없음 — 실패 케이스가 모두 이미 골든셋에 있습니다.")


if __name__ == "__main__":
    asyncio.run(main())
