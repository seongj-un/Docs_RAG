"""How an L3 episode is scored (M7 W4).

Every decision in ``eval/l3.py``'s scoring that could reasonably have gone the
other way has a test here, because the choice is what the reported numbers mean:

* ``query_contains`` is **AND**, not OR (an OR list measures nothing);
* matching ignores spacing and case but not affixes (Korean particles);
* argument accuracy is **not scored** when the wrong tool was chosen, so one
  failure is not counted twice;
* a call made in a scenario where silence was correct is an unnecessary call.

No database, no model — the episodes are hand-built.
"""

import pytest

from app.mcp.variants import DECOMPOSED, MONOLITHIC, ROLE_NONE
from eval import l3
from eval import scenarios as scen

SET = {s.id: s for s in scen.load()}


def episode(*calls, turns=2, truncated=False):
    return l3.Episode(
        scenario_id="t",
        calls=[l3.Call(name=n, args=a) for n, a in calls],
        turns=turns,
        truncated=truncated,
    )


def scenario(**over):
    import json

    base = {
        "id": "t",
        "utterance": "질문",
        "expected_tool": "search",
        "kind": "single_search",
        "why": "문서에 적힌 사실을 물었기 때문이다.",
        "max_calls": 2,
    }
    base.update(over)
    return scen.parse(json.dumps(base, ensure_ascii=False))


# --- 툴 선택 ---------------------------------------------------------------


def test_right_tool_first_is_correct():
    scored = l3.score(scenario(), episode(("search_documents", {"question": "질문"})),
                      MONOLITHIC)
    assert scored.tool_choice is True


def test_wrong_tool_first_is_incorrect_even_if_the_right_one_follows():
    """첫 호출이 곧 설명을 읽고 내린 선택이다. 뒤의 교정은 다른 지표가 잰다."""
    scored = l3.score(
        scenario(),
        episode(("list_collections", {}), ("search", {"question": "질문"})),
        DECOMPOSED,
    )
    assert scored.tool_choice is False


def test_silence_is_correct_when_silence_was_the_answer():
    silent = scenario(expected_tool="none", max_calls=0, min_calls=0)
    assert l3.score(silent, episode(), MONOLITHIC).tool_choice is True
    called = l3.score(silent, episode(("search_documents", {})), MONOLITHIC)
    assert called.tool_choice is False


def test_missing_role_falls_back_to_silence():
    """분해 조건에만 있는 툴을 기대하는 문항은, 없는 조건에서 침묵이 정답이다."""
    listing = scenario(expected_tool="list", max_calls=2)
    assert l3.score(listing, episode(), MONOLITHIC).tool_choice is True
    # 그리고 검색으로 흉내 내는 것은 정답이 아니다 — 검색은 구절을 돌려주지
    # 문서 목록을 돌려주지 않는다.
    called = l3.score(listing, episode(("search_documents", {"question": "문서"})),
                      MONOLITHIC)
    assert called.tool_choice is False
    assert l3.score(listing, episode(("list_collections", {})), DECOMPOSED).tool_choice


# --- 인자 정확도 -----------------------------------------------------------


def args_score(spec, calls):
    return l3.score_arguments(
        scenario(expected_args=spec),
        [l3.Call(name="search_documents", args=a) for a in calls],
        "question",
    )


def test_query_contains_is_and_not_or():
    spec = {"query_contains": ["학생", "목록"]}
    assert args_score(spec, [{"question": "학생 목록 조회"}]) is True
    # OR 로 읽으면 아래가 통과한다. 그러면 목록으로 적은 의미가 없어진다.
    assert args_score(spec, [{"question": "학생 응답 형태"}]) is False


def test_matching_ignores_spacing_and_case_but_survives_particles():
    assert args_score({"query_contains": ["대기 순번"]},
                      [{"question": "대기순번은 어떤 필드야"}]) is True
    assert args_score({"query_contains": ["E4012"]},
                      [{"question": "e4012 가 떴어"}]) is True


def test_query_not_contains_catches_a_translated_query():
    spec = {"query_not_contains": ["student"]}
    assert args_score(spec, [{"question": "학생 목록 응답 형태"}]) is True
    assert args_score(spec, [{"question": "student list response shape"}]) is False


def test_args_exact_and_args_absent():
    assert args_score({"args_exact": {"document_id": "D-1"}},
                      [{"question": "q", "document_id": "d-1"}]) is True
    assert args_score({"args_exact": {"document_id": "D-1"}},
                      [{"question": "q", "document_id": "D-2"}]) is False
    assert args_score({"args_absent": ["document_id"]}, [{"question": "q"}]) is True
    assert args_score({"args_absent": ["document_id"]},
                      [{"question": "q", "document_id": "D-9"}]) is False


def test_only_the_first_call_is_judged_except_any_call_contains():
    # 첫 호출이 틀렸으면 두 번째가 맞아도 인자 정확도는 오답이다.
    assert args_score({"query_contains": ["강의"]},
                      [{"question": "학생"}, {"question": "강의"}]) is False
    # 반면 여러 번 부르는 것이 정답인 문항의 조건은 호출 전체에 걸린다.
    assert args_score({"any_call_contains": [["학생"], ["강의"]]},
                      [{"question": "학생 한도"}, {"question": "강의 한도"}]) is True
    assert args_score({"any_call_contains": [["학생"], ["강의"]]},
                      [{"question": "학생 한도"}]) is False


def test_the_groups_must_be_satisfied_by_different_calls():
    """한 질의가 두 묶음을 다 덮으면 이 제약은 query_contains 와 같아진다.

    그리고 합친 질의는 사용자 쪽 실패 모드다 — 리랭커가 세 구절을 전부 한쪽에서
    골라 오면 나머지 절반은 조용히 빠진 채 답이 완성된다.
    """
    spec = {"any_call_contains": [["학생"], ["강의"]]}
    assert args_score(spec, [{"question": "학생 강의 분당 한도"}]) is False
    assert (
        args_score(spec, [{"question": "학생 한도"}, {"question": "강의 한도"}]) is True
    )
    # 순서가 뒤바뀌어도, 남는 호출이 있어도 통과해야 한다.
    assert args_score(
        spec,
        [
            {"question": "강의 한도"},
            {"question": "쓸모없는 질의"},
            {"question": "학생 한도"},
        ],
    ) is True


def test_arguments_are_not_scored_when_the_tool_choice_was_wrong():
    """같은 실패를 두 지표에 두 번 세지 않는다."""
    scored = l3.score(
        scenario(expected_args={"query_contains": ["학생"]}),
        episode(("list_collections", {})),
        DECOMPOSED,
    )
    assert scored.tool_choice is False
    assert scored.arguments is None


def test_arguments_are_not_scored_on_silent_scenarios():
    silent = scenario(expected_tool="none", max_calls=0, min_calls=0)
    assert l3.score(silent, episode(), MONOLITHIC).arguments is None


# --- 호출 수 · 불필요 호출 -------------------------------------------------


def test_a_call_in_a_silent_scenario_is_entirely_unnecessary():
    silent = scenario(expected_tool="none", max_calls=0, min_calls=0)
    twice = episode(("search_documents", {}), ("search_documents", {}))
    scored = l3.score(silent, twice, MONOLITHIC)
    assert (scored.unnecessary_calls, scored.over_budget) == (2, True)


def test_the_calls_a_scenario_needs_are_not_unnecessary():
    multi = scenario(min_calls=2, max_calls=3)
    twice = episode(("search_documents", {}), ("search_documents", {}))
    scored = l3.score(multi, twice, MONOLITHIC)
    assert (scored.unnecessary_calls, scored.over_budget) == (0, False)


def test_going_over_the_budget_is_recorded_without_capping_the_count():
    """예산은 채점 기준이지 차단기가 아니다 — 차단기는 HARD_CALL_CAP 이다.

    둘이 같은 값이면 "예산 초과"가 구조적으로 일어날 수 없고, 불필요 호출
    비율은 0 에 붙은 채 아무것도 말하지 않게 된다.
    """
    assert l3.HARD_CALL_CAP > max(s.max_calls for s in SET.values())
    scored = l3.score(
        scenario(), episode(*[("search_documents", {"question": "q"})] * 4), MONOLITHIC
    )
    assert (scored.calls, scored.unnecessary_calls, scored.over_budget) == (4, 3, True)


# --- 집계 ------------------------------------------------------------------


def test_aggregate_reports_its_own_denominator_for_argument_accuracy():
    rows = [
        l3.Score("a", "k", "search", "search_documents", True, True, 1, 2, 0, False,
                 "search_documents", False),
        l3.Score("b", "k", "search", "search_documents", False, None, 1, 2, 0, False,
                 "other", False),
        l3.Score("c", "k", ROLE_NONE, None, True, None, 0, 1, 0, False, None, False),
    ]
    agg = l3.aggregate(rows)
    assert agg["tool_choice_accuracy"] == pytest.approx(2 / 3)
    # 채점된 것은 하나뿐이고, 그 사실이 표에 함께 나가야 100% 가 오해되지 않는다.
    assert agg["argument_accuracy"] == 1.0
    assert agg["argument_scored_n"] == 1
    assert agg["calls_on_no_tool_scenarios"] == 0


def test_unnecessary_call_rate_is_zero_when_nothing_was_called():
    rows = [
        l3.Score("a", "k", ROLE_NONE, None, True, None, 0, 1, 0, False, None, False)
    ]
    assert l3.aggregate(rows)["unnecessary_call_rate"] == 0.0
