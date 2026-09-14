"""The W6 comparison rules: citations, refusal, cost, latency, position bias.

``eval/w6.py`` calls no model, so every judgement it makes can be pinned here
without a GPU, a key, or a database — the same split ``tests/test_eval_l2.py``
makes for the rubric.

What is worth pinning is the *fairness* of the comparison, because that is where
a generation-site experiment goes wrong quietly. Three rules carry it:

* the same page extractor and the same refusal detector run on both modes;
* each mode is scored against the evidence **its own generator saw**, so the
  mode that was handed more passages is not penalised for having more to cite;
* structural availability of citations (A has them, B cannot) is reported as a
  property and never enters a score column, because it is a restatement of the
  two architectures rather than a measurement of them.

The detector is the load-bearing piece of the refusal axis — the axis W6 names
as the decision criterion — so its failure modes are written down as tests
rather than as prose: a corpus full of "권한이 없으면 E4012" must not read as a
corpus full of refusals.
"""

import pytest

from eval import l2, w6


# --- 페이지 인용 추출 ------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("답은 [p.3] 에 있다.", {3}),
        ("근거: p.12 와 p. 14", {12, 14}),
        ("자세한 것은 [p.3-5] 참고", {3, 4, 5}),
        ("12쪽에 나온다", {12}),
        ("3~4쪽 참고", {3, 4}),
        ("7 페이지", {7}),
        ("see page 9", {9}),
        ("pages 2-3", {2, 3}),
    ],
)
def test_the_extractor_reads_the_page_forms_both_modes_actually_write(text, expected):
    """A 는 `[p.N]` 를 쓰도록 프롬프트가 시키고, B 는 자기 말로 쓴다.

    한쪽 표기만 읽는 추출기는 그 한쪽의 습관을 점수로 바꾼다. 그래서 같은
    추출기가 두 모드 모두에서 나올 법한 표기를 전부 읽어야 한다.
    """
    assert w6.extract_pages(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "권한이 없으면 E4012 가 내려간다.",
        "HTTP 상태는 403 이고 분당 한도는 600 이다.",
        "student_id 는 필수이며 최대 100 건까지 조회된다.",
    ],
)
def test_bare_numbers_are_never_read_as_citations(text):
    """이 코퍼스는 오류 코드·상태 코드·한도로 가득하다.

    맨 숫자를 페이지로 읽으면 '인용을 아주 많이 한' 답변이 대량으로 생기고,
    귀속률은 그 가짜 인용들에 의해 결정된다.
    """
    assert w6.extract_pages(text) == set()


def test_an_absurd_range_is_not_read_as_citing_the_whole_document():
    """`p.1-400` 하나가 문서 전체 인용이 되면 귀속률이 의미를 잃는다."""
    assert w6.extract_pages("근거는 p.1-400 전체다") == {1}
    # 뒤집힌 범위도 시작 쪽만 인정한다.
    assert w6.extract_pages("p.9-3") == {9}


def test_page_spans_expand_the_same_way_for_hits_and_citations():
    """검색 히트와 인용이 같은 두 필드를 쓰므로 같은 함수로 편다."""
    assert w6.pages_of([{"page_from": 2, "page_to": 4}]) == {2, 3, 4}
    assert w6.pages_of([{"page_from": 5, "page_to": None}]) == {5}
    assert w6.pages_of([{"page_from": None, "page_to": 3}]) == set()


def test_gold_pages_come_from_the_corpus_body_not_from_a_page_label():
    """골든셋은 페이지가 아니라 스니펫을 적는다 — 본문에서 되짚는다."""
    documents = {
        "spec_api": ["1쪽 본문", "| E4012 | 403 | 호출자의 권한", "3쪽 본문"],
        "spec_errors": ["E4012 가 나면 토큰을 다시 발급한다"],
    }
    spans = [{"doc": "spec_api", "snippet": "| E4012 | 403 | 호출자의"}]
    assert w6.resolve_gold_pages(spans, documents) == {2}

    # 여러 쪽에 걸린 스니펫은 전부 정답이다 — 어느 쪽을 인용해도 맞기 때문에,
    # 하나를 임의로 고르면 맞은 답을 틀렸다고 세게 된다.
    everywhere = [{"doc": "spec_api", "snippet": "본문"}]
    assert w6.resolve_gold_pages(everywhere, documents) == {1, 3}

    # 모르는 문서·빈 스니펫은 조용히 아무 쪽도 만들지 않는다.
    assert w6.resolve_gold_pages([{"doc": "없음", "snippet": "x"}], documents) == set()


# --- 인용 채점의 공정성 ----------------------------------------------------


def test_each_mode_is_scored_against_what_its_own_generator_saw():
    """같은 답변이라도 본 것이 다르면 귀속률이 다르다 — 그것이 옳다.

    모드 A 의 생성기가 본 것은 접지된 청크(=citations)이고, 모드 B 의
    생성기가 본 것은 검색 히트 전부다. 공통 분모를 하나로 잡으면 더 많은
    청크를 받은 쪽이 '본 적 없는 페이지를 인용했다'로 몰린다.
    """
    answer = "답은 [p.3] 과 [p.9] 에 있다."
    server = w6.score_citations(
        answer, seen_pages={3}, gold_pages={3}, refused=False
    )
    client = w6.score_citations(
        answer, seen_pages={3, 9}, gold_pages={3}, refused=False
    )
    assert server.attributable == 0.5  # 9 쪽은 본 적이 없다 = 지어낸 인용
    assert client.attributable == 1.0
    assert server.gold_hit is True and client.gold_hit is True


def test_a_refusal_is_not_scored_for_citations():
    """거부는 인용하지 않는 것이 정답이다 — 0 점이 아니라 해당 없음.

    0 으로 접으면 '거부를 잘한 모드'가 인용 점수로 벌을 받고, 그 벌은
    W6 의 결정 기준(거부 정확도)과 정확히 반대 방향으로 작용한다.
    """
    score = w6.score_citations(
        "제공된 문서에서 찾을 수 없습니다.",
        seen_pages={1, 2},
        gold_pages=set(),
        refused=True,
    )
    assert score.scored is False
    assert score.has_citation is None
    assert score.attributable is None
    assert score.gold_hit is None


def test_an_answer_with_no_citation_counts_in_the_rate_but_not_in_attribution():
    """인용을 안 한 것과 틀리게 한 것은 다른 실패다.

    두 실패를 한 칸에 합치면 '아무 인용도 안 하는' 모드가 귀속률 만점을
    받는다 — Notion 이 B 에 대해 경고한 바로 그 모양이다.
    """
    score = w6.score_citations(
        "학생 목록 조회는 E4012 를 낸다.", seen_pages={1}, gold_pages={1}, refused=False
    )
    assert score.has_citation is False
    assert score.attributable is None
    rows = [{"citation": score.to_dict()}]
    metrics = w6.citation_metrics(rows)
    assert metrics["scored_n"] == 1
    assert metrics["cited_n"] == 0
    assert metrics["citation_rate"] == 0.0
    assert metrics["attributable"] is None


def test_gold_hit_is_not_scored_when_the_item_has_no_gold_page():
    """no_answer 문항에는 맞출 페이지가 없다 — 0 이 아니라 없음."""
    score = w6.score_citations(
        "답은 [p.2] 다.", seen_pages={2}, gold_pages=set(), refused=False
    )
    assert score.gold_hit is None
    metrics = w6.citation_metrics([{"citation": score.to_dict()}])
    assert metrics["gold_n"] == 0
    assert metrics["gold_hit_rate"] is None


def test_structured_citations_are_a_property_and_never_a_score():
    """A=100%·B=0% 짜리 칸은 측정이 아니라 정의를 다시 쓴 것이다."""
    assert w6.STRUCTURED_CITATIONS == {
        w6.MODE_SERVER: True,
        w6.MODE_CLIENT: False,
    }
    # 점수 집계에는 그 이름이 아예 나타나지 않는다.
    metrics = w6.citation_metrics(
        [{"citation": w6.score_citations("[p.1]", seen_pages={1},
                                         gold_pages={1}, refused=False).to_dict()}]
    )
    assert "structured" not in " ".join(metrics)


# --- 거부 탐지 -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "제공된 문서에서 찾을 수 없습니다.",
        "문서에 해당 내용이 없습니다.",
        "업로드된 자료에는 그 정보가 없어 답변할 수 없습니다.",
        "검색된 컨텍스트에 관련 내용이 언급되어 있지 않습니다.",
        "The provided documents do not contain that information.",
    ],
)
def test_refusals_are_detected_in_both_modes_wording(text):
    assert w6.looks_refused(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "학생 목록 조회에서 권한이 없으면 E4012 가 내려갑니다 [p.2].",
        "문서 [p.3] 에 따르면 페이지 크기 상한은 100 입니다.",
        "해당 필드는 필수가 아니며 값이 없으면 기본값이 쓰입니다.",
        "권한이 없습니다 라는 메시지가 함께 내려갑니다.",
    ],
)
def test_normal_answers_about_absence_are_not_refusals(text):
    """이 코퍼스는 '없으면 E4012' 같은 문장으로 가득하다.

    짧은 부정어 하나로 잡으면 정상 답변이 통째로 거부로 집계되고, 그러면
    두 모드의 거부율은 둘 다 100% 가 되어 아무것도 구별하지 못한다.
    """
    assert w6.looks_refused(text) is False


def test_a_negation_far_from_any_source_word_is_not_a_refusal():
    """문장 단위로 보는 이유. 두 단어가 답변 어딘가에 따로 있는 것만으로는 아니다."""
    assert w6.looks_refused("재시도 횟수 제한은 찾을 수 없는 값이 아닙니다") is False
    # 반대로 한 문장 안에 둘 다 있으면 거부다.
    assert w6.looks_refused("문서에서 찾을 수 없습니다") is True


def test_a_partial_absence_inside_a_real_answer_is_not_a_refusal():
    """실측에서 이 규칙이 깨졌던 자리. 스모크 2문항 중 1건이 여기서 잘못 잡혔다.

    이 데이터셋의 질문은 '권한이나 중복 때문에 실패하면' 처럼 **둘을 묻는데
    문서에는 하나만 있는** 모양이 많다. 그러면 두 모드 모두 하나를 답하고
    나머지에 대해 '문서에 없다'를 덧붙이는데, 그것은 답을 한 것이다. 덧붙인
    문장만 보고 거부로 세면 두 모드의 거부율이 나란히 부풀어 축이 무의미해진다.
    """
    answered = (
        "강의 단건 조회 시 권한 문제로 실패할 경우 **E4052** 오류 코드가 "
        "내려갑니다 [p.2, p.5].\n\n제공된 문서에는 강의 단건 조회와 관련하여 "
        "'중복'으로 인한 오류 코드는 명시되어 있지 않습니다. 참고로, 강의 등록 "
        "시 `course_id` 가 이미 존재하여 발생하는 중복 오류 코드는 **E4062** "
        "입니다 [p.2]."
    )
    assert w6.looks_refused(answered) is False


def test_a_citation_marker_does_not_split_the_leading_sentence():
    """`[p.2, p.5]` 의 마침표가 문장을 쪼개면 선두 문장 규칙이 엉뚱한 조각을 본다.

    이것을 놓치면 위의 부분 거부 테스트가 **이유를 모른 채** 실패한다 —
    실제로 그렇게 한 번 깨졌다.
    """
    parts = [s for s in w6._SENTENCE.split("답은 E4052 다 [p.2, p.5]. 그리고 끝.")
             if s.strip()]
    assert parts[0] == "답은 E4052 다 [p.2, p.5]"


def test_an_answer_that_is_entirely_refusal_counts_even_behind_a_heading():
    """개조식 답변의 선두는 제목 한 줄일 수 있다."""
    assert w6.looks_refused("### 결론\n문서에 관련 내용이 없습니다.") is True
    assert w6.looks_refused("") is False


def test_the_detector_is_validated_against_mode_a_structured_flag():
    """B 의 거부율은 전적으로 탐지기의 값이다 — 탐지기를 먼저 재야 한다."""
    rows = [
        {"mode": w6.MODE_SERVER, "server_refused": True,
         "server_answer": "제공된 문서에서 찾을 수 없습니다."},
        {"mode": w6.MODE_SERVER, "server_refused": False,
         "server_answer": "답은 [p.1] 이다."},
        # 탐지기가 과탐하는 경우: 참값은 답변인데 문장이 거부처럼 읽힌다.
        {"mode": w6.MODE_SERVER, "server_refused": False,
         "server_answer": "문서에 그 항목은 명시되어 있지 않지만 [p.2] 에 유사한 규정이 있다."},
        # 모드 B 행은 구조화 플래그가 없으므로 검증 표본이 아니다.
        {"mode": w6.MODE_CLIENT, "server_refused": None, "server_answer": None},
    ]
    result = w6.detector_agreement(rows)
    assert result["n"] == 3
    assert result["agreement"] == pytest.approx(2 / 3)
    assert result["false_positive"] == 1
    assert result["false_negative"] == 0


def test_detector_agreement_reports_nothing_rather_than_a_fake_number():
    assert w6.detector_agreement([])["agreement"] is None


# --- 거부 정확도 -----------------------------------------------------------


def _row(mode, qid, qtype, refused, **extra):
    return {
        "mode": mode,
        "question_id": qid,
        "type": qtype,
        "refused": refused,
        "citation": w6.CitationScore(scored=False).to_dict(),
        "cost": w6.Cost().to_dict(),
        "latency": {"server_ms": 0.0, "agent_ms": 0.0, "total_ms": 0.0},
        **extra,
    }


def test_refusal_accuracy_reports_both_directions():
    """한 방향만 내면 '무조건 거부'가 만점이 된다."""
    rows = [
        _row(w6.MODE_CLIENT, "n1", w6.NO_ANSWER_TYPE, True),
        _row(w6.MODE_CLIENT, "n2", w6.NO_ANSWER_TYPE, False),
        _row(w6.MODE_CLIENT, "a1", "single_fact", False),
        _row(w6.MODE_CLIENT, "a2", "single_fact", True),
    ]
    metrics = w6.refusal_metrics(rows)
    assert metrics["no_answer_n"] == 2
    assert metrics["refusal_recall"] == 0.5
    assert metrics["answerable_n"] == 2
    assert metrics["false_refusal_rate"] == 0.5
    # 어느 문항이 틀렸는지까지 낸다 — 6문항짜리 분모에서는 비율보다 목록이
    # 결론에 쓸모 있다.
    assert metrics["wrongly_answered_ids"] == ["n2"]
    assert metrics["wrongly_refused_ids"] == ["a2"]


def test_refusal_accuracy_needs_nothing_but_the_label_and_the_text():
    """이 축이 judge 없이 서는 것이 W6 결론의 무게 중심이다.

    ``refused`` 는 텍스트에서, ``type`` 은 데이터셋에서 온다. 그 둘만 든 행으로
    수치가 나오면, 이 축은 미검증 judge 에 아무것도 빚지지 않는다는 뜻이다.
    """
    bare = [
        {"question_id": "n1", "type": w6.NO_ANSWER_TYPE, "refused": True},
        {"question_id": "a1", "type": "single_fact", "refused": False},
    ]
    metrics = w6.refusal_metrics(bare)
    assert metrics["refusal_recall"] == 1.0
    assert metrics["false_refusal_rate"] == 0.0


# --- 비용 -----------------------------------------------------------------


def test_cost_counts_both_sides_because_mode_b_is_not_free():
    """서버 비용 0 이 질의 비용 0 이 아니다 — 청크를 호출자가 문다."""
    server = w6.Cost(server_in=900, server_out=120, client_in=400, client_out=60)
    client = w6.Cost(server_in=0, server_out=0, client_in=2600, client_out=180)
    assert server.total == 1480
    assert client.server_total == 0
    assert client.total == 2780
    rows = [
        _row(w6.MODE_SERVER, "q1", "single_fact", False, cost=server.to_dict()),
        _row(w6.MODE_CLIENT, "q1", "single_fact", False, cost=client.to_dict()),
    ]
    grouped = w6.by_mode(rows)
    assert w6.cost_metrics(grouped[w6.MODE_SERVER])["server_tokens_mean"] == 1020
    assert w6.cost_metrics(grouped[w6.MODE_CLIENT])["server_tokens_mean"] == 0
    assert w6.cost_metrics(grouped[w6.MODE_CLIENT])["client_tokens_mean"] == 2780


# --- 지연 -----------------------------------------------------------------


def test_latency_reports_median_beside_mean():
    """중앙값이 함께 있어야 429 백오프 한 건이 평균을 끌고 가는 것이 보인다."""
    rows = [
        _row(w6.MODE_SERVER, f"q{i}", "single_fact", False,
             latency={"server_ms": ms, "agent_ms": 100.0, "total_ms": ms + 100.0})
        for i, ms in enumerate([1000.0, 1100.0, 1200.0, 60000.0])
    ]
    metrics = w6.latency_metrics(rows)
    assert metrics["server_ms_median"] == 1150.0
    assert metrics["server_ms_mean"] > 15000
    assert metrics["n"] == 4


# --- 위치 편향 -------------------------------------------------------------


def test_position_bias_removes_the_item_from_the_conclusion():
    """두 판정이 같은 **자리**를 골랐다면 승부가 아니라 편향이다.

    ``judge_pairwise`` 가 순서를 뒤집어 두 번 돌리고, 여기서 그 결과가
    결론에서 빠지는 것을 확인한다 — W6 이 요구한 장치가 실제로 작동하는지.
    """
    decided = l2.PairwiseOutcome(
        question_id="q1",
        variant_a=w6.MODE_SERVER,
        variant_b=w6.MODE_CLIENT,
        pick_ab=w6.MODE_SERVER,
        pick_ba=w6.MODE_SERVER,
    )
    # ab 에서는 첫째(server)를, ba 에서는 첫째(client)를 골랐다 = 자리가 결정했다.
    biased = l2.PairwiseOutcome(
        question_id="q2",
        variant_a=w6.MODE_SERVER,
        variant_b=w6.MODE_CLIENT,
        pick_ab=w6.MODE_SERVER,
        pick_ba=w6.MODE_CLIENT,
    )
    assert decided.winner == w6.MODE_SERVER
    assert decided.position_biased is False
    assert biased.winner is None
    assert biased.position_biased is True

    summary = l2.pairwise_summary([decided, biased])
    assert summary["n"] == 2
    assert summary["decided"] == 1
    assert summary["position_biased"] == 1
    assert summary["wins"] == {w6.MODE_SERVER: 1}


def test_the_mode_names_match_the_variant_names():
    """같은 문자열이 변형·L2Record.variant·리포트 열 이름에 동시에 쓰인다.

    셋 중 하나만 다르면 조인이 조용히 비고, 표는 '레코드 없음'이 아니라
    **한쪽 모드가 빠진 표**로 나온다.
    """
    from app.mcp import variants

    assert w6.MODES == variants.GENERATION_SITE
    for mode in w6.MODES:
        assert variants.get(mode).name == mode
    assert w6.TRACE_SOURCE_OF[w6.MODE_SERVER] == "mcp_answer"
    assert w6.TRACE_SOURCE_OF[w6.MODE_CLIENT] == "mcp_search"


def test_rescore_rebuilds_the_verdicts_from_the_evidence():
    """탐지기를 고치면 이미 산 데이터에도 적용돼야 한다 — 안 그러면 아무도 안 고친다.

    실제로 겪은 일이다: 스모크 실행 뒤 규칙을 고쳤더니 저장된 ``refused`` 는
    옛 규칙의 값이라, 거부 칸과 탐지기 검증 칸이 서로 다른 규칙을 말했다.
    """
    stale = {
        "mode": w6.MODE_SERVER,
        "question_id": "q1",
        "type": "single_fact",
        # 옛 규칙이 남긴 틀린 판정.
        "refused": True,
        "citation": w6.CitationScore(scored=False).to_dict(),
        "citation_server_text": None,
        "final_text": "답은 [p.3] 입니다. 문서에는 중복 오류가 명시되어 있지 않습니다.",
        "server_answer": "답은 [p.3] 입니다.",
        "server_refused": False,
        "seen_pages": [3],
        "gold_pages": [3],
    }
    fresh = w6.rescore([stale])[0]
    assert fresh["refused"] is False
    assert fresh["citation"]["scored"] is True
    assert fresh["citation"]["cited"] == [3]
    assert fresh["citation"]["gold_hit"] is True
    assert fresh["citation_server_text"]["cited"] == [3]
    # 원본은 건드리지 않는다 — jsonl 은 지불의 기록이다.
    assert stale["refused"] is True


def test_rescore_uses_the_servers_flag_for_the_server_text_column():
    """서버 원문 칸의 존재 이유는 '구조화된 판정이 있으면 이렇게 된다' 이다.

    거기까지 텍스트 탐지기로 덮으면 두 칸이 같은 자로 재게 되어 비교 대상이
    사라진다.
    """
    row = {
        "mode": w6.MODE_SERVER,
        "final_text": "문서에서 찾을 수 없습니다.",
        # 텍스트는 거부처럼 읽히지만 서버는 답했다고 찍었다 — 구조화 판정이 이긴다.
        "server_answer": "제공된 문서에는 해당 항목이 명시되어 있지 않지만 [p.2] 를 보라.",
        "server_refused": False,
        "seen_pages": [2],
        "gold_pages": [2],
    }
    fresh = w6.rescore([row])[0]
    assert fresh["refused"] is True                      # 최종 텍스트는 탐지기로
    assert fresh["citation_server_text"]["scored"] is True  # 원문은 서버 플래그로


def test_summarize_keeps_l2_out_of_the_pure_half():
    """미검증 judge 의 수치가 순수 함수들 사이에 섞여 앉으면 안 된다."""
    rows = [
        _row(w6.MODE_SERVER, "q1", "single_fact", False),
        _row(w6.MODE_CLIENT, "q1", "single_fact", False),
    ]
    summary = w6.summarize(rows)
    assert set(summary) == set(w6.MODES)
    for mode in w6.MODES:
        assert "l2" not in summary[mode]
        assert set(summary[mode]) >= {
            "refusal", "citation", "cost", "latency", "structured_citations"
        }
    # A 에만 '서버 원문' 칸이 있다. B 에서는 원문이 곧 최종 텍스트다.
    assert summary[w6.MODE_SERVER]["citation_server_text"] is not None
    assert summary[w6.MODE_CLIENT]["citation_server_text"] is None
