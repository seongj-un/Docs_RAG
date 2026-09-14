"""The L2 judge's own guarantees, checked without ever calling a judge model.

Three things are on trial here, and none of them needs a GPU:

1. **Cohen's kappa arithmetic.** 사람 라벨이 아직 없어도 계산은 순수 함수라
   지금 검증할 수 있다. 라벨이 들어오는 순간 한 걸음이면 되게 하는 것이 목적이다.
2. **The rubric version contract.** 루브릭을 고치고 버전을 안 올린 상태에서
   비교가 통과하면 버전 태그는 아무것도 보증하지 않는다.
3. **The unvalidated mark.** 표식이 저장 레코드와 집계 키 이름에 실제로
   박혀 있는지. 이 테스트가 증인이다 — 표식이 리포트 문구로만 존재하면
   표를 복사하는 순간 사라진다.
"""

import json

import pytest

from eval import judge_local, l2, l2_run


# --- Cohen's kappa ---


def test_textbook_binary_case_matches_the_hand_computed_value():
    """Landis & Koch 류 2x2 예제: po=0.70, pe=0.50 이면 kappa=0.40."""
    # 둘 다 yes 20, 둘 다 no 15, A만 yes 5, B만 yes 10 (n=50).
    a = [1] * 20 + [0] * 15 + [1] * 5 + [0] * 10
    b = [1] * 20 + [0] * 15 + [0] * 5 + [1] * 10
    value = l2.cohens_kappa(a, b, weights="none", categories=(0, 1))
    assert value == pytest.approx(0.40)


def test_quadratic_weighting_forgives_adjacent_disagreement():
    """순서형이므로 한 칸 차이와 세 칸 차이가 같은 불일치일 수 없다.

    손계산: a=[0,1,2,3], b=[1,1,2,3] 에서
      가중 없음  1 - 0.25/0.75      = 0.6667
      이차 가중  1 - 0.027778/0.22222 = 0.8750
    """
    a, b = [0, 1, 2, 3], [1, 1, 2, 3]
    assert l2.cohens_kappa(a, b, weights="none") == pytest.approx(2 / 3)
    assert l2.cohens_kappa(a, b, weights="quadratic") == pytest.approx(0.875)
    linear = l2.cohens_kappa(a, b, weights="linear")
    # 선형은 둘 사이에 있어야 한다. 아니면 가중 함수가 거리를 잘못 쓰고 있다.
    assert 2 / 3 < linear < 0.875


def test_perfect_agreement_is_one_under_every_weighting():
    a = [0, 1, 2, 3, 3, 1]
    assert l2.kappa_all(a, list(a)) == {
        "none": pytest.approx(1.0),
        "linear": pytest.approx(1.0),
        "quadratic": pytest.approx(1.0),
    }


def test_systematic_opposition_is_negative():
    """우연보다 나쁜 일치는 음수로 나와야 한다. 0 으로 바닥을 치면 안 된다."""
    a = [0, 0, 3, 3]
    b = [3, 3, 0, 0]
    assert l2.cohens_kappa(a, b, weights="none") < 0


def test_a_lazy_judge_that_always_says_three_is_undefined_not_perfect():
    """모든 라벨이 한 범주면 kappa 는 0/0 이다.

    1.0 으로 접으면 "전부 3점을 주는 judge" 가 완벽한 일치도를 얻는다 — 이
    판정기가 막으려는 바로 그 결론이다.
    """
    with pytest.raises(l2.KappaUndefined):
        l2.cohens_kappa([3, 3, 3, 3], [3, 3, 3, 3])
    assert l2.kappa_all([3, 3, 3], [3, 3, 3]) == {
        "none": None,
        "linear": None,
        "quadratic": None,
    }


def test_malformed_label_input_is_refused():
    with pytest.raises(l2.KappaUndefined):
        l2.cohens_kappa([1, 2], [1])
    with pytest.raises(l2.KappaUndefined):
        l2.cohens_kappa([], [])
    with pytest.raises(l2.KappaUndefined):
        l2.cohens_kappa([0, 9], [0, 1])  # 척도 밖
    with pytest.raises(ValueError):
        l2.cohens_kappa([0, 1], [1, 0], weights="cubic")


def test_kappa_verdict_follows_the_w6_thresholds():
    assert "재작성" in l2.kappa_verdict(0.39)
    assert "보류" in l2.kappa_verdict(0.5)
    assert "채택" in l2.kappa_verdict(0.6)
    assert l2.kappa_verdict(None) == "측정 불가"


# --- 루브릭 버전 계약 ---


def _record(**over) -> l2.L2Record:
    base = dict(
        question_id="q1",
        question_type="single_fact",
        variant="server",
        rubric_version=l2.RUBRIC_VERSION,
        rubric_sha256=l2.rubric_sha256(),
        judge_provider="ollama",
        judge_model="qwen3:4b",
        verdicts=[
            l2.DimensionVerdict("groundedness", "[0] 에 같은 문장이 있다", 3),
            l2.DimensionVerdict("correctness", "금액이 일치한다", 3),
            l2.DimensionVerdict("refusal_accuracy", "거부하지 않고 답했다", 3),
        ],
    )
    base.update(over)
    return l2.L2Record(**base)


def test_the_prompt_and_the_hash_are_built_from_the_same_string():
    """프롬프트만 고치고 해시는 그대로인 상태가 생기면 안 된다."""
    text = l2.rubric_text()
    assert l2.RUBRIC_VERSION in text
    for dimension in l2.DIMENSIONS:
        assert dimension in text
        for anchor in l2.RUBRIC[dimension]["anchors"].values():
            assert anchor in text
    system = judge_local.SYSTEM_PROMPT.format(rubric=text)
    assert text in system
    assert l2.RUBRIC_VERSION in system


def test_editing_the_rubric_without_bumping_the_version_is_caught(monkeypatch):
    base = [_record()]
    original = dict(l2.RUBRIC["groundedness"])
    monkeypatch.setitem(
        l2.RUBRIC,
        "groundedness",
        {**original, "note": original["note"] + " (몰래 고친 문장)"},
    )
    head = [_record(rubric_sha256=l2.rubric_sha256())]

    assert base[0].rubric_version == head[0].rubric_version  # 사람은 못 알아챈다
    with pytest.raises(l2.IncomparableRubrics) as exc:
        l2.check_comparable(base, head)
    assert "RUBRIC_VERSION" in str(exc.value)


def test_a_different_rubric_version_refuses_comparison():
    # 현재 버전에서 파생시킨다. 리터럴을 적으면 다음 버전 올림에서 두 값이
    # 같아져 이 테스트가 조용히 무의미해진다 — 실제로 v1->v2 에서 겪었다.
    other = f"{l2.RUBRIC_VERSION}-other"
    with pytest.raises(l2.IncomparableRubrics):
        l2.check_comparable([_record()], [_record(rubric_version=other)])


def test_a_different_judge_model_refuses_comparison():
    """채점자가 바뀐 것을 품질 변화로 읽으면 안 된다."""
    with pytest.raises(l2.IncomparableRubrics):
        l2.check_comparable([_record()], [_record(judge_model="qwen3:8b")])


def test_the_same_rubric_compares_fine():
    l2.check_comparable([_record()], [_record(question_id="q2")])


# --- unvalidated 표식 ---


def test_a_fresh_record_is_unvalidated_and_says_why():
    record = _record()
    assert record.validation.status == l2.UNVALIDATED
    assert record.validation.kappa is None
    assert "kappa" in record.validation.why
    assert record.to_dict()["validation"]["status"] == l2.UNVALIDATED


def test_aggregate_keys_carry_the_unvalidated_mark():
    """수치를 'groundedness' 라고 부르는 문자열이 어디에도 없어야 한다."""
    agg = l2.aggregate([_record()])
    assert set(agg) == {f"{d}__unvalidated" for d in l2.DIMENSIONS}
    assert "groundedness" not in agg


def test_the_mark_disappears_only_once_kappa_exists():
    validated = l2.Validation(
        status=l2.VALIDATED, kappa=0.71, kappa_weights="quadratic", kappa_n=40
    )
    assert l2.metric_key("groundedness", validated) == "groundedness"
    assert l2.metric_key("groundedness", l2.Validation()) == "groundedness__unvalidated"
    assert set(l2.aggregate([_record(validation=validated)])) == set(l2.DIMENSIONS)


def test_records_round_trip_through_disk_with_the_mark_intact(tmp_path):
    path = tmp_path / "l2.jsonl"
    l2.write_records(path, [_record(), _record(question_id="q2")])
    raw = path.read_text(encoding="utf-8")
    assert raw.count(l2.UNVALIDATED) == 2  # 저장 레코드에도 박혀 있다

    back = l2.read_records(path)
    assert [r.question_id for r in back] == ["q1", "q2"]
    assert back[0].validation.status == l2.UNVALIDATED
    assert back[0].scores() == {
        "groundedness": 3,
        "correctness": 3,
        "refusal_accuracy": 3,
    }


def test_the_banner_names_the_recovery_procedure():
    assert "UNVALIDATED" in l2.BANNER
    assert "kappa" in l2.BANNER
    assert str(l2.KAPPA_ADOPT_AT) in l2.BANNER


# --- 해당 없음 ---


def test_no_answer_questions_do_not_get_a_correctness_score():
    """담아야 할 사실이 없는 문항에 0 을 주면 거부를 잘한 문항이 평균을 깎는다."""
    assert l2.applicable_dimensions("no_answer") == (
        "groundedness",
        "refusal_accuracy",
    )
    assert l2.applicable_dimensions("single_fact") == l2.DIMENSIONS


def test_missing_dimensions_leave_the_denominator_alone():
    answerable = _record()
    refusal = _record(
        question_id="q2",
        question_type="no_answer",
        verdicts=[
            l2.DimensionVerdict("groundedness", "컨텍스트가 비었고 답도 안 했다", 3),
            l2.DimensionVerdict("refusal_accuracy", "분명히 거부했다", 3),
        ],
    )
    agg = l2.aggregate([answerable, refusal])
    # correctness 는 한 문항에서만 재졌으므로 평균이 3.0 이어야 한다. 2.0 이면
    # 재지 않은 칸을 0 으로 접고 있다는 뜻이다.
    assert agg["correctness__unvalidated"] == pytest.approx(3.0)
    assert agg["groundedness__unvalidated"] == pytest.approx(3.0)


# --- 사람 라벨 조인 ---


def test_label_rows_are_joinable_on_question_dimension_and_rubric():
    rows = l2.label_rows([_record()])
    assert len(rows) == 3
    row = rows[0]
    assert row["question_id"] == "q1"
    assert row["dimension"] == "groundedness"
    assert row["rubric_version"] == l2.RUBRIC_VERSION
    assert row["rubric_sha256"] == l2.rubric_sha256()
    assert row["human_score"] is None  # 사람이 채울 칸


def test_unfilled_labels_are_skipped_not_counted_as_zero():
    records = [_record()]
    rows = l2.label_rows(records)
    rows[0]["human_score"] = 2  # 하나만 채웠다
    paired = l2.join_labels(records, rows)
    assert paired == {"groundedness": ([3], [2])}


def test_labels_written_against_another_rubric_are_refused():
    records = [_record()]
    rows = l2.label_rows(records)
    for row in rows:
        row["human_score"] = 3
        row["rubric_version"] = "l2-ko-v0"
    with pytest.raises(l2.LabelMismatch):
        l2.join_labels(records, rows)


def test_kappa_report_carries_the_rubric_and_the_verdict():
    records = [
        _record(question_id=f"q{i}", verdicts=[
            l2.DimensionVerdict("groundedness", "근거", score)
        ])
        for i, score in enumerate([3, 3, 2, 1, 0, 3, 2, 0])
    ]
    rows = l2.label_rows(records)
    for row, human in zip(rows, [3, 3, 2, 1, 0, 3, 2, 0]):
        row["human_score"] = human
    report = l2.kappa_report(records, rows)
    assert report["rubric_version"] == l2.RUBRIC_VERSION
    assert report["judge_model"] == "qwen3:4b"
    assert report["headline_kappa_quadratic"] == pytest.approx(1.0)
    assert "채택" in report["verdict"]
    assert report["per_dimension"]["groundedness"]["n"] == 8


# --- A/B 위치 편향 ---


def test_position_bias_is_named_rather_than_counted_as_a_win():
    """두 순서에서 같은 **자리**를 고르면 그건 승부가 아니다."""
    biased = l2.PairwiseOutcome("q1", "server", "client", "server", "client")
    assert biased.winner is None
    assert biased.position_biased

    real = l2.PairwiseOutcome("q2", "server", "client", "server", "server")
    assert real.winner == "server"
    assert not real.position_biased

    summary = l2.pairwise_summary([biased, real])
    assert summary == {
        "n": 2,
        "decided": 1,
        "wins": {"server": 1},
        "position_biased": 1,
        "position_bias_rate": pytest.approx(0.5),
    }


def test_order_positions_swaps_the_slots():
    assert l2.order_positions("server", "client", "ab") == ("server", "client")
    assert l2.order_positions("server", "client", "ba") == ("client", "server")
    with pytest.raises(ValueError):
        l2.order_positions("server", "client", "cd")


def test_the_pairwise_prompt_hides_which_system_wrote_which_answer():
    item = {"question": "보증금은?", "contexts": ["보증금 5천만원"], "type": "single_fact"}
    prompt = judge_local.build_pairwise_prompt(item, "답 A", "답 B")
    assert "first" in prompt and "second" in prompt
    assert "server" not in prompt and "client" not in prompt


# --- 판정 파싱 ---


def test_the_schema_puts_evidence_before_the_score():
    """근거 먼저·점수 나중을 프롬프트의 부탁이 아니라 문법으로 만든다."""
    schema = judge_local.response_schema(("groundedness",))
    assert schema["properties"]["groundedness"]["required"] == ["evidence", "score"]
    assert list(schema["properties"]["groundedness"]["properties"]) == [
        "evidence",
        "score",
    ]
    assert schema["properties"]["groundedness"]["properties"]["score"]["enum"] == [
        0, 1, 2, 3
    ]


def test_a_score_without_evidence_is_an_error_not_a_score():
    payload = {"groundedness": {"evidence": "  ", "score": 3}}
    with pytest.raises(ValueError, match="근거"):
        judge_local.parse_verdicts(payload, ("groundedness",))


def test_an_off_scale_score_is_an_error_not_a_silent_zero():
    with pytest.raises(ValueError, match="척도"):
        judge_local.parse_verdicts(
            {"groundedness": {"evidence": "근거", "score": 7}}, ("groundedness",)
        )
    with pytest.raises(ValueError):
        judge_local.parse_verdicts({}, ("groundedness",))


def test_the_user_prompt_carries_indexed_contexts_and_the_dimension_list():
    item = {
        "question": "보증금은?",
        "contexts": ["보증금 5천만원", "관리비 별도"],
        "answer": "5천만원이다",
        "reference": "5천만원",
        "type": "single_fact",
    }
    prompt = judge_local.build_user_prompt(item, l2.DIMENSIONS)
    assert "[0] 보증금 5천만원" in prompt
    assert "[1] 관리비 별도" in prompt
    for dimension in l2.DIMENSIONS:
        assert dimension in prompt


def test_a_no_answer_question_tells_the_judge_what_correct_looks_like():
    item = {"question": "대표 전화는?", "contexts": [], "answer": "02-1234-5678",
            "reference": "", "type": "no_answer"}
    dimensions = l2.applicable_dimensions("no_answer")
    prompt = judge_local.build_user_prompt(item, dimensions)
    assert "거부" in prompt
    assert "correctness" not in prompt


# --- CLI ---


def test_rubric_command_prints_the_version_hash_and_banner(capsys):
    assert l2_run.main(["rubric"]) == 0
    out = capsys.readouterr().out
    assert l2.RUBRIC_VERSION in out
    assert l2.rubric_sha256() in out
    assert "UNVALIDATED" in out


def test_compare_refuses_across_rubric_versions(tmp_path, capsys):
    base, head = tmp_path / "base.jsonl", tmp_path / "head.jsonl"
    l2.write_records(base, [_record()])
    l2.write_records(head, [_record(rubric_version=f"{l2.RUBRIC_VERSION}-other")])
    assert l2_run.main(["compare", "--base", str(base), "--head", str(head)]) == 2
    assert "비교 거부" in capsys.readouterr().err


def test_report_marks_every_number_unvalidated(tmp_path, capsys):
    path = tmp_path / "l2.jsonl"
    l2.write_records(path, [_record()])
    assert l2_run.main(["report", "--records", str(path)]) == 0
    out = capsys.readouterr().out
    assert "groundedness__unvalidated" in out
    assert "UNVALIDATED" in out


def test_kappa_below_the_adoption_line_exits_non_zero(tmp_path, capsys):
    """미달을 exit 0 으로 내보내면 CI 도 사람도 통과로 읽는다."""
    records = [
        _record(
            question_id=f"q{i}",
            verdicts=[l2.DimensionVerdict("groundedness", "근거", judge)],
        )
        for i, judge in enumerate([3, 3, 3, 0, 0, 1, 2, 3])
    ]
    rows = l2.label_rows(records)
    for row, human in zip(rows, [0, 1, 3, 3, 2, 0, 3, 0]):
        row["human_score"] = human

    rec_path, label_path = tmp_path / "r.jsonl", tmp_path / "l.jsonl"
    l2.write_records(rec_path, records)
    label_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )
    code = l2_run.main(
        ["kappa", "--records", str(rec_path), "--labels", str(label_path)]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "quadratic" in captured.out
    assert "unvalidated" in captured.err


def test_labels_command_spreads_the_sample_across_question_types(tmp_path, capsys):
    """전체 무작위로 뽑으면 no_answer 가 한 건도 안 걸려 거부 정확도를 못 잰다."""
    records = [
        _record(question_id=f"sf{i}", question_type="single_fact") for i in range(20)
    ] + [_record(question_id=f"na{i}", question_type="no_answer") for i in range(4)]
    rec_path, out_path = tmp_path / "r.jsonl", tmp_path / "labels.jsonl"
    l2.write_records(rec_path, records)

    assert l2_run.main(
        ["labels", "--records", str(rec_path), "--out", str(out_path), "--sample", "8"]
    ) == 0
    rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
    picked = {r["question_id"] for r in rows}
    assert sum(1 for q in picked if q.startswith("na")) == 4
    assert all(r["human_score"] is None for r in rows)
    assert "human_score" in capsys.readouterr().out
