"""Persist eval runs to Postgres and read them back as ``RunReport``s.

Reuses the app's own engine and models (``app/db.py``, ``app/models.py``) —
the evaluation history lives in the same database as everything else, so a
backup that captures the product captures the numbers that justified it.

Runs are addressed by *reference* rather than by id alone, because the useful
comparison is almost never "these two uuids": it is "this commit against the
one before it". ``latest~1`` is what a CI job actually wants to say.
"""

import uuid
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from eval.report import QuestionResult, RunReport

from app.models import EvalResult, EvalRun


class RunNotFound(LookupError):
    """A run reference matched nothing."""


async def save(session: AsyncSession, report: RunReport) -> uuid.UUID:
    """Write one run and its per-question rows. Returns the run id."""
    run = EvalRun(
        dataset_name=report.dataset_name,
        dataset_sha256=report.dataset_sha256,
        config=report.config,
        git_sha=report.git_sha,
        label=report.label,
        k=report.k,
        num_questions=report.num_questions,
        num_scored=report.num_scored,
        metrics=report.metrics,
        metrics_by_type=report.metrics_by_type,
        metrics_by_split=report.metrics_by_split,
    )
    session.add(run)
    await session.flush()
    session.add_all(
        [
            EvalResult(
                run_id=run.id,
                question_id=row.question_id,
                question_type=row.question_type,
                split=row.split,
                first_gold_rank=row.first_gold_rank,
                metrics=row.metrics,
                retrieved_chunk_ids=row.retrieved_chunk_ids,
                gold_chunk_ids=row.gold_chunk_ids,
            )
            for row in report.results
        ]
    )
    await session.commit()
    report.run_id = str(run.id)
    return run.id


def _to_report(run: EvalRun) -> RunReport:
    return RunReport(
        dataset_name=run.dataset_name,
        dataset_sha256=run.dataset_sha256,
        config=run.config,
        k=run.k,
        git_sha=run.git_sha,
        label=run.label,
        run_id=str(run.id),
        created_at=run.created_at.isoformat() if run.created_at else None,
        num_questions=run.num_questions,
        num_scored=run.num_scored,
        metrics=dict(run.metrics or {}),
        metrics_by_type=dict(run.metrics_by_type or {}),
        metrics_by_split=dict(run.metrics_by_split or {}),
        results=[
            QuestionResult(
                question_id=row.question_id,
                question_type=row.question_type,
                split=row.split,
                first_gold_rank=row.first_gold_rank,
                metrics=dict(row.metrics or {}),
                retrieved_chunk_ids=list(row.retrieved_chunk_ids or []),
                gold_chunk_ids=list(row.gold_chunk_ids or []),
            )
            for row in sorted(run.results, key=lambda r: r.question_id)
        ],
    )


def _base_query(dataset_name: str | None, config: str | None):
    stmt = select(EvalRun).options(selectinload(EvalRun.results))
    if dataset_name:
        stmt = stmt.where(EvalRun.dataset_name == dataset_name)
    if config:
        stmt = stmt.where(EvalRun.config == config)
    return stmt.order_by(EvalRun.created_at.desc(), EvalRun.id.desc())


async def recent(
    session: AsyncSession,
    *,
    dataset_name: str | None = None,
    config: str | None = None,
    limit: int = 20,
) -> list[RunReport]:
    rows = await session.execute(_base_query(dataset_name, config).limit(limit))
    return [_to_report(run) for run in rows.scalars().all()]


async def resolve(
    session: AsyncSession,
    ref: str,
    *,
    dataset_name: str | None = None,
    config: str | None = None,
) -> RunReport:
    """Turn a reference into a run.

    ``latest`` / ``latest~N``  N번째 최신 실행 (데이터셋·구성 필터 안에서)
    ``sha:<git sha>``          그 커밋의 가장 최근 실행
    ``<uuid 앞자리>``          id 접두사
    """
    ref = ref.strip()

    if ref == "latest" or ref.startswith("latest~"):
        offset = int(ref.split("~", 1)[1]) if "~" in ref else 0
        rows = await session.execute(
            _base_query(dataset_name, config).offset(offset).limit(1)
        )
        run = rows.scalars().first()
        if run is None:
            raise RunNotFound(
                f"{ref!r} 에 해당하는 실행이 없다 "
                f"(dataset={dataset_name}, config={config}). "
                "비교하려면 base 쪽 실행이 먼저 저장돼 있어야 한다."
            )
        return _to_report(run)

    if ref.startswith("sha:"):
        wanted = ref[4:]
        rows = await session.execute(
            _base_query(dataset_name, config).where(EvalRun.git_sha.like(f"{wanted}%"))
        )
        run = rows.scalars().first()
        if run is None:
            raise RunNotFound(f"커밋 {wanted!r} 로 저장된 실행이 없다")
        return _to_report(run)

    # 남은 형태는 실행 uuid 하나뿐이다. 접두사 매칭은 일부러 안 받는다 —
    # 두 실행에 걸리는 접두사를 조용히 하나로 고르면, 비교 대상이 사람이
    # 의도한 것과 달라도 리포트는 멀쩡해 보인다.
    rows = await session.execute(
        select(EvalRun)
        .options(selectinload(EvalRun.results))
        .where(EvalRun.id == _as_uuid(ref))
    )
    run = rows.scalars().first()
    if run is None:
        raise RunNotFound(f"실행 {ref!r} 을 찾지 못했다")
    return _to_report(run)


def _as_uuid(ref: str) -> uuid.UUID:
    try:
        return uuid.UUID(ref)
    except ValueError:
        raise RunNotFound(
            f"{ref!r} 은 실행 참조로 읽을 수 없다 — "
            "latest, latest~N, sha:<커밋>, 또는 실행 uuid 전체여야 한다."
        ) from None


def as_json(report: RunReport) -> dict:
    return asdict(report)
