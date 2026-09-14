"""Golden-set datasets: the jsonl format, its loader, and its validator.

One question per line. The format is fixed by the M7 W1 design note; see
``FORMAT.md`` next to this file for the field-by-field spec and the rules a
question has to satisfy.

The loader is deliberately strict — an unknown question type or an
out-of-range snippet is an error, not a warning. 관대한 로더는 오타 하나로
``freshness`` 문항 일곱 개를 통째로 집계에서 빠뜨리고도 초록으로 통과한다.
그러면 지표는 멀쩡해 보이는데 실제로 재는 대상이 달라져 있다.

A dataset is identified by ``name`` (the file stem) and ``sha256`` (of the raw
bytes). 두 실행을 비교할 때 데이터셋 해시가 다르면 지표 차이는 검색이 바뀐
것인지 질문이 바뀐 것인지 구분할 수 없다 — diff 가 이 해시를 보고 막는다.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

# W1 이 정한 유형 분류. 이름을 여기서 한 번만 정의해 데이터셋·러너·리포트가
# 같은 문자열을 쓰게 한다.
TYPES = ("single_fact", "multi_doc", "rule_exception", "no_answer", "freshness")

# M7 개요 표는 같은 유형을 다른 이름으로 부른다("문서에 없음", "최신성").
# 두 이름이 돌아다니는 동안 데이터셋이 조용히 갈라지지 않게 별칭을 받아
# 정규 이름으로 접어 넣는다.
TYPE_ALIASES = {"unanswerable": "no_answer", "recency": "freshness"}

SPLITS = ("tune", "holdout")
DIFFICULTIES = ("easy", "medium", "hard")

# L1 은 검색만 잰다. 답이 문서에 없는 문항에는 정답 청크가 없으므로 여기서
# 점수를 매길 대상이 아니다 — 거부 정확도는 L2 judge 의 몫이다(W1).
L1_EXCLUDED_TYPES = ("no_answer",)

# 스니펫 길이 한계. 짧으면 여러 곳에 걸리고, 길면 청크 경계를 쉽게 넘는다.
SNIPPET_MIN = 20
SNIPPET_MAX = 80


class DatasetError(ValueError):
    """A dataset file violated the format spec."""


@dataclass(frozen=True)
class GoldSpan:
    doc: str
    snippet: str


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    type: str
    gold_spans: list[GoldSpan]
    reference_answer: str
    difficulty: str
    source: str
    split: str
    verified_at: str

    @property
    def scored_in_l1(self) -> bool:
        return self.type not in L1_EXCLUDED_TYPES

    @property
    def docs(self) -> list[str]:
        seen: list[str] = []
        for span in self.gold_spans:
            if span.doc not in seen:
                seen.append(span.doc)
        return seen


@dataclass(frozen=True)
class Dataset:
    name: str
    path: Path
    sha256: str
    questions: list[Question] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.questions)

    @property
    def docs(self) -> list[str]:
        seen: list[str] = []
        for question in self.questions:
            for doc in question.docs:
                if doc not in seen:
                    seen.append(doc)
        return sorted(seen)

    def scored(self) -> list[Question]:
        return [q for q in self.questions if q.scored_in_l1]

    def type_counts(self) -> dict[str, int]:
        counts = {t: 0 for t in TYPES}
        for question in self.questions:
            counts[question.type] += 1
        return counts

    def split_counts(self) -> dict[str, int]:
        counts = {s: 0 for s in SPLITS}
        for question in self.questions:
            counts[question.split] += 1
        return counts


_REQUIRED = (
    "id",
    "question",
    "type",
    "gold_spans",
    "reference_answer",
    "difficulty",
    "source",
    "split",
    "verified_at",
)


def _fail(path: Path, line_no: int, message: str) -> None:
    raise DatasetError(f"{path}:{line_no}: {message}")


def parse_question(row: dict, path: Path, line_no: int) -> Question:
    missing = [key for key in _REQUIRED if key not in row]
    if missing:
        _fail(path, line_no, f"필수 필드 누락: {', '.join(missing)}")

    qtype = TYPE_ALIASES.get(row["type"], row["type"])
    if qtype not in TYPES:
        _fail(path, line_no, f"알 수 없는 유형 {row['type']!r} (허용: {', '.join(TYPES)})")
    if row["split"] not in SPLITS:
        _fail(path, line_no, f"알 수 없는 split {row['split']!r}")
    if row["difficulty"] not in DIFFICULTIES:
        _fail(path, line_no, f"알 수 없는 난이도 {row['difficulty']!r}")
    if not str(row["question"]).strip():
        _fail(path, line_no, "question 이 비어 있다")

    spans_raw = row["gold_spans"]
    if not isinstance(spans_raw, list):
        _fail(path, line_no, "gold_spans 는 배열이어야 한다")
    # "문서에 없음" 문항은 빈 배열이 정답이고, 나머지는 비어 있으면 채점할
    # 근거가 없다. 둘을 뒤집어 쓰면 한쪽은 만점, 한쪽은 0점으로 굳는다.
    if qtype in L1_EXCLUDED_TYPES and spans_raw:
        _fail(path, line_no, f"{qtype} 문항에는 gold_spans 가 없어야 한다")
    if qtype not in L1_EXCLUDED_TYPES and not spans_raw:
        _fail(path, line_no, f"{qtype} 문항에 gold_spans 가 비어 있다")

    spans: list[GoldSpan] = []
    for span in spans_raw:
        if not isinstance(span, dict) or "doc" not in span or "snippet" not in span:
            _fail(path, line_no, "gold_spans 항목은 {doc, snippet} 이어야 한다")
        snippet = span["snippet"]
        length = len(" ".join(str(snippet).split()))
        if not SNIPPET_MIN <= length <= SNIPPET_MAX:
            _fail(
                path,
                line_no,
                f"스니펫 길이 {length}자 — {SNIPPET_MIN}~{SNIPPET_MAX}자여야 한다: "
                f"{str(snippet)[:40]!r}",
            )
        spans.append(GoldSpan(doc=str(span["doc"]), snippet=str(snippet)))

    return Question(
        id=str(row["id"]),
        question=str(row["question"]),
        type=qtype,
        gold_spans=spans,
        reference_answer=str(row["reference_answer"]),
        difficulty=str(row["difficulty"]),
        source=str(row["source"]),
        split=str(row["split"]),
        verified_at=str(row["verified_at"]),
    )


def load(path: str | Path) -> Dataset:
    """Read and validate a jsonl dataset. Raises ``DatasetError`` on any fault."""
    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()

    questions: list[Question] = []
    seen: set[str] = set()
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            _fail(path, line_no, f"JSON 파싱 실패: {exc}")
        question = parse_question(row, path, line_no)
        if question.id in seen:
            _fail(path, line_no, f"질문 id 중복: {question.id}")
        seen.add(question.id)
        questions.append(question)

    if not questions:
        raise DatasetError(f"{path}: 문항이 하나도 없다")

    return Dataset(name=path.stem, path=path, sha256=digest, questions=questions)


def dumps(question: Question) -> str:
    """Serialize one question back to its canonical single-line form."""
    return json.dumps(
        {
            "id": question.id,
            "question": question.question,
            "type": question.type,
            "gold_spans": [
                {"doc": s.doc, "snippet": s.snippet} for s in question.gold_spans
            ],
            "reference_answer": question.reference_answer,
            "difficulty": question.difficulty,
            "source": question.source,
            "split": question.split,
            "verified_at": question.verified_at,
        },
        ensure_ascii=False,
    )
