"""M7 L2 answer-quality judging: the rubric, the record shape, and Cohen's kappa.

**No model is called from this file.** Everything here is a pure function over
values, so the rubric, the record format, the comparability rule and the kappa
arithmetic can all be tested without a GPU, a network, or a judge model — the
same split ``eval/report.py`` makes for L1.

Three things this module exists to enforce, each from the M7 W6 design note:

``rubric_version`` + ``rubric_sha256``
    A rubric change makes old L2 numbers incomparable. 사람이 버전 문자열
    올리는 것을 잊을 수 있으므로 루브릭 본문의 sha256 을 함께 기록하고, 둘 중
    **하나라도** 다르면 비교를 거부한다. W3 이 ``dataset_sha256`` 으로 하는 것과
    같은 장치다 — 측정 기준이 달라진 것을 품질 변화로 읽는 순간 리포트는
    거짓말이 된다.

Evidence before scores
    판정 JSON 은 차원마다 근거를 먼저 쓰고 점수를 나중에 쓴다. 스키마의 키
    순서가 곧 생성 순서라 모델이 점수를 먼저 뱉고 근거를 사후에 지어내는 것을
    구조로 막는다.

The ``unvalidated`` mark
    사람 라벨과의 Cohen's kappa 가 아직 없다. W6 노트의 표현대로 **보정 없는
    judge 점수는 장식**이므로, 이 상태에서 나온 수치는 집계 키 이름부터
    ``groundedness__unvalidated`` 처럼 달라진다. 키가 달라야 나중에 누군가
    "groundedness 0.82" 라고 인용할 때 그 문자열이 어디에도 없다.
"""

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- 루브릭 ---

# 사람이 읽는 버전. 루브릭 본문을 고치면 **반드시** 올린다. 잊어도 아래
# rubric_sha256() 이 잡지만, 잡힌 뒤에 사람이 고칠 것은 결국 이 문자열이다.
#
# 이력 — 버전이 올라간 이유를 여기 남긴다. W6 의 함정("루브릭을 중간에 고치면
# 이전 L2 수치와 비교 불가")은 고치지 말라는 뜻이 아니라 **언제 왜 고쳤는지
# 남기라는 뜻**이다. 이력이 없으면 두 수치가 왜 비교 불가인지도 설명 못 한다.
#
#   l2-ko-v1  2026-09-14  최초.
#   l2-ko-v2  2026-09-14  4문항 동작 확인에서 드러난 두 결함을 고쳤다.
#     ① 거부 답변의 groundedness 가 0 으로 나왔다. "찾을 수 없습니다"는 아무
#        사실도 주장하지 않으므로 근거 없는 주장이 아니다. v1 의 0점 설명이
#        "확인되는 주장이 사실상 없다"여서 거부가 그대로 걸렸고, 거부를 잘한
#        문항이 groundedness 평균을 끌어내리고 있었다.
#     ② 답을 회피한 답변에 correctness 3점이 나왔다. 담은 사실이 하나도 없는데
#        틀린 사실도 없다는 이유로 만점이 됐다. 0점 설명을 명시적으로 고쳤다.
#     ③ 답할 수 있는 질문에 (틀린) 답을 한 경우 refusal_accuracy 가 2점이었다.
#        판정자가 답의 품질을 거부 판단에 섞어 넣었다는 뜻이라, 같은 잘못을 두
#        차원에서 두 번 세지 말라고 명시했다.
RUBRIC_VERSION = "l2-ko-v2"

# M7 부모 페이지의 L2 정의: groundedness · 정답성 · "모른다" 정확도.
DIMENSIONS = ("groundedness", "correctness", "refusal_accuracy")

# 0~3 순서형. 이분(0/1)으로 두지 않은 이유는 "부분적으로 근거 있음"과
# "지어냄"이 같은 점수로 뭉개지면 M4 부터 써 온 진단 — 검색 실패인가 생성
# 실패인가 — 을 못 하기 때문이다. 반대로 5점 이상으로 늘리지 않은 이유는
# 사람 라벨 30~50건으로 kappa 를 낼 때 범주가 많을수록 우연 일치가 줄어드는
# 만큼 각 범주의 표본도 줄어, 신뢰구간이 결론을 못 낼 만큼 넓어지기 때문이다.
SCALE_MIN = 0
SCALE_MAX = 3
SCALE = tuple(range(SCALE_MIN, SCALE_MAX + 1))

# 차원별 점수 기준. "좋은 답을 안다"고 가정하지 않는다는 W6 원칙에 따라, 각
# 점수가 무엇인지를 판정자가 추론할 필요 없이 **글로** 적는다. 이 문자열은
# 프롬프트에 그대로 실리고 sha256 에도 그대로 들어간다.
RUBRIC: dict[str, dict] = {
    "groundedness": {
        "question": "답변의 내용이 제공된 컨텍스트로 확인되는가?",
        "note": (
            "사실 여부가 아니라 **근거 여부**를 본다. 세상에서 참인 문장이라도 "
            "컨텍스트에 없으면 점수를 깎는다. 반대로 컨텍스트가 틀렸더라도 "
            "답변이 그 컨텍스트를 충실히 따랐다면 여기서는 감점하지 않는다 "
            "— 그건 correctness 가 볼 몫이다. "
            "답변이 거부이고(예: '제공된 문서에서 찾을 수 없습니다') 아무 사실도 "
            "주장하지 않았다면 **3점**이다 — 없는 주장은 근거 없는 주장이 아니다. "
            "거부하면서 근거 없는 사실을 덧붙였다면 그 덧붙인 부분으로 판정한다."
        ),
        "anchors": {
            3: "답변의 모든 주장이 컨텍스트 문장으로 직접 확인된다. 덧붙인 사실이 없다. 주장 자체를 하지 않은 거부도 여기다.",
            2: "핵심 주장은 컨텍스트로 확인되나, 컨텍스트에 없는 부수적 설명·일반론이 한두 문장 섞였다.",
            1: "일부만 컨텍스트에서 왔고, 답의 중심이 되는 주장 중 하나 이상이 컨텍스트에 없다.",
            0: "컨텍스트에 없는 사실을 주장했다. 지어냈거나 다른 문서 이야기를 한다.",
        },
    },
    "correctness": {
        "question": "답변이 기준답변에 비추어 사실로 맞는가?",
        "note": (
            "기준답변에 담긴 사실을 기준으로 본다. 표현·어순·분량이 달라도 "
            "사실이 같으면 감점하지 않는다. 기준답변에 없는 내용을 더 말한 "
            "것은, 그것이 틀리지 않았다면 감점하지 않는다. **수치·날짜·고유명사가 "
            "다르면 그것만으로 1점 이하다** — 이 제품에서 틀린 금액은 틀린 답이다. "
            "답변이 거부라서 기준답변의 사실을 **하나도 담지 못했다면 0점**이다. "
            "틀린 사실이 없다는 이유로 만점을 주지 않는다 — 여기서 재는 것은 "
            "'틀리지 않았는가'가 아니라 '맞는 사실을 담았는가'다. 거부 자체가 "
            "옳았는지는 refusal_accuracy 가 따로 본다."
        ),
        "anchors": {
            3: "기준답변의 사실을 모두 담았고 틀린 사실이 없다.",
            2: "핵심 사실은 맞으나 기준답변의 사실 중 일부가 빠졌다. 틀린 사실은 없다.",
            1: "맞는 부분과 틀린 부분이 섞여 있다. 수치·날짜·고유명사 중 하나가 틀렸다면 최대 1점이다.",
            0: "기준답변과 사실이 어긋난다. 또는 답을 회피·거부해 기준답변의 사실을 하나도 담지 못했다.",
        },
    },
    "refusal_accuracy": {
        "question": "거부/응답의 선택이 옳았는가?",
        "note": (
            "답의 품질이 아니라 **답할지 말지의 판단**만 본다. 문서에 근거가 "
            "없는 질문(no_answer)에는 거부가 정답이고, 근거가 있는 질문에는 "
            "응답이 정답이다. 컨텍스트가 비어 있는데도 답을 지어냈다면 0점이다. "
            "**답의 내용이 틀렸더라도 '답하기로 한 판단' 자체가 옳았다면 3점이다** "
            "— 틀린 내용은 correctness 가 이미 벌점을 줬다. 여기서 또 깎으면 같은 "
            "잘못을 두 번 세게 되고, 두 차원이 같은 것을 재게 된다."
        ),
        "anchors": {
            3: "판단이 옳고 명확하다. 답할 수 있는 질문에 거부 없이 답했거나, 근거 없는 질문을 분명히 거부했다.",
            2: "판단은 옳으나 흐릿하다. 답하면서 불필요하게 '확실하지 않다'를 덧붙였거나, 거부하면서 근거 없는 추측을 곁들였다.",
            1: "판단이 부분적으로 틀렸다. 답할 수 있는 질문의 일부만 답하고 나머지를 거부했거나, 거부하면서도 근거 없는 답을 흘렸다.",
            0: "판단이 반대다. 답할 수 있는 질문을 거부했거나, 근거 없는 질문에 근거 없이 단언했다.",
        },
    },
}

# 해당 없음. 모든 문항에서 세 차원이 다 성립하지는 않는다 — 예를 들어
# no_answer 문항에는 담아야 할 사실이 없으므로 correctness 는 잴 것이 없다.
# 그때 0 을 주면 "거부를 잘한 문항"이 정답성 0점으로 평균을 끌어내린다.
# 없는 측정은 0 이 아니라 없음이어야 한다(eval/judge.py 의 _ratio 와 같은 판단).
NOT_APPLICABLE = None


def applicable_dimensions(question_type: str) -> tuple[str, ...]:
    """Which rubric dimensions mean anything for this question type.

    ``no_answer`` 문항은 기준답변이 "문서에 없다"이므로 정답성을 점수로 잴
    대상이 아니다. groundedness 는 남긴다 — 거부해야 할 질문에 답을 지어낸
    경우가 바로 groundedness 0 이고, 그 신호를 버리면 안 된다.
    """
    if question_type == "no_answer":
        return ("groundedness", "refusal_accuracy")
    return DIMENSIONS


def rubric_text() -> str:
    """The rubric exactly as it goes into the prompt.

    프롬프트에 실리는 문자열과 해시되는 문자열이 같아야 한다. 둘을 따로 만들면
    프롬프트만 고치고 해시는 그대로인 상태가 생기고, 그 순간 버전 태그는
    아무것도 보증하지 않는다.
    """
    lines = [f"[루브릭 {RUBRIC_VERSION}] 점수는 {SCALE_MIN}~{SCALE_MAX} 정수다.", ""]
    for name in DIMENSIONS:
        spec = RUBRIC[name]
        lines.append(f"## {name} — {spec['question']}")
        lines.append(spec["note"])
        for score in sorted(spec["anchors"], reverse=True):
            lines.append(f"  {score}점: {spec['anchors'][score]}")
        lines.append("")
    lines.append(
        "잴 차원은 문항마다 아래에서 지정된다. 지정된 차원에는 반드시 "
        f"{SCALE_MIN}~{SCALE_MAX} 정수를 준다. 애매하다고 비우지 말고 루브릭에서 "
        "가장 가까운 점수를 고른다. 어느 차원을 뺄지는 판정자가 정하지 않는다 — "
        "잴 수 없는 차원은 애초에 지정되지 않는다."
    )
    return "\n".join(lines)


def rubric_sha256() -> str:
    """Digest of the rubric body — the tamper-evident half of the version tag.

    사람이 버전 문자열을 올리는 것을 잊어도 이 값은 반드시 달라진다. 비교는
    둘 다 본다.
    """
    return hashlib.sha256(rubric_text().encode("utf-8")).hexdigest()


# --- 검증 상태 ---

# 사람 라벨과의 kappa 가 없는 상태. W6 노트: "보정 없이 judge 점수를 쓰면 그
# 점수는 장식이다." 장식을 수치처럼 인용하지 못하게 하는 것이 아래 세 장치다.
UNVALIDATED = "unvalidated"
VALIDATED = "validated"

# W6 의 채택선. 0.4 미만이면 루브릭이 모호한 것이고, 0.6 이상이면 채택·잠금.
KAPPA_REWRITE_BELOW = 0.4
KAPPA_ADOPT_AT = 0.6

UNVALIDATED_WHY = (
    "사람 라벨 30~50건과의 Cohen's kappa 를 아직 재지 않았다. "
    "이 수치는 judge 모델의 의견이며 품질 주장으로 인용할 수 없다."
)

BANNER = (
    "⚠️  UNVALIDATED — 이 L2 수치는 사람 라벨과 대조되지 않았다.\n"
    "    judge-사람 일치도(Cohen's kappa)가 없으므로 품질 주장의 근거가 아니다.\n"
    "    보정 절차: python -m eval.l2_run labels ... 로 라벨 서식을 뽑고,\n"
    "    직접 라벨링한 뒤 python -m eval.l2_run kappa ... 로 일치도를 낸다.\n"
    f"    채택선 kappa >= {KAPPA_ADOPT_AT}, kappa < {KAPPA_REWRITE_BELOW} 면 루브릭 재작성(W6)."
)


@dataclass(frozen=True)
class Validation:
    """Whether this judge's numbers have been checked against a human."""

    status: str = UNVALIDATED
    kappa: float | None = None
    kappa_weights: str | None = None
    kappa_n: int = 0
    checked_at: str | None = None
    why: str = UNVALIDATED_WHY

    @property
    def is_validated(self) -> bool:
        return self.status == VALIDATED

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "kappa": self.kappa,
            "kappa_weights": self.kappa_weights,
            "kappa_n": self.kappa_n,
            "checked_at": self.checked_at,
            "why": self.why,
        }


def metric_key(dimension: str, validation: Validation | None = None) -> str:
    """Aggregate key for a dimension, carrying its validation state in the name.

    검증 전에는 키가 ``groundedness__unvalidated`` 다. 표식을 문서에 적는 것으로
    끝내면 표를 복사해 가는 순간 사라지지만, 키 이름에 박아 두면 그 수치를
    ``groundedness`` 라고 부르는 문장은 어느 파일에도 존재하지 않게 된다.
    """
    if validation is not None and validation.is_validated:
        return dimension
    return f"{dimension}__{UNVALIDATED}"


# --- 판정 레코드 ---


@dataclass
class DimensionVerdict:
    """One dimension's judgement: the evidence, then the score."""

    dimension: str
    evidence: str
    score: int | None

    def to_dict(self) -> dict:
        # 키 순서도 근거 먼저다. 저장된 JSON 을 사람이 읽을 때도 같은 순서로
        # 읽히는 편이 낫다.
        return {
            "dimension": self.dimension,
            "evidence": self.evidence,
            "score": self.score,
        }


@dataclass
class L2Record:
    """One question judged once, in the shape a human label can be joined to.

    조인 키는 ``(question_id, variant, dimension)`` 이고, 그 옆에
    ``rubric_version`` · ``rubric_sha256`` 이 붙는다. 라벨링 UI 는 지금 만들지
    않는다 — 사람이 labels jsonl 을 채워 넣기만 하면 kappa 가 나오도록
    **조인 가능성만** 갖춰 둔다(W6, kappa 검증 보류 결정).

    ``variant`` 는 W6 의 나머지 절반(서버 생성 vs 클라이언트 생성)을 위한
    자리다. 비교 대상이 없는 지금도 값을 비워 두지 않고 "server" 를 적는
    이유는, 나중에 두 번째 값이 생겼을 때 기존 레코드가 어느 쪽이었는지
    복원 불가능해지는 것이 traces.source 에서 이미 겪은 사고이기 때문이다.
    """

    question_id: str
    question_type: str
    variant: str
    rubric_version: str
    rubric_sha256: str
    judge_provider: str
    judge_model: str
    verdicts: list[DimensionVerdict] = field(default_factory=list)
    validation: Validation = field(default_factory=Validation)
    order: str = "single"
    latency_ms: int = 0
    created_at: str | None = None
    question: str | None = None
    note: str | None = None

    def scores(self) -> dict[str, int | None]:
        return {v.dimension: v.score for v in self.verdicts}

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "question_type": self.question_type,
            "variant": self.variant,
            "rubric_version": self.rubric_version,
            "rubric_sha256": self.rubric_sha256,
            "judge_provider": self.judge_provider,
            "judge_model": self.judge_model,
            "order": self.order,
            "verdicts": [v.to_dict() for v in self.verdicts],
            "validation": self.validation.to_dict(),
            "latency_ms": self.latency_ms,
            "created_at": self.created_at,
            "question": self.question,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "L2Record":
        validation = Validation(**raw.get("validation", {}))
        return cls(
            question_id=raw["question_id"],
            question_type=raw["question_type"],
            variant=raw.get("variant", "server"),
            rubric_version=raw["rubric_version"],
            rubric_sha256=raw["rubric_sha256"],
            judge_provider=raw.get("judge_provider", "?"),
            judge_model=raw.get("judge_model", "?"),
            verdicts=[DimensionVerdict(**v) for v in raw.get("verdicts", [])],
            validation=validation,
            order=raw.get("order", "single"),
            latency_ms=raw.get("latency_ms", 0),
            created_at=raw.get("created_at"),
            question=raw.get("question"),
            note=raw.get("note"),
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_records(path, records: list[L2Record]) -> None:
    from pathlib import Path

    Path(path).write_text(
        "\n".join(json.dumps(r.to_dict(), ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def read_records(path) -> list[L2Record]:
    from pathlib import Path

    out: list[L2Record] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(L2Record.from_dict(json.loads(line)))
    return out


# --- 집계 ---


def aggregate(records: list[L2Record]) -> dict[str, float | None]:
    """Mean score per dimension, over the records where that dimension applied.

    None(해당 없음)은 분모에서 뺀다. 0 으로 접으면 잴 수 없던 문항이 점수를
    깎아, no_answer 문항이 많은 데이터셋일수록 정답성이 낮게 보인다.
    """
    validation = records[0].validation if records else None
    out: dict[str, float | None] = {}
    for dimension in DIMENSIONS:
        values = [
            v.score
            for r in records
            for v in r.verdicts
            if v.dimension == dimension and v.score is not None
        ]
        key = metric_key(dimension, validation)
        out[key] = sum(values) / len(values) if values else None
    return out


def group_by_type(records: list[L2Record]) -> dict[str, dict[str, float | None]]:
    groups: dict[str, list[L2Record]] = {}
    for record in records:
        groups.setdefault(record.question_type, []).append(record)
    return {name: aggregate(rows) for name, rows in sorted(groups.items())}


# --- 비교 가능성 ---


class IncomparableRubrics(ValueError):
    """Two L2 runs were scored under rubrics that are not the same rubric."""


def check_comparable(base: list[L2Record], head: list[L2Record]) -> None:
    """Refuse to compare L2 numbers produced under different rubrics or judges.

    W6 의 함정 그대로다 — "루브릭을 중간에 고치면 이전 L2 수치와 비교 불가."
    버전 문자열과 본문 해시를 **둘 다** 본다. 문자열만 보면 고치고 안 올린
    경우를 놓치고, 해시만 보면 리포트에 찍을 이름이 없다.

    judge 모델도 본다. 같은 루브릭이라도 다른 모델은 다른 채점자이고, 두
    채점자의 평균 차이를 품질 변화로 읽는 것이 이 함수가 막으려는 바로 그
    사고다.
    """
    if not base or not head:
        raise IncomparableRubrics("비교할 레코드가 비어 있다")

    b, h = base[0], head[0]
    if b.rubric_version != h.rubric_version:
        raise IncomparableRubrics(
            f"루브릭 버전이 다르다: base={b.rubric_version} head={h.rubric_version}. "
            "루브릭이 바뀌면 이전 L2 수치와 비교할 수 없다 — base 를 새 루브릭으로 "
            "다시 채점해야 한다."
        )
    if b.rubric_sha256 != h.rubric_sha256:
        raise IncomparableRubrics(
            f"루브릭 버전 문자열은 {b.rubric_version} 로 같은데 본문이 다르다 "
            f"({b.rubric_sha256[:12]} -> {h.rubric_sha256[:12]}). "
            "루브릭을 고치고 RUBRIC_VERSION 을 올리지 않았다."
        )
    if b.judge_model != h.judge_model:
        raise IncomparableRubrics(
            f"judge 모델이 다르다: base={b.judge_model} head={h.judge_model}. "
            "채점자가 바뀐 것을 품질 변화로 읽을 수 없다."
        )


# --- A/B 비교 (위치 편향) ---

# W6: "비교 평가는 A/B 순서를 바꿔 두 번 돌린다." 생성 위치 비교(서버 생성 vs
# 클라이언트 생성)는 app/mcp/ 를 건드려야 해서 뒤로 미뤘지만, 구조는 여기 있다.
# 이름이 아니라 **자리**를 반환한다 — 판정자는 "첫째"와 "둘째"만 보고, 어느
# 쪽이 우리 것인지 알 수 없어야 한다.
ORDERS = ("ab", "ba")


def order_positions(variant_a: str, variant_b: str, order: str) -> tuple[str, str]:
    """Which variant sits in the first and second slot for this ordering."""
    if order == "ab":
        return variant_a, variant_b
    if order == "ba":
        return variant_b, variant_a
    raise ValueError(f"알 수 없는 순서 {order!r} (허용: {', '.join(ORDERS)})")


@dataclass
class PairwiseOutcome:
    """One pairwise comparison judged in both orders.

    ``winner`` 는 두 판정이 **같은 변형**을 골랐을 때만 정해진다. 두 판정이 같은
    **자리**를 골랐다면 그것은 승부가 아니라 위치 편향이고, 그 문항은 결론에
    쓰지 않는다. 한 방향만 돌려 놓고 이 구분을 못 하는 것이 W6 이 경고한 바다.
    """

    question_id: str
    variant_a: str
    variant_b: str
    pick_ab: str
    pick_ba: str

    @property
    def winner(self) -> str | None:
        if self.pick_ab == self.pick_ba:
            return self.pick_ab
        return None

    @property
    def position_biased(self) -> bool:
        """Both runs picked the same slot, so the slot decided — not the answer."""
        if self.pick_ab == self.pick_ba:
            return False
        first_ab = self.variant_a
        first_ba = self.variant_b
        return self.pick_ab == first_ab and self.pick_ba == first_ba


def pairwise_summary(outcomes: list[PairwiseOutcome]) -> dict:
    """Wins per variant, plus how often position alone decided."""
    wins = Counter(o.winner for o in outcomes if o.winner is not None)
    biased = sum(1 for o in outcomes if o.position_biased)
    return {
        "n": len(outcomes),
        "decided": sum(wins.values()),
        "wins": dict(wins),
        "position_biased": biased,
        "position_bias_rate": biased / len(outcomes) if outcomes else 0.0,
    }


# --- Cohen's kappa ---

WEIGHTS = ("none", "linear", "quadratic")


class KappaUndefined(ValueError):
    """Kappa cannot be computed for these labels."""


def cohens_kappa(
    rater_a: list[int],
    rater_b: list[int],
    *,
    weights: str = "quadratic",
    categories: tuple[int, ...] = SCALE,
) -> float:
    """Cohen's kappa between two raters over an ordinal scale.

    ``weights="quadratic"`` 가 기본인 이유는 이 루브릭의 점수가 **순서형**이기
    때문이다. 가중 없는 kappa 는 3점을 2점으로 본 것과 3점을 0점으로 본 것을
    똑같은 한 번의 불일치로 센다. 그러면 "거의 같게 본다"와 "정반대로 본다"가
    한 숫자에 뭉개져, kappa 0.5 가 무엇을 뜻하는지 말할 수 없게 된다. 이차
    가중은 (i-j)^2 에 비례해 벌점을 주므로 인접 칸의 이견에 관대하고 반대편
    이견에 엄하다 — 순서형 루브릭에서 쓰는 표준(Cohen 1968)이다.

    다만 **가중 없는 kappa 도 함께 보고한다**(``kappa_all``). 이차 가중은
    점수가 한쪽으로 쏠린 표본에서 높게 나오는 성질이 있어, 그것만 인용하면
    실제보다 일치도가 좋아 보인다. W6 의 0.4/0.6 선이 어느 정의의 kappa 인지
    적어 두는 것이 리포트의 의무다.

    Raises ``KappaUndefined`` when the inputs cannot produce a number.
    """
    if weights not in WEIGHTS:
        raise ValueError(f"알 수 없는 가중 {weights!r} (허용: {', '.join(WEIGHTS)})")
    if len(rater_a) != len(rater_b):
        raise KappaUndefined(
            f"두 라벨의 길이가 다르다: {len(rater_a)} vs {len(rater_b)}"
        )
    n = len(rater_a)
    if n == 0:
        raise KappaUndefined("라벨이 하나도 없다")

    index = {c: i for i, c in enumerate(categories)}
    for value in (*rater_a, *rater_b):
        if value not in index:
            raise KappaUndefined(
                f"척도 밖의 값 {value!r} (허용: {', '.join(map(str, categories))})"
            )

    k = len(categories)
    observed = [[0.0] * k for _ in range(k)]
    for a, b in zip(rater_a, rater_b):
        observed[index[a]][index[b]] += 1.0

    rows = [sum(row) for row in observed]
    cols = [sum(observed[i][j] for i in range(k)) for j in range(k)]

    def penalty(i: int, j: int) -> float:
        if weights == "none":
            return 0.0 if i == j else 1.0
        distance = abs(categories[i] - categories[j])
        span = categories[-1] - categories[0]
        if span == 0:
            return 0.0
        if weights == "linear":
            return distance / span
        return (distance / span) ** 2

    disagreement_observed = sum(
        penalty(i, j) * observed[i][j] / n for i in range(k) for j in range(k)
    )
    disagreement_expected = sum(
        penalty(i, j) * (rows[i] / n) * (cols[j] / n) for i in range(k) for j in range(k)
    )

    if math.isclose(disagreement_expected, 0.0, abs_tol=1e-12):
        # 우연 불일치가 0 이라는 것은 두 라벨이 모두 한 범주에만 몰려 있다는
        # 뜻이다. 이때 kappa 는 0/0 이라 정의되지 않는다 — 1.0 으로 접으면
        # "전부 3점을 준 게으른 judge"가 완벽한 일치도를 얻는다.
        raise KappaUndefined(
            "우연 기대 불일치가 0 이다 — 두 라벨이 한 범주에만 몰려 있어 "
            "kappa 가 정의되지 않는다. 점수가 갈리는 표본이 필요하다."
        )
    return 1.0 - disagreement_observed / disagreement_expected


def kappa_all(
    rater_a: list[int],
    rater_b: list[int],
    *,
    categories: tuple[int, ...] = SCALE,
) -> dict[str, float | None]:
    """All three kappa definitions at once, so none of them can be cherry-picked."""
    out: dict[str, float | None] = {}
    for weight in WEIGHTS:
        try:
            out[weight] = cohens_kappa(
                rater_a, rater_b, weights=weight, categories=categories
            )
        except KappaUndefined:
            out[weight] = None
    return out


def kappa_verdict(value: float | None) -> str:
    """W6 의 채택선을 문장으로. 숫자만 찍으면 다음 행동이 안 적힌다."""
    if value is None:
        return "측정 불가"
    if value >= KAPPA_ADOPT_AT:
        return f"채택 가능 (>= {KAPPA_ADOPT_AT}) — 프롬프트를 잠근다"
    if value < KAPPA_REWRITE_BELOW:
        return f"루브릭 재작성 (< {KAPPA_REWRITE_BELOW}) — 기준이 모호하다"
    return f"보류 ({KAPPA_REWRITE_BELOW} ~ {KAPPA_ADOPT_AT}) — L2 자동화를 채택하지 않는다"


# --- 사람 라벨 조인 ---


def label_rows(records: list[L2Record]) -> list[dict]:
    """Flatten records into the long form a human label file joins against.

    한 행이 한 판정이다 — ``(question_id, variant, dimension)`` 이 조인 키이고,
    ``human_score`` 는 비어 있다. 사람이 이 파일을 채우면 그대로 kappa 입력이
    된다. 라벨링 UI 는 만들지 않는다(W6 kappa 보류 결정) — 조인만 되면 된다.
    """
    rows: list[dict] = []
    for record in records:
        for verdict in record.verdicts:
            rows.append(
                {
                    "question_id": record.question_id,
                    "variant": record.variant,
                    "dimension": verdict.dimension,
                    "question_type": record.question_type,
                    "rubric_version": record.rubric_version,
                    "rubric_sha256": record.rubric_sha256,
                    "judge_model": record.judge_model,
                    "judge_score": verdict.score,
                    "judge_evidence": verdict.evidence,
                    "human_score": None,
                }
            )
    return rows


class LabelMismatch(ValueError):
    """Human labels were written against a different rubric than these records."""


def join_labels(
    records: list[L2Record], labels: list[dict]
) -> dict[str, tuple[list[int], list[int]]]:
    """Pair judge scores with human scores, per dimension.

    루브릭이 다르면 조인을 거부한다. 사람이 v1 루브릭으로 라벨링하고 judge 가
    v2 로 채점한 것을 합치면 kappa 는 나오지만 그 숫자는 두 루브릭의 차이를
    재고 있다 — 바로 W6 이 "비교 불가"라고 적은 그 상황이다.
    """
    if not records:
        raise LabelMismatch("판정 레코드가 비어 있다")
    version = records[0].rubric_version
    digest = records[0].rubric_sha256

    by_key = {
        (r.question_id, r.variant, v.dimension): v.score
        for r in records
        for v in r.verdicts
    }

    paired: dict[str, tuple[list[int], list[int]]] = {}
    for row in labels:
        human = row.get("human_score")
        if human is None or human == "":
            continue  # 아직 안 채운 행은 조용히 건너뛴다.
        if row.get("rubric_version") != version or row.get("rubric_sha256") != digest:
            raise LabelMismatch(
                f"라벨이 다른 루브릭으로 작성됐다: "
                f"라벨={row.get('rubric_version')}/{str(row.get('rubric_sha256'))[:12]} "
                f"레코드={version}/{digest[:12]}. 같은 루브릭으로 다시 라벨링해야 한다."
            )
        key = (row["question_id"], row.get("variant", "server"), row["dimension"])
        judge = by_key.get(key)
        if judge is None:
            continue  # judge 가 '해당 없음'을 준 칸은 잴 대상이 없다.
        a, b = paired.setdefault(row["dimension"], ([], []))
        a.append(int(judge))
        b.append(int(human))
    return paired


def kappa_report(records: list[L2Record], labels: list[dict]) -> dict:
    """Per-dimension and pooled kappa, with the W6 verdict attached."""
    paired = join_labels(records, labels)
    per_dimension = {
        dimension: {
            "n": len(judge),
            "kappa": kappa_all(judge, human),
        }
        for dimension, (judge, human) in sorted(paired.items())
    }
    pooled_judge = [s for judge, _ in paired.values() for s in judge]
    pooled_human = [s for _, human in paired.values() for s in human]
    pooled = kappa_all(pooled_judge, pooled_human) if pooled_judge else {}
    headline = pooled.get("quadratic")
    return {
        "rubric_version": records[0].rubric_version if records else None,
        "rubric_sha256": records[0].rubric_sha256 if records else None,
        "judge_model": records[0].judge_model if records else None,
        "per_dimension": per_dimension,
        "pooled": {"n": len(pooled_judge), "kappa": pooled},
        "headline_kappa_quadratic": headline,
        "verdict": kappa_verdict(headline),
    }
