"""M7 W5 proofs: the cumulative A/B ladder runs and records what W8 will need.

Requires Postgres (migration 0013). 임베딩·리랭커는 스텁이다 — CI 에 모델
서버가 없고, 여기서 증명할 것은 검색 품질이 아니라 **배관**이다: 사다리가
칸마다 청킹을 실제로 바꿔 다시 인덱싱하는가, 청킹이 같은 칸은 인덱스를
재사용하는가, 그리고 각 칸이 지표와 **함께** 조건·비용을 남기는가.

품질을 스텁으로 재는 것은 의미가 없다. 그래서 점수는 검사하지 않는다.
하지만 청킹은 진짜고, pgvector 조회도 진짜고, gold 스팬 해석도 진짜다 —
그 셋이 W5 에서 실제로 바뀌는 부분이다.
"""

import asyncio
import json
import uuid
from argparse import Namespace

import pytest
from sqlalchemy import delete, select, text

from app.config import settings
from app.constants import EVAL_USER_ID
from app.db import SessionLocal, engine
from app.models import Document, EvalRun, User
from eval import harness, store

from tests.test_eval_harness_run import _db_available, run_async, stub_models  # noqa: F401

pytestmark = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")

PROVIDER = "tests.spec_docs_fixture"


@pytest.fixture
def dataset_path(tmp_path):
    """Two questions over a two-page, heading-rich fixture document."""
    name = f"pytest-ab-{uuid.uuid4().hex[:8]}"
    rows = [
        {
            "id": "a1",
            "question": "학생 목록 조회의 오류 코드는?",
            "type": "single_fact",
            "gold_spans": [{"doc": "spec", "snippet": "| E4012 | 403 | 다른 학과를 지정했다 |"}],
            "reference_answer": "E4012 다.",
            "difficulty": "easy",
            "source": "pytest",
            "split": "tune",
            "verified_at": "2026-09-14",
        },
        {
            "id": "a2",
            "question": "입학 연도를 담는 필드 이름은?",
            "type": "single_fact",
            "gold_spans": [
                {"doc": "spec", "snippet": "| enrolled_year | integer | 입학 연도를 담는다 |"}
            ],
            "reference_answer": "enrolled_year 다.",
            "difficulty": "easy",
            "source": "pytest",
            "split": "tune",
            "verified_at": "2026-09-14",
        },
    ]
    path = tmp_path / f"{name}.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    yield path

    async def sweep():
        async with SessionLocal() as session:
            await session.execute(delete(EvalRun).where(EvalRun.dataset_name == name))
            docs = (
                (await session.execute(
                    select(Document).where(Document.user_id == EVAL_USER_ID)
                )).scalars().all()
            )
            for doc in docs:
                await session.delete(doc)
            user = await session.get(User, EVAL_USER_ID)
            if user is not None:
                await session.delete(user)
            await session.commit()

    run_async(sweep)


def _args(dataset_path, **overrides):
    base = dict(
        dataset=str(dataset_path), k=5, split="tune", provider=PROVIDER,
        only=None, no_store=False, json_out=None, verbose=False,
    )
    base.update(overrides)
    return Namespace(command="ab", **base)


def test_the_ladder_runs_every_rung_and_records_its_conditions(
    stub_models, dataset_path, capsys
):
    assert asyncio.run(harness.cmd_ab(_args(dataset_path))) == 0

    async def read_back():
        async with SessionLocal() as session:
            return await store.recent(
                session, dataset_name=dataset_path.stem, limit=20
            )

    runs = {r.label: r for r in run_async(read_back)}
    assert set(runs) == {rung[0] for rung in harness.AB_LADDER}

    for name, strategy, prefix, config in harness.AB_LADDER:
        run = runs[name]
        # 조건이 지표와 같은 행에 있어야 두 숫자를 나란히 놓을 수 있다.
        assert run.config == config
        assert run.chunk_strategy == strategy
        assert run.heading_prefix is prefix
        # Notion W5 "같이 기록할 것": 인덱스 크기와 질의당 지연.
        assert run.index_chunks and run.index_chunks > 0
        assert run.index_bytes and run.index_bytes > 0
        assert run.latency_ms_p50 is not None
        assert run.latency_ms_mean is not None
        # precision 이 없으면 Notion 함정 1번을 확인할 방법이 없다.
        assert "P@5" in run.metrics


def test_rungs_that_share_a_chunking_reuse_the_index(stub_models, dataset_path):
    """A2·A3·A4 는 질의만 바뀐다. 다시 인덱싱하면 같은 인덱스가 다르게 보인다."""
    assert asyncio.run(harness.cmd_ab(_args(dataset_path))) == 0

    async def read_back():
        async with SessionLocal() as session:
            return await store.recent(session, dataset_name=dataset_path.stem, limit=20)

    runs = {r.label: r for r in run_async(read_back)}

    # 청킹이 바뀐 칸만 재인덱싱 시간이 있다.
    assert runs["A0-baseline"].reindex_seconds is not None
    assert runs["A1-section"].reindex_seconds is not None
    assert runs["A2-heading-prefix"].reindex_seconds is not None
    assert runs["A3-hybrid"].reindex_seconds is None
    assert runs["A4-rerank"].reindex_seconds is None
    # 재사용한 칸은 인덱스도 같아야 한다.
    assert runs["A3-hybrid"].index_chunks == runs["A2-heading-prefix"].index_chunks


def test_section_chunking_changes_the_index(stub_models, dataset_path):
    """사다리가 청킹을 정말로 바꾸는가 — 안 바뀌면 A1 칸은 아무 실험도 아니다."""
    assert asyncio.run(harness.cmd_ab(_args(dataset_path))) == 0

    async def read_back():
        async with SessionLocal() as session:
            return await store.recent(session, dataset_name=dataset_path.stem, limit=20)

    runs = {r.label: r for r in run_async(read_back)}
    # 청크 **개수**는 우연히 같을 수 있다. 본문 바이트는 다르다 — 고정 창은
    # CHUNK_OVERLAP 만큼 같은 글자를 두 번 싣고, 섹션 청킹은 싣지 않는다.
    assert runs["A0-baseline"].index_bytes != runs["A1-section"].index_bytes


def test_the_ladder_restores_the_process_settings(stub_models, dataset_path):
    """설정을 되돌리지 않으면 같은 프로세스의 다음 인덱싱이 조용히 달라진다."""
    before = (settings.chunk_strategy, settings.chunk_heading_prefix)
    asyncio.run(harness.cmd_ab(_args(dataset_path, no_store=True)))
    assert (settings.chunk_strategy, settings.chunk_heading_prefix) == before


def test_only_runs_the_named_rungs(stub_models, dataset_path):
    args = _args(dataset_path, only=["A0-baseline", "A4-rerank"], no_store=True)
    assert asyncio.run(harness.cmd_ab(args)) == 0
