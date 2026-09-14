"""Run reports and the version-to-version diff, with no database in sight.

A run is a plain value here — aggregates plus one row per question — so it can
come from Postgres, from a ``--json-out`` file, or from a test, and the diff
does not care which. 회귀 판정을 DB 안에 두면 DB 없이는 검증할 수 없는 규칙이
되고, CI 에서 가장 중요한 로직이 가장 안 돌아가는 로직이 된다.

**회귀의 정의가 이 파일의 본론이다.** 집계 평균만 보면 "recall 0.02 하락"
같은 숫자가 나오는데, 그 숫자로는 고쳐야 할 것이 무엇인지 알 수 없다. 그래서
두 가지를 함께 본다:

    집계 회귀   지표 평균이 base 보다 tolerance 넘게 떨어졌다
    문항 회귀   개별 질문의 R@k 가 떨어졌다 — 어떤 질문인지 이름으로 나온다

둘 중 하나라도 있으면 non-zero 로 끝난다. 평균이 유지되는데 문항이 뒤집히는
경우(하나 좋아지고 하나 나빠짐)를 통과시키면, 하네스는 "회귀를 자동으로
잡는다"는 목적을 정확히 그 지점에서 놓친다.
"""

import math
from dataclasses import asdict, dataclass, field

from eval import metrics as metric_lib

# 부동소수 비교 여유. 같은 입력을 두 번 돌려 나온 0.3333333333333333 과
# 0.33333333333333337 을 회귀로 부르지 않기 위한 최소치일 뿐, 실질적인
# 허용 하락폭이 아니다 — 그쪽은 --tolerance 다.
EPS = 1e-9


@dataclass
class QuestionResult:
    question_id: str
    question_type: str
    split: str
    first_gold_rank: int | None
    metrics: dict[str, float]
    retrieved_chunk_ids: list[str] = field(default_factory=list)
    gold_chunk_ids: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    dataset_name: str
    dataset_sha256: str
    config: str
    k: int
    git_sha: str | None = None
    label: str | None = None
    run_id: str | None = None
    created_at: str | None = None
    num_questions: int = 0
    num_scored: int = 0
    metrics: dict[str, float] = field(default_factory=dict)
    metrics_by_type: dict[str, dict[str, float]] = field(default_factory=dict)
    metrics_by_split: dict[str, dict[str, float]] = field(default_factory=dict)
    results: list[QuestionResult] = field(default_factory=list)

    def by_id(self) -> dict[str, QuestionResult]:
        return {r.question_id: r for r in self.results}

    def describe(self) -> str:
        who = self.run_id[:8] if self.run_id else "(unsaved)"
        return f"{who} {self.config} {self.git_sha or '?'} {self.created_at or ''}".strip()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "RunReport":
        results = [QuestionResult(**row) for row in raw.get("results", [])]
        return cls(**{**raw, "results": results})


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def aggregate(results: list[QuestionResult], keys: list[str]) -> dict[str, float]:
    """Mean of each metric over the given questions."""
    return {key: mean([r.metrics.get(key, 0.0) for r in results]) for key in keys}


def group_by(
    results: list[QuestionResult], attr: str, keys: list[str]
) -> dict[str, dict[str, float]]:
    groups: dict[str, list[QuestionResult]] = {}
    for result in results:
        groups.setdefault(getattr(result, attr), []).append(result)
    return {name: aggregate(rows, keys) for name, rows in sorted(groups.items())}


# --- diff ---


@dataclass
class MetricDelta:
    key: str
    base: float
    head: float

    @property
    def delta(self) -> float:
        return self.head - self.base


@dataclass
class QuestionFlip:
    question_id: str
    question_type: str
    base_score: float
    head_score: float
    base_rank: int | None
    head_rank: int | None


@dataclass
class DiffReport:
    base: RunReport
    head: RunReport
    primary_key: str
    tolerance: float
    deltas: list[MetricDelta] = field(default_factory=list)
    regressions: list[QuestionFlip] = field(default_factory=list)
    improvements: list[QuestionFlip] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)

    @property
    def metric_regressions(self) -> list[MetricDelta]:
        return [d for d in self.deltas if d.delta < -(self.tolerance + EPS)]

    @property
    def has_regression(self) -> bool:
        return bool(self.regressions or self.metric_regressions)


class IncomparableRuns(ValueError):
    """The two runs do not measure the same thing."""


def check_comparable(base: RunReport, head: RunReport, *, strict: bool = True) -> None:
    """Refuse to diff runs whose numbers mean different things.

    데이터셋이나 구성이나 cutoff 가 다르면 지표 차이는 검색 변화가 아니라
    측정 대상 변화다. 그걸 "회귀 없음"이라고 출력하는 것이 이 하네스가 할 수
    있는 가장 나쁜 일이라, 기본값은 아예 비교를 거부하는 쪽이다.
    """
    if base.config != head.config:
        raise IncomparableRuns(
            f"구성이 다르다: base={base.config} head={head.config}"
        )
    if base.k != head.k:
        raise IncomparableRuns(f"cutoff 가 다르다: base k={base.k} head k={head.k}")
    if base.dataset_name != head.dataset_name:
        raise IncomparableRuns(
            f"데이터셋이 다르다: base={base.dataset_name} head={head.dataset_name}"
        )
    if strict and base.dataset_sha256 != head.dataset_sha256:
        raise IncomparableRuns(
            "데이터셋 내용이 바뀌었다 "
            f"({base.dataset_sha256[:12]} -> {head.dataset_sha256[:12]}). "
            "질문이 달라진 것을 지표 변화로 읽지 않으려면 base 를 다시 돌려야 "
            "한다. 그래도 비교하려면 --allow-dataset-change."
        )


def diff(
    base: RunReport,
    head: RunReport,
    *,
    tolerance: float = 0.0,
    strict: bool = True,
) -> DiffReport:
    check_comparable(base, head, strict=strict)

    keys = [k for k in head.metrics if k in base.metrics]
    # 지표 키 순서는 리포트 전체에서 같아야 눈으로 비교가 된다.
    ordered = [k for k in metric_lib.metric_keys(ndcg_k=metric_lib.NDCG_K) if k in keys]
    ordered += [k for k in keys if k not in ordered]
    deltas = [MetricDelta(k, base.metrics[k], head.metrics[k]) for k in ordered]

    primary = f"R@{head.k}"
    base_rows, head_rows = base.by_id(), head.by_id()
    regressions: list[QuestionFlip] = []
    improvements: list[QuestionFlip] = []
    for qid, head_row in head_rows.items():
        base_row = base_rows.get(qid)
        if base_row is None:
            continue
        before = base_row.metrics.get(primary, 0.0)
        after = head_row.metrics.get(primary, 0.0)
        flip = QuestionFlip(
            question_id=qid,
            question_type=head_row.question_type,
            base_score=before,
            head_score=after,
            base_rank=base_row.first_gold_rank,
            head_rank=head_row.first_gold_rank,
        )
        if after < before - EPS:
            regressions.append(flip)
        elif after > before + EPS:
            improvements.append(flip)

    return DiffReport(
        base=base,
        head=head,
        primary_key=primary,
        tolerance=tolerance,
        deltas=deltas,
        regressions=sorted(regressions, key=lambda f: f.question_id),
        improvements=sorted(improvements, key=lambda f: f.question_id),
        dropped=sorted(set(base_rows) - set(head_rows)),
        added=sorted(set(head_rows) - set(base_rows)),
    )


# --- printing ---


def _fmt(value: float) -> str:
    return "  n/a" if value is None or math.isnan(value) else f"{value:.3f}"


def print_run(report: RunReport, keys: list[str]) -> None:
    print(
        f"\n=== {report.dataset_name} · {report.config} "
        f"(문항 {report.num_questions}, 채점 {report.num_scored}, k={report.k}) ==="
    )
    print(f"{'group':16}{'n':>4}" + "".join(f"{key:>9}" for key in keys))
    counts = {"ALL": report.num_scored}
    rows = {"ALL": report.metrics}
    for name, agg in report.metrics_by_type.items():
        rows[name] = agg
        counts[name] = sum(1 for r in report.results if r.question_type == name)
    for name, agg in report.metrics_by_split.items():
        rows[f"split:{name}"] = agg
        counts[f"split:{name}"] = sum(1 for r in report.results if r.split == name)
    for name, agg in rows.items():
        cells = "".join(f"{agg.get(key, 0.0):9.3f}" for key in keys)
        print(f"{name:16}{counts.get(name, 0):>4}{cells}")


def _rank(value: int | None) -> str:
    return "-" if value is None else str(value)


def print_diff(report: DiffReport) -> None:
    print(f"\n=== 지표 변화 ({report.base.describe()} -> {report.head.describe()}) ===")
    print(f"{'metric':12}{'base':>9}{'head':>9}{'delta':>9}")
    for delta in report.deltas:
        mark = ""
        if delta.delta < -(report.tolerance + EPS):
            mark = "  ⚠ 회귀"
        elif delta.delta > EPS:
            mark = "  ↑"
        print(
            f"{delta.key:12}{delta.base:9.3f}{delta.head:9.3f}"
            f"{delta.delta:+9.3f}{mark}"
        )

    print(f"\n=== 뒤집힌 문항 ({report.primary_key} 기준) ===")
    if not report.regressions and not report.improvements:
        print("  (없음 — 모든 문항이 같은 점수)")
    for flip in report.regressions:
        print(
            f"  ✗ 회귀 {flip.question_id} [{flip.question_type}] "
            f"{flip.base_score:.3f} -> {flip.head_score:.3f} "
            f"(정답 순위 {_rank(flip.base_rank)} -> {_rank(flip.head_rank)})"
        )
    for flip in report.improvements:
        print(
            f"  ✓ 개선 {flip.question_id} [{flip.question_type}] "
            f"{flip.base_score:.3f} -> {flip.head_score:.3f} "
            f"(정답 순위 {_rank(flip.base_rank)} -> {_rank(flip.head_rank)})"
        )

    if report.dropped or report.added:
        print(
            f"\n문항 구성이 달라졌다 — 사라짐 {len(report.dropped)}개, "
            f"새로 생김 {len(report.added)}개"
        )

    if report.has_regression:
        print(
            f"\n회귀 {len(report.regressions)}문항 · "
            f"지표 {len(report.metric_regressions)}개 하락"
        )
    else:
        print("\n회귀 없음")
