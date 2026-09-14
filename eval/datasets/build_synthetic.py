"""Regenerate the synthetic jsonl datasets from ``eval/corpora``.

    python -m eval.datasets.build_synthetic

The jsonl files are committed — this script exists so the conversion is
reviewable and repeatable, not so the files are generated on the fly. 손으로
36줄을 옮겨 적으면 스니펫 하나가 조용히 어긋나도 아무도 모른다. 여기서는
모든 스니펫이 고유한지(W1 의 3곳 이하 규칙)를 생성 시점에 확인하고, 어긋나면
파일을 쓰지 않고 죽는다.

**유형 매핑은 자동이 아니다.** M4 골든셋의 유형(factual/numeric/multi_hop/
unanswerable)과 M7 W1 의 유형(single_fact/multi_doc/rule_exception/no_answer/
freshness)은 재려는 것이 다르다. 기계적으로 factual→single_fact 로 접으면
"예외 규칙을 정확히 짚는가"를 재는 문항이 전부 기본 검색 문항으로 뭉개진다.
그래서 예외 성격의 문항은 아래 ``_RULE_EXCEPTIONS`` 에 질문 텍스트로 명시해
둔다 — 판단이 코드에 남아야 나중에 뒤집을 수 있다.

**freshness 는 0문항이다.** 합성 코퍼스에는 개정 이력도 버전도 없어서 "오래된
값을 물고 오는가"를 잴 대상 자체가 없다. 없는 유형을 억지로 채우면 그 7문항이
freshness 를 측정한다고 **거짓으로** 보고하게 된다. 실제 업무 문서 골든셋이
private repo 에서 들어올 때 채워질 칸으로 비워둔다.
"""

import hashlib
import re
from dataclasses import replace
from pathlib import Path

from eval.corpora import golden, hard
from eval.datasets import SNIPPET_MAX, SNIPPET_MIN, Question, GoldSpan, dumps
from eval.datasets import synthetic
from eval.gold import match_count, normalize

_HERE = Path(__file__).resolve().parent
VERIFIED_AT = "2026-09-14"

# "제12조 (보안) " 같은 머리말은 어느 조항에나 있고 질문의 답을 담고 있지
# 않다. 스니펫은 답이 실제로 적힌 부분이어야 한다.
_HEADING = re.compile(r"^제\d+조\s*\([^)]*\)\s*")

# 예외·단서를 정확히 짚는지를 보는 문항. 질문 텍스트로 적는 이유는 인덱스로
# 적으면 골든셋에 문항이 하나 끼는 순간 전부 밀려서 조용히 틀리기 때문이다.
_RULE_EXCEPTIONS = {
    "보일러가 고장나면 누가 고치나요?",       # 임대인/임차인 분담의 경계
    "강아지를 키울 수 있나요?",               # 10kg 이하 1마리라는 단서
    "다른 사람에게 세를 놓을 수 있나요?",     # 서면 동의라는 예외
    "아무 통지도 안 하면 계약은 어떻게 되나요?",  # 묵시적 갱신
    "음주운전 사고도 보상되나요?",            # 면책사유
    "치과 치료는 언제부터 보장되나요?",       # 일반 보장개시와 다른 대기기간
    "환불 조건이 어떻게 되나요?",             # 미사용 7일이라는 단서
}

# 유형별 난이도. 한 문항씩 손으로 매기는 것이 정직하지만, 합성 문항에는
# 구분할 근거가 없다 — 유형이 곧 난이도라는 사실을 그대로 적는다.
_DIFFICULTY = {
    "single_fact": "easy",
    "rule_exception": "medium",
    "multi_doc": "hard",
    "no_answer": "medium",
}


def _candidates(body: str) -> list[str]:
    """Snippet candidates for a clause, best first.

    앞에서 자른 것을 먼저 본다 — 사실이 조항 앞쪽에 있는 경우가 많고, 그
    구간이 고유하면 더 볼 필요가 없다. 고유하지 않으면(hard 코퍼스의 요금
    조항들처럼 앞부분이 서로 같다) 뒤쪽 창으로 옮겨 간다.
    """
    words = body.split()
    out: list[str] = []

    def window(chunk: list[str]) -> str | None:
        text = " ".join(chunk)
        return text if SNIPPET_MIN <= len(text) <= SNIPPET_MAX else None

    for start in range(len(words)):
        chunk: list[str] = []
        for word in words[start:]:
            chunk.append(word)
            if len(" ".join(chunk)) > SNIPPET_MAX:
                chunk.pop()
                break
        candidate = window(chunk)
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def snippet_for(page_text: str, pages: list[str], where: str) -> str:
    """Pick a unique 20-80 char snippet from one page of a document."""
    body = _HEADING.sub("", normalize(page_text)) or normalize(page_text)
    for candidate in _candidates(body):
        if match_count(candidate, pages) == 1:
            return candidate
    raise RuntimeError(
        f"{where}: 고유한 스니펫을 못 만들었다 — 조항이 다른 쪽과 구분되지 않는다: "
        f"{body[:60]!r}"
    )


HOLDOUT_RATIO = 0.3


def assign_splits(questions: list[Question]) -> list[Question]:
    """Label 70% tune / 30% holdout, keeping every type's ratio.

    난수를 쓰지 않는다. 같은 데이터셋을 다시 만들 때 split 이 바뀌면 holdout
    이 오염되고(W8 에서만 열어보기로 한 약속이 깨진다), 그 오염은 지표 변화로
    위장돼 나타난다 — 검색이 좋아진 게 아니라 쉬운 문항이 holdout 으로 옮겨
    간 것일 수 있는데 구분할 방법이 없다.

    유형 안에서 id 해시 순으로 줄을 세우고 뒤쪽 30% 를 holdout 으로 둔다.
    해시 임계값(예: h >= 0.7)으로 정하지 않는 이유는 문항 수가 적을 때 비율이
    크게 흔들리기 때문이다 — 20문항짜리 셋에서 실제로 55/45 가 나왔다.
    """
    by_type: dict[str, list[Question]] = {}
    for question in questions:
        by_type.setdefault(question.type, []).append(question)

    holdout: set[str] = set()
    for qtype, group in by_type.items():
        ordered = sorted(
            group,
            key=lambda q: hashlib.sha256(f"{qtype}:{q.id}".encode()).hexdigest(),
        )
        count = round(len(ordered) * HOLDOUT_RATIO)
        holdout.update(q.id for q in ordered[len(ordered) - count :])

    return [
        replace(q, split="holdout" if q.id in holdout else "tune") for q in questions
    ]


def _question(
    qid: str,
    text: str,
    qtype: str,
    spans: list[GoldSpan],
    reference: str,
    source: str,
) -> Question:
    return Question(
        id=qid,
        question=text,
        type=qtype,
        gold_spans=spans,
        reference_answer=reference,
        difficulty=_DIFFICULTY[qtype],
        source=source,
        split="tune",  # assign_splits 가 마지막에 다시 매긴다
        verified_at=VERIFIED_AT,
    )


def build_golden() -> list[Question]:
    source = "eval/corpora/golden.py — M4 합성 골든셋(임대차·보험·SaaS) 변환"
    questions: list[Question] = []
    for i, item in enumerate(golden.QUESTIONS, start=1):
        qid = f"syn-golden-{i:03d}"
        doc = item["doc"]
        pages = synthetic.pages_for(doc)

        if item["type"] == "unanswerable":
            qtype = "no_answer"
        elif item["q"] in _RULE_EXCEPTIONS:
            qtype = "rule_exception"
        elif len(item["gold_pages"]) > 1:
            # M4 의 multi_hop 은 **한 문서 안의 두 조항**을 합치는 문항이다.
            # W1 의 multi_doc 은 여러 문서를 종합하는 문항이라 엄밀히는 다르다.
            # 그래도 multi_doc 으로 싣는 이유는, L1 에서 두 유형이 요구하는
            # 계산이 같기 때문이다 — 정답 스팬이 여럿일 때의 macro recall 과
            # nDCG. 이 데이터셋은 그 경로를 검증하고, 문서 간 혼동 자체는
            # 업무 문서 골든셋이 들어와야 측정된다.
            qtype = "multi_doc"
        else:
            qtype = "single_fact"

        spans = [
            GoldSpan(
                doc=doc,
                snippet=snippet_for(pages[page - 1], pages, f"{qid} p{page}"),
            )
            for page in item["gold_pages"]
        ]
        questions.append(
            _question(qid, item["q"], qtype, spans, item["reference"], source)
        )
    return questions


def build_hard() -> list[Question]:
    """The confusable-fee corpus: 40 near-identical clauses, 20 queries.

    전부 single_fact 다. 이 코퍼스가 재는 것은 유형 분포가 아니라 **혼동
    후보 속에서 정답 하나를 집어내는가**이고, 그래서 난이도만 hard 로 올린다.
    다른 유형을 여기서 억지로 만들면 라벨이 내용과 어긋난다.
    """
    source = "eval/corpora/hard.py — 혼동 요금 조항 40쪽 코퍼스 변환"
    pages = synthetic.pages_for("fees")
    questions: list[Question] = []
    for i, (text, gold_page, channel) in enumerate(hard.QUERIES, start=1):
        qid = f"syn-hard-{i:03d}"
        span = GoldSpan(
            doc="fees",
            snippet=snippet_for(pages[gold_page - 1], pages, f"{qid} p{gold_page}"),
        )
        questions.append(
            Question(
                id=qid,
                question=text,
                type="single_fact",
                gold_spans=[span],
                # 기준 답변은 조항 원문 그대로다. 이 코퍼스에는 사람이 쓴
                # 답이 없고, 지어내면 L2 judge 가 나중에 그 거짓을 기준으로
                # 채점하게 된다.
                reference_answer=normalize(pages[gold_page - 1]),
                difficulty="hard",
                source=f"{source} ({channel} 채널 질의)",
                split="tune",  # assign_splits 가 마지막에 다시 매긴다
                verified_at=VERIFIED_AT,
            )
        )
    return questions


def write(path: Path, questions: list[Question]) -> None:
    path.write_text(
        "\n".join(dumps(q) for q in questions) + "\n", encoding="utf-8"
    )
    print(f"{path.name}: {len(questions)}문항")


def main() -> None:
    for name, questions in (
        ("synthetic_golden.jsonl", build_golden()),
        ("synthetic_hard.jsonl", build_hard()),
    ):
        write(_HERE / name, assign_splits(questions))


if __name__ == "__main__":
    main()
