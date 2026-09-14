"""Regenerate ``spec_golden.jsonl`` from ``eval/corpora/spec.py``.

    python -m eval.datasets.build_spec

Same contract as ``build_synthetic.py``: the jsonl is committed, and this
script exists so the conversion is reviewable and repeatable. 손으로 44줄을
옮겨 적으면 스니펫 하나가 조용히 어긋나도 아무도 모른다.

**차이 하나.** ``build_synthetic`` 은 쪽 전체에서 고유한 스니펫을 찾는다. 이
코퍼스에서는 그러면 안 된다 — 명세 쪽의 앞부분은 열여덟 쪽이 공유하는
보일러플레이트라, 쪽 앞에서 자른 스니펫은 고유하지 않거나(운 좋게 고유해도)
질문의 답과 아무 상관이 없다. 그래서 ``spec.py`` 의 질문은 정답이 적힌 **줄**을
지목하는 ``anchor`` 를 들고 있고, 여기서는 그 줄 안에서만 창을 고른다.

**청킹 실험과의 관계.** W5 는 청킹 전략을 바꾼다. 정답을 원문 스니펫으로
저장하므로 러너가 실행 시점에 다시 푼다(``eval/gold.py``) — 원칙적으로는
견딘다. 다만 스니펫이 길수록 청크 경계를 넘을 확률이 커지고, 넘으면 정답
청크가 둘이 되어 지표가 청킹 쪽으로 흔들린다. 그래서 여기서는 **고유한 것 중
가장 짧은** 창을 고른다(build_synthetic 은 가장 긴 창을 고른다). 한 줄 안에서
자르므로 한 청크 안에 들어갈 가능성도 그만큼 높다.
"""

from pathlib import Path

from eval.corpora import spec
from eval.datasets import SNIPPET_MAX, SNIPPET_MIN, GoldSpan, Question, dumps
from eval.datasets import synthetic
from eval.datasets.build_synthetic import assign_splits, write
from eval.gold import match_count, normalize

_HERE = Path(__file__).resolve().parent
VERIFIED_AT = "2026-09-14"
OUT = "spec_golden.jsonl"

SOURCE = "eval/corpora/spec.py — M7 W5 명세형 합성 코퍼스(API 명세·오류 사전·운영 정책)"


def _anchor_line(page_text: str, anchor: str) -> str:
    """The single source line an anchor points at."""
    for line in page_text.split("\n"):
        if anchor in line:
            return normalize(line)
    raise RuntimeError(f"anchor {anchor!r} 가 어느 줄에도 없다")


def snippet_for(doc: str, page: int, anchor: str, where: str) -> str:
    """Shortest unique 20-80 char window inside the anchored line.

    가장 짧은 것을 고르는 이유는 청킹 실험 때문이다 — 스니펫이 짧을수록 청크
    경계를 덜 넘고, 그래야 지표가 "검색이 찾았는가"만 재고 "청킹이 어떻게
    잘랐는가"에 덜 휘둘린다.
    """
    pages = synthetic.pages_for(doc)
    line = _anchor_line(pages[page - 1], anchor)
    words = line.split()

    best: str | None = None
    for start in range(len(words)):
        window: list[str] = []
        for word in words[start:]:
            window.append(word)
            text = " ".join(window)
            if len(text) > SNIPPET_MAX:
                break
            if len(text) < SNIPPET_MIN:
                continue
            if match_count(text, pages) != 1:
                continue
            if best is None or len(text) < len(best):
                best = text
            break
    if best is None:
        raise RuntimeError(
            f"{where}: {line[:60]!r} 안에서 고유한 20~80자 창을 못 만들었다 — "
            "그 줄이 문서 안에서 구분되지 않는다는 뜻이다."
        )
    return best


def build() -> list[Question]:
    questions: list[Question] = []
    for item in spec.QUESTIONS:
        spans = [
            GoldSpan(
                doc=doc,
                snippet=snippet_for(doc, page, anchor, f"{item['id']} {doc} p{page}"),
            )
            for doc, page, anchor in item["gold"]
        ]
        questions.append(
            Question(
                id=item["id"],
                question=item["q"],
                type=item["type"],
                gold_spans=spans,
                reference_answer=item["reference"],
                difficulty=item["difficulty"],
                source=SOURCE,
                split="tune",  # assign_splits 가 마지막에 다시 매긴다
                verified_at=VERIFIED_AT,
            )
        )
    return questions


def main() -> None:
    write(_HERE / OUT, assign_splits(build()))


if __name__ == "__main__":
    main()
