"""M7 W3 proofs: one command turns a jsonl dataset into stored L1 numbers.

Requires Postgres. The embedding and rerank servers are stubbed — CI has no
model server, and what has to be proved here is the wiring, not the retrieval
quality: that the dataset is indexed through the *real* ingest path, that gold
snippets resolve against the chunks that path actually produced, that every
question is scored, and that the run lands in ``eval_runs``/``eval_results``
where the next commit's diff can find it.

품질을 스텁으로 재는 것은 의미가 없다. 그래서 점수는 검사하지 않고 배관만
검사한다 — 하지만 배관은 **진짜** 청킹과 **진짜** pgvector 조회를 지난다.
"""

import asyncio
import hashlib
import json
import uuid
from argparse import Namespace

import pytest
from pgvector import SparseVector
from sqlalchemy import delete, select, text

from app.config import settings
from app.constants import EVAL_USER_ID
from app.db import SessionLocal, engine
from app.models import Document, EvalRun, User
from app.services import embeddings, rerank
from eval import datasets, gold, harness, store


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

PROVIDER = "tests.eval_docs_fixture"


# --- stubs standing in for the model servers ---


def _dense(textual: str) -> list[float]:
    """Bag-of-characters vector: deterministic, and similar text lands close.

    무작위 벡터를 쓰면 검색이 우연에 좌우돼 테스트가 가끔 깨진다. 글자 분포로
    만들면 같은 조항을 가리키는 질의가 그 조항 근처에 떨어져, 배관이 잘못됐을
    때와 모델이 없을 때가 구분된다.
    """
    vector = [0.0] * settings.embed_dim
    for token in textual.split():
        index = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % settings.embed_dim
        vector[index] += 1.0
    if not any(vector):
        vector[0] = 1.0
    return vector


def _sparse(textual: str) -> SparseVector:
    mapping = {
        int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % settings.embed_sparse_dim: 1.0
        for token in textual.split()
    } or {0: 1.0}
    return SparseVector(mapping, settings.embed_sparse_dim)


@pytest.fixture
def stub_models(monkeypatch):
    async def embed_texts(texts):
        return [_dense(t) for t in texts]

    async def embed_full(texts):
        return [_dense(t) for t in texts], [_sparse(t) for t in texts]

    async def embed_query(one):
        return _dense(one)

    async def embed_query_full(one):
        return _dense(one), _sparse(one)

    async def fake_rerank(query, texts):
        # 순서를 유지한다 — 리랭커가 없는 환경에서 점수를 지어내지 않는다.
        return [(i, 1.0 - i * 0.01) for i in range(len(texts))]

    for module in (embeddings, harness.embeddings):
        monkeypatch.setattr(module, "embed_texts", embed_texts, raising=False)
        monkeypatch.setattr(module, "embed_full", embed_full, raising=False)
        monkeypatch.setattr(module, "embed_query", embed_query, raising=False)
        monkeypatch.setattr(module, "embed_query_full", embed_query_full, raising=False)
    monkeypatch.setattr(rerank, "rerank", fake_rerank)
    monkeypatch.setattr(harness.rerank, "rerank", fake_rerank)


@pytest.fixture
def dataset_path(tmp_path):
    """A three-question dataset over the fixture document."""
    name = f"pytest-{uuid.uuid4().hex[:8]}"
    rows = [
        {
            "id": "t1", "question": "보증금은 얼마인가요?", "type": "single_fact",
            "gold_spans": [{"doc": "manual", "snippet": "임대차 보증금은 금 오천만원으로 한다."}],
            "reference_answer": "오천만원이다.", "difficulty": "easy",
            "source": "pytest", "split": "tune", "verified_at": "2026-09-14",
        },
        {
            "id": "t2", "question": "보증금과 월세를 합치면?", "type": "multi_doc",
            "gold_spans": [
                {"doc": "manual", "snippet": "임대차 보증금은 금 오천만원으로 한다."},
                {"doc": "manual", "snippet": "월 차임은 금 삼백만원이며 매월 5일에 지급한다."},
            ],
            "reference_answer": "오천삼백만원이다.", "difficulty": "hard",
            "source": "pytest", "split": "tune", "verified_at": "2026-09-14",
        },
        {
            "id": "t3", "question": "주차는 몇 대인가요?", "type": "no_answer",
            "gold_spans": [], "reference_answer": "문서에 없다.", "difficulty": "medium",
            "source": "pytest", "split": "tune", "verified_at": "2026-09-14",
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
        dataset=str(dataset_path), config=["hybrid+rerank"], k=5, split="tune",
        provider=PROVIDER, label="pytest", no_store=False, no_reindex=False,
        json_out=None, verbose=False,
    )
    base.update(overrides)
    return Namespace(command="run", **base)


def test_run_indexes_scores_and_stores(stub_models, dataset_path, capsys):
    dataset = datasets.load(dataset_path)

    assert asyncio.run(harness.cmd_run(_args(dataset_path))) == 0

    async def read_back():
        async with SessionLocal() as session:
            return await store.resolve(
                session, "latest", dataset_name=dataset.name, config="hybrid+rerank"
            )

    saved = run_async(read_back)

    assert saved.num_questions == 3
    # no_answer 문항은 L1 에서 빠진다 — 3문항 중 2문항만 채점된다.
    assert saved.num_scored == 2
    assert {r.question_id for r in saved.results} == {"t1", "t2"}
    assert saved.git_sha
    assert set(saved.metrics) >= {"R@1", "R@5", "MRR", "nDCG@5"}
    assert "multi_doc" in saved.metrics_by_type

    # 스니펫이 실제 인덱싱 결과의 청크로 풀렸다는 증거. 여기가 비면 지표는
    # 전부 0이 되면서도 아무것도 고장 나 보이지 않는다.
    by_id = {r.question_id: r for r in saved.results}
    assert by_id["t1"].gold_chunk_ids
    assert len(by_id["t2"].gold_chunk_ids) >= 2
    assert by_id["t1"].retrieved_chunk_ids


def test_no_store_leaves_the_database_alone(stub_models, dataset_path):
    dataset = datasets.load(dataset_path)

    assert asyncio.run(harness.cmd_run(_args(dataset_path, no_store=True))) == 0

    async def count():
        async with SessionLocal() as session:
            runs = await store.recent(session, dataset_name=dataset.name)
            return len(runs)

    assert run_async(count) == 0


def test_eval_corpus_is_exactly_the_dataset(stub_models, dataset_path):
    """The eval account must own the dataset's documents and nothing else.

    검색이 코퍼스 전체를 훑으므로, 남의 문서가 한 장이라도 섞이면 지표가
    그 문서에 좌우된다.
    """
    asyncio.run(harness.cmd_run(_args(dataset_path, no_store=True)))

    async def owned():
        async with SessionLocal() as session:
            rows = (
                (await session.execute(
                    select(Document).where(Document.user_id == EVAL_USER_ID)
                )).scalars().all()
            )
            return sorted(doc.filename for doc in rows)

    dataset = datasets.load(dataset_path)
    assert run_async(owned) == [f"m7_{dataset.name}_manual.pdf"]


def test_a_missing_snippet_stops_the_run(stub_models, dataset_path, tmp_path):
    """문서가 바뀌어 스니펫이 사라지면 0점이 아니라 예외여야 한다."""
    broken = tmp_path / "broken.jsonl"
    broken.write_text(
        json.dumps(
            {
                "id": "t1", "question": "없는 것을 묻는다", "type": "single_fact",
                "gold_spans": [
                    {"doc": "manual", "snippet": "이 문장은 이 문서 어디에도 적혀 있지 않은 내용이다."}
                ],
                "reference_answer": "-", "difficulty": "easy", "source": "pytest",
                "split": "tune", "verified_at": "2026-09-14",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    async def go():
        # 실패하는 실행이라 cmd_run 이 끝까지 가지 못한다 — 엔진을 여기서
        # 닫지 않으면 다음 루프가 이전 루프의 커넥션을 잡고 터진다.
        try:
            await harness.cmd_run(_args(broken))
        finally:
            await engine.dispose()

    with pytest.raises(gold.GoldResolutionError, match="사라졌다"):
        asyncio.run(go())
