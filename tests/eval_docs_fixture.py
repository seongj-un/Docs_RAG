"""A tiny document provider, used to drive the harness end to end in tests.

Lives beside the tests rather than in ``eval/datasets`` because it exists only
to prove the ``--provider`` seam works with a module the harness has never
heard of — which is exactly how the private work-document golden set will be
plugged in.
"""

_DOCS = {
    "manual": [
        "제1조 (보증금) 임대차 보증금은 금 오천만원으로 한다.",
        "제2조 (차임) 월 차임은 금 삼백만원이며 매월 5일에 지급한다.",
        "제3조 (반려동물) 체중 10kg 이하 소형견 1마리만 사육할 수 있다.",
    ]
}


def names() -> list[str]:
    return sorted(_DOCS)


def pages_for(doc: str) -> list[str]:
    return _DOCS[doc]
