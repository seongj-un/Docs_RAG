"""M7 W5: section chunking and the heading-path prefix (no infra required).

두 가지를 나눠서 본다. 하나는 **새 동작이 실제로 다른가** — 섹션 경계에서
잘리는가, 접두사가 붙는가. 다른 하나는 **기존 동작이 그대로인가** — 기본값으로
부르면 M1 때와 같은 청크가 나오는가. 뒤쪽이 깨지면 프로덕션 인덱싱이 조용히
달라지고, 그건 이 작업에서 가장 비싼 사고다.
"""

import pytest

from app.services import chunking

PAGE = """# 학사 관리 API 명세
## 학생
### 목록 조회
GET /v1/students 로 학생 목록을 쪽 단위로 돌려준다. 권한 밖의 자원을 요청하면 오류가 내려간다.
#### 응답 필드
| 이름 | 타입 | 설명 |
| student_id | string | 학생 고유 식별자 |
| enrolled_year | integer | 입학 연도 |
### 단건 조회
GET /v1/students/{id} 로 학생 하나를 돌려준다. 목록과 같은 권한 규칙을 따른다.
"""

# 섹션이 하나로 합쳐지지 않을 만큼 작은 예산. 700 으로는 이 쪽 전체가 한
# 청크라 "섹션 경계에서 잘렸는가"를 볼 수가 없다.
SMALL = 30


def test_default_call_is_unchanged_fixed_chunking():
    """기본값은 M1 동작이다 — 경로도 없고 임베딩 입력도 content 그대로다."""
    chunks = chunking.chunk_pages([PAGE], chunk_size=700, chunk_overlap=100)

    assert len(chunks) == 1
    assert chunks[0].heading_path == ()
    assert chunks[0].embed_text == chunks[0].content
    # 명시적으로 fixed 를 넘긴 것과 같아야 한다.
    assert chunks == chunking.chunk_pages(
        [PAGE], chunk_size=700, chunk_overlap=100, strategy="fixed"
    )


def _starts(chunks) -> set[str]:
    return {c.content.splitlines()[0].strip() for c in chunks}


def test_section_chunking_cuts_at_headings_and_fixed_does_not():
    fixed = chunking.chunk_pages([PAGE], SMALL, 5)
    section = chunking.chunk_pages([PAGE], SMALL, 5, strategy="section")

    # 본문을 가진 소절 헤딩은 섹션 전략에서 어떤 청크의 **첫 줄**이 된다.
    # ("### 목록 조회"는 빠진다 — 바로 앞의 "# 명세"·"## 학생"이 본문 없는
    # 헤딩이라 한 청크로 딸려 들어가고, 그 청크의 첫 줄은 "# 명세"다.)
    headings = {"#### 응답 필드", "### 단건 조회"}
    assert headings <= _starts(section)
    # 고정 창에서는 그렇지 않다 — 헤딩이 청크 한가운데에 묻힌다.
    assert not headings & _starts(fixed)


def test_section_chunking_keeps_a_table_whole():
    """표가 청크 경계에서 쪼개지면 머리행과 데이터행이 갈라진다 — Notion W5."""
    section = chunking.chunk_pages([PAGE], SMALL, 5, strategy="section")
    holder = [c for c in section if "| student_id | string" in c.content]

    assert len(holder) == 1
    assert "| 이름 | 타입 | 설명 |" in holder[0].content


def test_heading_prefix_changes_only_the_embedder_input():
    """content 는 원문의 축자 부분문자열로 남아야 한다 — 인용과 gold 가 여기 기댄다."""
    plain = chunking.chunk_pages([PAGE], SMALL, 5, strategy="section")
    prefixed = chunking.chunk_pages(
        [PAGE], SMALL, 5, strategy="section", heading_prefix=True
    )

    assert [c.content for c in plain] == [c.content for c in prefixed]
    assert all(c.content in PAGE for c in prefixed)
    assert any(c.embed_text != c.content for c in prefixed)
    first = prefixed[0]
    assert first.embed_text.startswith(
        chunking.HEADING_SEP.join(first.heading_path)
    )


def test_a_chunk_that_starts_mid_section_still_names_its_section():
    """접두사가 가장 필요한 청크가 정확히 이것이다 — 헤딩을 한 글자도 못 담은 청크."""
    chunks = chunking.chunk_pages([PAGE], SMALL, 5, heading_prefix=True)
    orphans = [c for c in chunks if not c.content.lstrip().startswith("#")]

    assert orphans, "고정 창이 헤딩 밖에서 시작하는 청크를 하나도 안 만들었다"
    assert all(len(c.heading_path) >= 2 for c in orphans)


def test_heading_context_carries_into_the_next_page():
    """쪽이 넘어가도 섹션은 이어진다. 이어지는 쪽이 경로를 잃으면 안 된다."""
    pages = ["# 문서\n## 절\n### 소절\n첫 쪽의 본문이다.", "앞 쪽에서 이어지는 본문이다."]
    chunks = chunking.chunk_pages(pages, 700, 100, heading_prefix=True)

    tail = [c for c in chunks if c.page_from == 2]
    assert tail
    assert tail[0].heading_path == ("문서", "절", "소절")


def test_sections_are_packed_up_to_the_budget():
    """작은 절들이 예산까지 합쳐진다 — 안 그러면 섹션 청킹은 그냥 '작은 청크'다."""
    packed = chunking.chunk_pages([PAGE], 700, 100, strategy="section")
    loose = chunking.chunk_pages([PAGE], SMALL, 5, strategy="section")

    assert len(packed) == 1 < len(loose)
    # 합쳐진 청크의 경로는 합쳐진 섹션들의 공통 조상이다.
    assert packed[0].heading_path == ()  # prefix 를 끄면 경로를 달지 않는다
    with_prefix = chunking.chunk_pages(
        [PAGE], 700, 100, strategy="section", heading_prefix=True
    )
    # 합쳐진 섹션이 전부 "학생" 아래에 있으므로 공통 조상은 거기까지다.
    assert with_prefix[0].heading_path == ("학사 관리 API 명세", "학생")


def test_a_heading_with_no_body_does_not_become_its_own_chunk():
    """'## 학생' 한 줄짜리 청크는 후보 한 자리를 먹고 아무 정보도 안 준다."""
    chunks = chunking.chunk_pages([PAGE], SMALL, 5, strategy="section")

    assert not any(c.content.strip() == "## 학생" for c in chunks)
    assert any("## 학생" in c.content for c in chunks)


def test_every_chunk_stays_within_one_page_under_both_strategies():
    pages = [PAGE, PAGE]
    for strategy in chunking.STRATEGIES:
        for chunk in chunking.chunk_pages(pages, SMALL, 5, strategy=strategy):
            assert chunk.page_from == chunk.page_to


def test_chunk_index_is_sequential_under_section_chunking():
    chunks = chunking.chunk_pages([PAGE, PAGE], SMALL, 5, strategy="section")
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_a_section_longer_than_the_budget_is_still_windowed():
    """섹션 하나가 예산을 넘으면 그 안에서 창을 낸다 — 안 그러면 임베더가 자른다."""
    page = "# 문서\n### 긴 절\n" + ("가나다라마바사 아자차카타파하. " * 200)
    chunks = chunking.chunk_pages([page], SMALL, 5, strategy="section")

    assert len(chunks) > 1
    assert all(c.token_count <= SMALL for c in chunks)


def test_text_without_headings_falls_back_to_one_section():
    """헤딩이 없는 문서(이 저장소의 기존 코퍼스 전부)에서도 죽지 않는다."""
    page = "제1조 (보증금) 임대차 보증금은 금 오천만원으로 한다."
    section = chunking.chunk_pages([page], 700, 100, strategy="section")
    fixed = chunking.chunk_pages([page], 700, 100)

    assert [c.content for c in section] == [c.content for c in fixed]


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError, match="unknown chunk strategy"):
        chunking.chunk_pages([PAGE], 700, 100, strategy="semantic")
