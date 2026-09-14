"""Tests for resolving gold spans to chunk ids (no infra required).

이 파일이 지키는 것은 M7 골든셋이 청킹 변경에서 살아남는다는 주장 자체다.
같은 스니펫이 청크 경계가 어디로 옮겨가든 옳은 청크들을 가리켜야 하고, 문서가
바뀌어 못 찾을 때는 조용히 0점이 아니라 예외로 터져야 한다.
"""

import uuid

import pytest

from app.services import chunking
from eval import gold


def chunk(text: str, page: int = 1) -> gold.SourceChunk:
    return gold.SourceChunk(uuid.uuid4(), page, text)


PAGE = "제2조 (보증금) 임대차 보증금은 금 오천만원(50,000,000원)으로 한다."


def test_normalize_collapses_extraction_whitespace():
    assert gold.normalize("가  나\n다\t라 ") == "가 나 다 라"


def test_snippet_inside_one_chunk_resolves_to_that_chunk():
    hit = chunk(PAGE)
    other = chunk("제3조 (차임) 월 차임은 금 삼백만원으로 한다.", page=2)

    resolved = gold.resolve_question(
        ["임대차 보증금은 금 오천만원"], [PAGE, other.content], [hit, other]
    )

    assert resolved == [{hit.chunk_id}]


def test_whitespace_differences_do_not_break_matching():
    hit = chunk("제2조 (보증금)  임대차\n보증금은 금 오천만원으로 한다.")
    resolved = gold.resolve_question(
        ["임대차 보증금은 금 오천만원"], [hit.content], [hit]
    )
    assert resolved == [{hit.chunk_id}]


def test_snippet_crossing_a_chunk_boundary_marks_every_overlapping_chunk():
    # 경계를 넘는 스니펫을 "통째로 포함하는 청크"로만 찾으면 정답이 0개가 되고,
    # 멀쩡한 검색이 실패로 기록된다.
    page = "앞부분 사실은 여기서 시작하고 뒷부분 에서 끝난다"
    first = chunk("앞부분 사실은 여기서")
    second = chunk("시작하고 뒷부분 에서 끝난다")

    resolved = gold.resolve_question(["여기서 시작하고"], [page], [first, second])

    assert resolved == [{first.chunk_id, second.chunk_id}]


def test_overlapping_chunks_both_count():
    page = "하나 둘 셋 넷 다섯 여섯"
    first = chunk("하나 둘 셋 넷")
    second = chunk("셋 넷 다섯 여섯")  # CHUNK_OVERLAP 이 만드는 모양

    resolved = gold.resolve_question(["셋 넷"], [page], [first, second])

    assert resolved == [{first.chunk_id, second.chunk_id}]


def test_missing_snippet_raises_instead_of_scoring_zero():
    hit = chunk(PAGE)
    with pytest.raises(gold.GoldResolutionError, match="사라졌다"):
        gold.resolve_question(["이 문장은 문서에 없다 정말로"], [PAGE], [hit])


def test_snippet_matching_too_many_places_raises():
    page = " ".join(["반복되는 같은 문장이다."] * (gold.MAX_MATCHES + 1))
    hit = chunk(page)
    with pytest.raises(gold.GoldResolutionError, match="곳에 걸린다"):
        gold.resolve_question(["반복되는 같은 문장이다."], [page], [hit])


def test_match_count_and_pages_need_no_index():
    pages = ["첫 쪽 내용이다.", "둘째 쪽에 정답이 있다."]
    assert gold.match_count("둘째 쪽에 정답이", pages) == 1
    assert gold.match_count("없는 문장", pages) == 0
    assert gold.pages_of("둘째 쪽에 정답이", pages) == [2]


def test_resolution_survives_a_change_of_chunk_size():
    """The same snippet must resolve correctly under different chunking.

    청킹 전략 비교(W5)가 골든셋을 무효화하지 않는다는 것이 이 방식의 유일한
    존재 이유다. 같은 쪽을 다른 크기로 잘라도 정답 청크는 스니펫을 덮어야 한다.
    """
    page = " ".join(f"문장{i}번 내용이 여기에 있다." for i in range(1, 60))
    page += " 결정적인 사실은 마지막에 적혀 있다."
    snippet = "결정적인 사실은 마지막에 적혀 있다."

    for size, overlap in ((700, 100), (120, 20), (60, 10)):
        parts = chunking.chunk_pages([page], chunk_size=size, chunk_overlap=overlap)
        chunks = [gold.SourceChunk(uuid.uuid4(), 1, p.content) for p in parts]

        resolved = gold.resolve_question([snippet], [page], chunks)

        assert len(resolved) == 1
        assert resolved[0], f"size={size} 에서 정답 청크를 못 찾았다"
        covering = {
            c.chunk_id for c in chunks if gold.normalize(snippet) in gold.normalize(c.content)
        }
        # 통째로 포함하는 청크가 있으면 그것들은 반드시 포함돼야 한다.
        assert covering <= resolved[0]
