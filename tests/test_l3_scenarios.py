"""The L3 scenario set and its loader (M7 W4).

The set is the measuring instrument, so the properties that make it able to
measure anything are asserted here rather than trusted. Two of them come
straight from Notion W4's third trap — "기대 툴을 라벨링할 때 본인 구현을 정답
으로 가정하지 말 것":

* **툴을 부르지 않는 것이 정답인 문항이 있어야 한다.** Without them "tool
  selection accuracy" collapses into "did it call the tool", which every
  condition scores 100% on and which tells us nothing about tool design.
* **여러 번 부르는 것이 정답인 문항이 있어야 한다.** Without them "call count"
  is a cost metric with no upper truth to compare against, and fewer always
  looks better.

Needs no database and no model: everything here is the file and the loader.
"""

import json
from collections import Counter

import pytest

from app.mcp.variants import DECOMPOSED, MONOLITHIC, ROLE_NONE
from eval import scenarios as scen

SET = scen.load()


def test_the_set_is_twenty_scenarios():
    # Notion 이 정한 크기. 줄이면 유형별 칸이 한 문항짜리가 되고, 늘리면 무료
    # 티어 예산 안에서 n=2 조차 못 돌린다.
    assert len(SET) == 20


def test_not_calling_a_tool_is_the_right_answer_somewhere():
    silent = [s for s in SET if s.expected_tool == ROLE_NONE]
    assert len(silent) >= 4, "툴을 안 부르는 것이 정답인 문항이 너무 적다"
    # 그리고 서로 다른 이유여야 한다. 인사 다섯 개는 한 문항을 다섯 번 센 것이다.
    assert len({s.kind for s in silent}) >= 4


def test_calling_twice_is_the_right_answer_somewhere():
    multi = [s for s in SET if s.min_calls >= 2]
    assert len(multi) >= 2
    for scenario in multi:
        # 여러 번이 정답인 문항은 "각각 찾았는가"를 채점해야 의미가 있다.
        assert scenario.expected_args.get("any_call_contains")


def test_every_role_the_variants_expose_is_exercised():
    roles = Counter(s.expected_tool for s in SET)
    for role in set(DECOMPOSED.role_map()) | set(MONOLITHIC.role_map()):
        assert roles[role] >= 1, f"역할 {role!r} 을 재는 문항이 없다"


def test_silent_scenarios_have_no_call_budget():
    for scenario in SET:
        if scenario.expected_tool == ROLE_NONE:
            assert scenario.max_calls == 0 and scenario.min_calls == 0


def test_every_label_carries_its_reason():
    # 함정 3 의 방어선. 근거를 못 쓰면 그 라벨은 우리 구현을 베낀 것일 수 있다.
    for scenario in SET:
        assert len(scenario.why) >= 20
        # "우리 툴이 그렇게 생겨서"가 이유인 라벨을 잡아내지는 못하지만, 이유
        # 칸이 툴 이름으로만 채워지는 것은 막는다.
        assert scenario.why.strip() != scenario.utterance.strip()


def test_only_fetch_and_list_scenarios_change_answer_between_conditions():
    """The fallback must be inert everywhere except where a tool is missing.

    ``search`` 는 모든 변형에 있으므로 그 문항의 정답은 조건과 무관해야 한다.
    조건마다 정답이 달라지는 문항이 늘어나면 두 조건의 평균은 같은 시험을 본
    점수가 아니게 되고, 표의 두 줄을 나란히 읽을 수 없다.
    """
    moved = [
        s.id
        for s in SET
        if s.expected_role_for(MONOLITHIC.role_map())
        != s.expected_role_for(DECOMPOSED.role_map())
    ]
    assert sorted(moved) == sorted(
        s.id for s in SET if s.expected_tool in ("fetch", "list")
    )


def test_role_resolution_differs_between_conditions():
    listing = next(s for s in SET if s.expected_tool == "list")
    assert listing.expected_role_for(DECOMPOSED.role_map()) == "list"
    # 분해하지 않은 조건에는 목록 툴이 없다. 그때 사용자 입장에서 옳은 행동은
    # 검색으로 흉내 내는 것이 아니라 못 한다고 말하는 것이다.
    assert listing.expected_role_for(MONOLITHIC.role_map()) == ROLE_NONE


def test_dataset_hash_is_the_file_bytes():
    import hashlib

    expected = hashlib.sha256(scen.DEFAULT_PATH.read_bytes()).hexdigest()
    assert scen.dataset_sha256() == expected


# --- 로더가 막아야 하는 것들 ----------------------------------------------


def _line(**over):
    base = {
        "id": "x1",
        "utterance": "질문",
        "expected_tool": "search",
        "kind": "single_search",
        "why": "사용자가 문서에 적힌 사실을 물었기 때문이다.",
        "max_calls": 2,
    }
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


def test_loader_accepts_a_minimal_line():
    scenario = scen.parse(_line())
    assert scenario.min_calls == 1 and scenario.fallback_tool == ROLE_NONE


@pytest.mark.parametrize(
    "over, fragment",
    [
        ({"expected_tool": "search_documents"}, "역할이 아니다"),
        ({"why": "짧"}, "why"),
        ({"expected_tool": "none", "max_calls": 2}, "max_calls 도 0"),
        ({"min_calls": 5, "max_calls": 2}, "min_calls"),
        ({"expected_args": {"nope": []}}, "모르는 expected_args"),
        ({"expected_args": {"query_contains": "학생"}}, "문자열 배열"),
    ],
)
def test_loader_refuses_bad_lines(over, fragment):
    with pytest.raises(ValueError) as err:
        scen.parse(_line(**over))
    assert fragment in str(err.value)


def test_duplicate_ids_are_rejected(tmp_path):
    path = tmp_path / "dup.jsonl"
    path.write_text(_line() + "\n" + _line() + "\n", encoding="utf-8")
    with pytest.raises(ValueError) as err:
        scen.load(path)
    assert "중복" in str(err.value)


# --- 자리표시자 ------------------------------------------------------------


def test_placeholders_resolve_in_history_and_in_expected_args():
    resolver = scen.Resolver(
        {"spec_errors.pdf": "DOC-1"}, {"spec_api.pdf": ["C0", "C1"]}
    )
    raw = scen.parse(
        _line(
            id="p1",
            utterance="chunk_id 가 {{chunk:spec_api.pdf:1}} 인 것",
            history=[{"role": "model", "text": "document_id {{doc:spec_errors.pdf}}"}],
            expected_args={"args_exact": {"document_id": "{{doc:spec_errors.pdf}}"}},
        )
    )
    resolved = resolver.scenario(raw)
    assert "C1" in resolved.utterance
    assert resolved.history[0].text.endswith("DOC-1")
    assert resolved.expected_args["args_exact"]["document_id"] == "DOC-1"


def test_placeholder_for_a_missing_document_is_loud():
    resolver = scen.Resolver({}, {})
    with pytest.raises(KeyError):
        resolver.text("{{doc:nope.pdf}}")


def test_every_placeholder_in_the_shipped_set_names_a_corpus_document():
    from eval.corpora import spec

    known = {f"{name}.pdf" for name in spec.DOCUMENTS}
    for scenario in SET:
        blob = scenario.utterance + json.dumps(scenario.expected_args) + "".join(
            t.text for t in scenario.history
        )
        for match in scen._PLACEHOLDER.finditer(blob):
            assert match.group(2) in known, match.group(0)


def test_normalize_folds_spacing_and_case_but_not_particles():
    # 조사가 붙은 말 안의 명사는 여전히 부분 문자열로 잡혀야 한다.
    assert scen.normalize("대기 순번") in scen.normalize("대기순번은 어디에")
    assert scen.normalize("E4012") in scen.normalize("e4012 오류")
