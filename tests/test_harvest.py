"""Tests for failure harvesting (no infra).

The loop's value depends on two properties: it must catch real failures, and it
must not re-add cases the golden set already covers — otherwise the set grows
with duplicates and the signal decays.
"""

from eval import harvest


def _record(question, *, qtype="factual", refused=False, **scores):
    base = {
        "faithfulness": 1.0,
        "answer_relevancy": 1.0,
        "context_precision": 1.0,
        "context_recall": 1.0,
    }
    base.update(scores)
    return {
        "question": question,
        "type": qtype,
        "refused": refused,
        "answer": "…",
        "reference": "기준답변",
        "gold_pages": [1],
        "document": "lease",
        "retrieved_pages": [1, 2, 3],
        "scores": base,
    }


def test_passing_record_is_not_harvested():
    assert harvest.failures_from_scored([_record("모두 통과")]) == []


def test_threshold_breach_is_harvested_with_reason():
    found = harvest.failures_from_scored([_record("정밀도 낮음", context_precision=0.1)])
    assert len(found) == 1
    assert any("context_precision" in r for r in found[0]["reasons"])
    assert found[0]["diagnosis"]  # 진단이 붙는다


def test_wrong_refusal_is_harvested_even_when_metrics_pass():
    """A refused-but-answerable case can still score well; it must not slip through."""
    found = harvest.failures_from_scored([_record("잘못 거부", refused=True)])
    assert len(found) == 1
    assert "답변 가능한데 거부" in found[0]["reasons"]


def test_correct_refusal_is_not_harvested():
    record = _record("문서에 없음", qtype="unanswerable", refused=True)
    assert harvest.failures_from_scored([record]) == []


def test_none_scores_do_not_count_as_failures():
    """An unmeasured metric is not a zero — it must not fabricate a failure."""
    record = _record("측정 불가", faithfulness=None, context_precision=None)
    assert harvest.failures_from_scored([record]) == []


def test_dedupe_drops_known_and_repeated_questions():
    known = {harvest._normalize("이미 있는 질문")}
    candidates = [
        {"question": "이미 있는 질문"},
        {"question": "  이미   있는 질문  "},  # whitespace variant
        {"question": "새로운 질문"},
        {"question": "새로운 질문"},  # repeated within the batch
    ]
    fresh, dropped = harvest.dedupe(candidates, known)
    assert [c["question"] for c in fresh] == ["새로운 질문"]
    assert dropped == 3


def test_existing_questions_come_from_the_golden_set():
    known = harvest.existing_questions()
    assert harvest._normalize("임대차 보증금은 얼마인가요?") in known
    assert len(known) > 30


def test_snippet_marks_unlabelled_fields():
    snippet = harvest.render_snippet([{
        "question": "라벨 없는 질문",
        "document": "",
        "gold_pages": [],
        "reference": "",
        "type": "",
        "reasons": ["프로덕션 질의가 거부됨"],
        "diagnosis": "미판정",
    }])
    assert "TODO" in snippet
    assert "라벨 없는 질문" in snippet
    assert "왜:" in snippet
