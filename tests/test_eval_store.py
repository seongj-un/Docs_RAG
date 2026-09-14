"""M7 W3 proofs: an eval run survives a round trip through Postgres.

Requires Postgres (migration 0011). No embedding or rerank server is involved —
what is under test is the storage and the reference resolution that the diff
report stands on, not the retrieval quality that fills it.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import delete, text

from app.db import SessionLocal, engine
from app.models import EvalRun
from eval import harness, report as report_lib, store


def run_async(coro_fn):
    async def wrapper():
        try:
            return await coro_fn()
        finally:
            await engine.dispose()

    return asyncio.run(wrapper())


def _db_available() -> bool:
    async def check():
        try:
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.run(check())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


@pytest.fixture
def dataset_name():
    """A name nobody else uses, swept afterwards so runs do not pile up."""
    name = f"pytest-{uuid.uuid4().hex[:8]}"
    yield name

    async def sweep():
        async with SessionLocal() as session:
            await session.execute(delete(EvalRun).where(EvalRun.dataset_name == name))
            await session.commit()

    run_async(sweep)


def make_report(dataset_name: str, *, sha: str, scores: dict[str, float]):
    results = [
        report_lib.QuestionResult(
            question_id=qid,
            question_type="single_fact",
            split="tune",
            first_gold_rank=1 if score else None,
            metrics={"R@1": score, "R@10": score, "MRR": score},
            retrieved_chunk_ids=[str(uuid.uuid4())],
            gold_chunk_ids=[str(uuid.uuid4())],
        )
        for qid, score in scores.items()
    ]
    return report_lib.RunReport(
        dataset_name=dataset_name,
        dataset_sha256="f" * 64,
        config="hybrid+rerank",
        k=10,
        git_sha=sha,
        label="pytest",
        num_questions=len(results),
        num_scored=len(results),
        metrics=report_lib.aggregate(results, ["R@1", "R@10", "MRR"]),
        metrics_by_type=report_lib.group_by(results, "question_type", ["R@1"]),
        metrics_by_split=report_lib.group_by(results, "split", ["R@1"]),
        results=results,
    )


def test_run_round_trips(dataset_name):
    original = make_report(dataset_name, sha="abc1234", scores={"q1": 1.0, "q2": 0.0})

    async def go():
        async with SessionLocal() as session:
            run_id = await store.save(session, original)
            loaded = await store.resolve(session, str(run_id))
            return run_id, loaded

    run_id, loaded = run_async(go)

    assert loaded.run_id == str(run_id)
    assert loaded.dataset_name == dataset_name
    assert loaded.git_sha == "abc1234"
    assert loaded.metrics == original.metrics
    assert loaded.created_at is not None
    assert [r.question_id for r in loaded.results] == ["q1", "q2"]
    assert loaded.results[0].first_gold_rank == 1
    # "못 찾음"은 0위가 아니라 NULL 로 돌아와야 한다.
    assert loaded.results[1].first_gold_rank is None


def test_latest_references_walk_backwards(dataset_name):
    async def go():
        async with SessionLocal() as session:
            await store.save(session, make_report(dataset_name, sha="old", scores={"q1": 1.0}))
            await store.save(session, make_report(dataset_name, sha="new", scores={"q1": 0.0}))
            latest = await store.resolve(session, "latest", dataset_name=dataset_name)
            previous = await store.resolve(session, "latest~1", dataset_name=dataset_name)
            by_sha = await store.resolve(session, "sha:old", dataset_name=dataset_name)
            listed = await store.recent(session, dataset_name=dataset_name)
            return latest, previous, by_sha, listed

    latest, previous, by_sha, listed = run_async(go)

    assert latest.git_sha == "new"
    assert previous.git_sha == "old"
    assert by_sha.git_sha == "old"
    assert [r.git_sha for r in listed] == ["new", "old"]


def test_unknown_reference_is_an_error(dataset_name):
    async def go():
        async with SessionLocal() as session:
            with pytest.raises(store.RunNotFound):
                await store.resolve(session, "latest", dataset_name=dataset_name)
            with pytest.raises(store.RunNotFound, match="실행 참조로 읽을 수 없다"):
                await store.resolve(session, "머리로 지어낸 참조")
            with pytest.raises(store.RunNotFound):
                await store.resolve(session, str(uuid.uuid4()))

    run_async(go)


def test_diff_cli_reads_stored_runs_and_fails_on_regression(dataset_name, capsys):
    async def go():
        async with SessionLocal() as session:
            await store.save(
                session, make_report(dataset_name, sha="before", scores={"q1": 1.0, "q2": 1.0})
            )
            await store.save(
                session, make_report(dataset_name, sha="after", scores={"q1": 1.0, "q2": 0.0})
            )

    run_async(go)

    code = harness.main(
        ["diff", "--base", "latest~1", "--head", "latest", "--dataset", f"{dataset_name}.jsonl"]
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "q2" in out
    assert "q1" not in out.split("뒤집힌 문항")[1]
