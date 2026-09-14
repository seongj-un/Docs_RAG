"""A tiny heading-structured document provider for the W5 A/B tests.

``tests/eval_docs_fixture.py`` 의 문서는 헤딩이 없다 — 그 픽스처는 W3 하네스
배관을 보는 것이라 그럴 이유가 없었다. W5 사다리는 청킹 **전략**을 바꾸므로,
헤딩이 없는 문서로 돌리면 두 전략이 같은 청크를 내고 테스트는 "사다리가
청킹을 바꾼다"를 검증하지 못한 채 초록으로 통과한다.

쪽은 CHUNK_SIZE(700토큰)보다 크다. 작으면 고정 창이 쪽을 한 번도 쪼개지
않아, 여기서도 두 전략이 같아진다 — eval/corpora/spec.py 가 적은 것과 같은
이유다.
"""

_FILLER = (
    "이 절의 규칙은 별도의 안내가 없는 한 모든 엔드포인트에 같게 적용되며, "
    "예외는 각 절에 따로 적는다. "
)


def _bulk(times: int) -> str:
    return (_FILLER * times).strip()


_PAGES = [
    "\n".join([
        "# 학사 API 명세",
        "## 학생",
        "### 목록 조회",
        f"GET /v1/students 로 학생 목록을 돌려준다. {_bulk(11)}",
        "#### 오류 코드",
        "| 코드 | HTTP | 발생 조건 |",
        "| E4012 | 403 | 다른 학과를 지정했다 |",
        "#### 비고",
        f"분당 호출 한도는 31회다. {_bulk(11)}",
    ]),
    "\n".join([
        "# 학사 API 명세",
        "## 학생",
        "### 단건 조회",
        f"GET /v1/students/{{id}} 로 학생 하나를 돌려준다. {_bulk(11)}",
        "#### 응답 필드",
        "| 이름 | 타입 | 설명 |",
        "| enrolled_year | integer | 입학 연도를 담는다 |",
        "#### 비고",
        f"분당 호출 한도는 38회다. {_bulk(11)}",
    ]),
]

_DOCS = {"spec": _PAGES}


def names() -> list[str]:
    return sorted(_DOCS)


def pages_for(doc: str) -> list[str]:
    return _DOCS[doc]
