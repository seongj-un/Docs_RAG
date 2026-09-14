"""Tests for the M7 jsonl golden-set format (no infra required).

The committed synthetic datasets are checked here too, not only by
``eval.harness validate``: a dataset that no longer parses is a broken build,
and it should break at ``pytest`` rather than at the first eval run of the week.
"""

import json
from pathlib import Path

import pytest

from eval import datasets
from eval.datasets import synthetic

DATASET_DIR = Path(__file__).resolve().parent.parent / "eval" / "datasets"
COMMITTED = sorted(DATASET_DIR.glob("*.jsonl"))

VALID = {
    "id": "q001",
    "question": "보증금은 얼마인가요?",
    "type": "single_fact",
    "gold_spans": [{"doc": "lease", "snippet": "임대차 보증금은 금 오천만원으로 한다"}],
    "reference_answer": "오천만원이다.",
    "difficulty": "easy",
    "source": "테스트",
    "split": "tune",
    "verified_at": "2026-09-14",
}


def write(tmp_path: Path, *rows: dict) -> Path:
    path = tmp_path / "set.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def test_loads_a_valid_row(tmp_path):
    dataset = datasets.load(write(tmp_path, VALID))

    assert len(dataset) == 1
    assert dataset.name == "set"
    assert dataset.questions[0].type == "single_fact"
    assert dataset.docs == ["lease"]
    assert len(dataset.sha256) == 64


def test_sha256_follows_content(tmp_path):
    first = datasets.load(write(tmp_path, VALID)).sha256
    second = datasets.load(write(tmp_path, {**VALID, "question": "달라진 질문"})).sha256
    assert first != second


def test_legacy_type_names_fold_into_the_w1_taxonomy(tmp_path):
    row = {**VALID, "type": "unanswerable", "gold_spans": []}
    assert datasets.load(write(tmp_path, row)).questions[0].type == "no_answer"


@pytest.mark.parametrize(
    "patch, expected",
    [
        ({"type": "trivia"}, "알 수 없는 유형"),
        ({"split": "train"}, "알 수 없는 split"),
        ({"difficulty": "impossible"}, "알 수 없는 난이도"),
        ({"gold_spans": []}, "gold_spans 가 비어 있다"),
        ({"gold_spans": [{"doc": "lease", "snippet": "짧다"}]}, "스니펫 길이"),
        ({"question": "  "}, "question 이 비어 있다"),
    ],
)
def test_rejects_malformed_rows(tmp_path, patch, expected):
    with pytest.raises(datasets.DatasetError, match=expected):
        datasets.load(write(tmp_path, {**VALID, **patch}))


def test_no_answer_must_not_carry_gold_spans(tmp_path):
    row = {**VALID, "type": "no_answer"}
    with pytest.raises(datasets.DatasetError, match="gold_spans 가 없어야"):
        datasets.load(write(tmp_path, row))


def test_missing_field_is_named(tmp_path):
    row = {key: value for key, value in VALID.items() if key != "reference_answer"}
    with pytest.raises(datasets.DatasetError, match="reference_answer"):
        datasets.load(write(tmp_path, row))


def test_duplicate_ids_are_rejected(tmp_path):
    with pytest.raises(datasets.DatasetError, match="중복"):
        datasets.load(write(tmp_path, VALID, VALID))


def test_empty_file_is_rejected(tmp_path):
    path = tmp_path / "set.jsonl"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(datasets.DatasetError, match="문항이 하나도 없다"):
        datasets.load(path)


def test_dumps_round_trips(tmp_path):
    original = datasets.load(write(tmp_path, VALID)).questions[0]
    reparsed = datasets.load(write(tmp_path, json.loads(datasets.dumps(original))))
    assert reparsed.questions[0] == original


# --- the datasets this repository ships ---


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: p.stem)
def test_committed_dataset_parses(path):
    dataset = datasets.load(path)

    assert len(dataset) > 0
    assert all(q.split in datasets.SPLITS for q in dataset.questions)
    # no_answer 문항만 gold 없이 살아남는다. 나머지가 비면 영원히 0점이다.
    for question in dataset.questions:
        assert bool(question.gold_spans) == question.scored_in_l1


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: p.stem)
def test_committed_dataset_points_at_known_documents(path):
    dataset = datasets.load(path)
    assert set(dataset.docs) <= set(synthetic.names())


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: p.stem)
def test_committed_dataset_keeps_the_holdout_ratio(path):
    dataset = datasets.load(path)
    splits = dataset.split_counts()
    share = splits["holdout"] / len(dataset)
    # W1: holdout 30%. 문항 수가 적어 정확히 0.3 은 못 맞추므로 폭을 준다.
    assert 0.25 <= share <= 0.35, f"holdout 비율 {share:.0%}"
