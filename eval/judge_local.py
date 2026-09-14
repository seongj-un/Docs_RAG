"""The M7 L2 judge, run on a local generation model (Ollama).

Prompt assembly, the structured-output contract, and the HTTP client. The
rubric itself and everything that can be checked without a model live in
``eval/l2.py``.

Why this is a *second* judge and not a rewrite of ``eval/judge.py``
-------------------------------------------------------------------
``eval/judge.py`` is the M4 judge: Gemini, four RAGAS-shaped metrics
(faithfulness · answer_relevancy · context_precision · context_recall), and its
numbers are cited as evidence in README and in the project's Notion pages —
``rerank_top`` is 3 today *because* that judge measured context_precision 0.246
at 8. Swapping the model or the metric definitions underneath that name would
silently change what those numbers mean while every document that quotes them
stays unchanged. So it is left exactly as it was.

This module is a different measurement, not a better one:

    eval/judge.py   M4 · Gemini · RAGAS 4지표 · 파이프라인 진단용
    eval/judge_local.py  M7 W6 · 로컬 모델 · L2 3차원 · 답변 품질 채점용

They also answer to different constraints. M7's L2 dimensions are
groundedness · 정답성 · 거부 정확도 (M7 개요의 헤드라인 지표 표), which are not
the four RAGAS metrics. And the M4 judge has a flaw this one exists to avoid:
it is Gemini judging Gemini's output, which W6 names outright — 모델은 자기
출력을 후하게 준다.

Why a local model
-----------------
Two constraints meet here. W6 wants a judge from a *different family* than the
model under test, and M7's open questions list still has "judge용 외부 LLM 호출에
문서 청크를 넣어도 되는지" unanswered. A local judge satisfies the first
(generation is Gemini/Google, the judge is not) and dissolves the second: the
chunks never leave this machine, which is the same argument that already
justifies BGE-M3 running locally.

Why ``qwen3:4b``
----------------
Chosen from what this machine can actually run, against four criteria:

* **계열** — Qwen (Alibaba). 피평가 모델이 Gemini(Google)이므로 다른 계열이다.
  같은 맥에 이미 받아져 있는 ``gemma3:4b-it-qat`` 은 **쓰지 않는다** — Gemma 는
  Gemini 와 같은 구글 계보라, W6 이 피하라고 한 자기채점에 가장 가까운 선택이다.
* **크기** — 4B, Q4_K_M. 디스크 2.5GB, ``num_ctx`` 8192 에서 상주 3.9GB.
  이 맥은 M5 10코어 · 16GB 이고 BGE-M3 와 리랭커 가중치가 이미 6.9GB 를 쓴다.
  8B 급(~5.2GB 디스크)을 올리면 모델 서버와 동시에 상주시키기 어렵다.
  실측(2026-09-14, MPS): 로드 2.2초 · 로드 포함 첫 판정 9.7초 · 이후 판정
  3.5~8.4초(중앙값 5.4초, 컨텍스트 3청크 기준). 같은 MPS 를 임베딩 서버와
  나눠 쓰므로 이 수치는 동시 부하에 따라 흔들린다.
* **한국어** — Qwen3 는 다국어 학습이고 한국어 지시·판정이 실용 수준이다.
  다만 이것은 *실용 수준*이지 사람 수준이 아니다 — 아래 경고를 볼 것.
* **라이선스** — Apache-2.0. 평가 수치를 공개하는 포폴 저장소에서 판정자의
  라이선스가 걸리면 수치 자체를 못 싣는다.

⚠️ **작은 모델은 사람과의 일치도가 낮고, 지금 우리는 그것을 재지 않았다.**
W6 노트가 정확히 이 조합을 경고한다 — "작은 모델은 사람과의 일치도가 떨어지므로
kappa 검증이 더 중요해진다." 그런데 이 저장소의 결정은 kappa 검증 **보류**다.
즉 이 judge 는 노션이 위험하다고 적은 두 조건(작은 로컬 모델 + 미검증)을 동시에
만족한다. 그래서 이 판정이 내는 모든 수치에는 ``unvalidated`` 표식이 붙고,
집계 키 이름부터 ``groundedness__unvalidated`` 다(``eval/l2.py``). 실제로
확인한 실패 모드 하나를 적어 둔다: 루브릭 없이 짧게 물으면 이 모델은 명백히
근거 있는 답에 groundedness 0 을 주고 컨텍스트의 숫자를 잘못 옮겨 적었다.
루브릭을 명시한 뒤에는 그러지 않았다 — 즉 이 판정의 품질은 프롬프트에 매우
민감하며, 그 민감도의 크기를 우리는 아직 모른다.

Why a separate server and not ``scripts/local_model_server.py``
---------------------------------------------------------------
That server loads BGE-M3 and the reranker at startup and keeps them resident,
because the query path is interactive and must not pay a load. The judge is the
opposite: it runs offline in batches and nobody is waiting. Putting a third set
of weights in that process would make all three resident at once on a 16GB
machine and would put the judge in contention with live retrieval for the one
MPS device — which contaminates exactly the latency numbers this project keeps
measuring. Ollama is already installed here, loads and unloads on its own
schedule, and needs no torch in the application venv (``requirements.txt``
deliberately has none).

    ollama pull qwen3:4b          # 2.5GB, once
    python -m eval.l2_run score --in /tmp/golden_records.json
"""

import json
import time
from dataclasses import dataclass

import httpx

from app.config import settings
from eval import l2

# 판정은 한 번에 하나만 보낸다. 이 맥에는 MPS 가 하나뿐이고, 동시 추론은
# 임베딩·리랭킹 지연 측정을 전부 오염시킨다. 배치 처리로 얻을 속도보다
# "이 저장소의 모든 시간 수치를 믿을 수 있는 것"이 비싸다.
CONCURRENCY = 1

SYSTEM_PROMPT = """당신은 한국어 문서 RAG 시스템의 답변을 채점하는 평가자다.

당신의 일은 아래 루브릭을 **그대로** 적용하는 것이다. 좋은 답이 무엇인지
당신의 기준으로 판단하지 않는다. 루브릭에 없는 이유로 점수를 올리거나 내리지
않는다.

{rubric}

출력 규칙 — 반드시 지킨다:
1. 차원마다 **근거를 먼저** 쓴다. 컨텍스트나 답변에서 실제 문구를 인용하고,
   그것이 루브릭의 어느 점수 설명에 해당하는지 적는다.
2. **점수는 근거를 쓴 다음에** 쓴다. 점수를 먼저 정해 놓고 근거를 맞추지 않는다.
3. 근거는 2~3문장. 요약하지 말고 무엇을 보고 그렇게 판단했는지 적는다.
4. 지정된 JSON 스키마로만 출력한다. 다른 말은 쓰지 않는다."""

# 스키마의 키 순서가 곧 생성 순서다(구조적 출력은 스키마를 문법으로 바꿔
# 토큰을 강제한다). evidence 를 score 앞에 두는 것이 "근거 먼저, 점수 나중"을
# 프롬프트의 부탁이 아니라 **문법**으로 만드는 지점이다.
def response_schema(dimensions: tuple[str, ...]) -> dict:
    return {
        "type": "object",
        "properties": {
            dimension: {
                "type": "object",
                "properties": {
                    "evidence": {"type": "string"},
                    "score": {"type": "integer", "enum": list(l2.SCALE)},
                },
                "required": ["evidence", "score"],
            }
            for dimension in dimensions
        },
        "required": list(dimensions),
    }


def build_user_prompt(item: dict, dimensions: tuple[str, ...]) -> str:
    """The question, what was retrieved, what was answered, and what was expected.

    컨텍스트에 인덱스를 붙인다. 판정자가 "[2] 의 …" 처럼 어느 청크를 봤는지
    적을 수 있어야 근거가 검증 가능해진다 — 근거가 검증 불가능하면 근거 먼저
    쓰게 한 의미가 없다.
    """
    contexts = item.get("contexts") or []
    rendered = "\n\n".join(f"[{i}] {c}" for i, c in enumerate(contexts)) or (
        "(검색된 컨텍스트 없음)"
    )
    reference = item.get("reference") or item.get("reference_answer") or ""
    qtype = item.get("type", "unknown")
    lines = [
        f"문항 유형: {qtype}",
        f"채점할 차원: {', '.join(dimensions)}",
        "",
        f"질문:\n{item['question']}",
        "",
        f"검색된 컨텍스트:\n{rendered}",
        "",
        f"생성된 답변:\n{item.get('answer', '')}",
        "",
        f"기준답변:\n{reference or '(없음 — 이 문항은 문서에 답이 없는 유형이다)'}",
    ]
    if qtype == "no_answer":
        lines.append(
            "\n이 문항은 문서에 답이 없는 유형이다. 올바른 응답은 거부이며, "
            "답을 지어냈다면 groundedness 와 refusal_accuracy 가 모두 낮아야 한다."
        )
    return "\n".join(lines)


class JudgeUnavailable(RuntimeError):
    """The local judge server did not answer usefully."""

    def __init__(self, cause: Exception):
        self.cause = cause
        super().__init__(
            f"로컬 judge({settings.judge_base_url}, {settings.judge_model}) 응답 실패: "
            f"{type(cause).__name__}: {cause}. "
            "`ollama serve` 가 떠 있고 `ollama pull "
            f"{settings.judge_model}` 이 끝났는지 확인할 것."
        )


@dataclass
class JudgeReply:
    payload: dict
    latency_ms: int


class LocalJudge:
    """Ollama-backed chat client, shaped for one judgement per call."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.judge_base_url).rstrip("/")
        self.model = model or settings.judge_model
        self.timeout_s = timeout_s or settings.judge_timeout_s
        self.provider = settings.judge_provider

    async def chat(self, system: str, user: str, schema: dict) -> JudgeReply:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            # Qwen3 는 하이브리드 추론 모델이라 기본이 사고 모드다. 켜 두면
            # 판정 1건이 수십 초가 되고, 무엇보다 사고 블록이 스키마 밖의
            # 토큰이라 구조적 출력과 섞인다. 우리가 원하는 "근거"는 숨은
            # 사고가 아니라 **기록되는 근거**다.
            "think": False,
            "format": schema,
            "options": {
                # 0.0. 같은 입력에 같은 점수가 나와야 회귀를 판별할 수 있다.
                "temperature": 0.0,
                "num_ctx": settings.judge_num_ctx,
            },
            "keep_alive": settings.judge_keep_alive,
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(f"{self.base_url}/api/chat", json=body)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise JudgeUnavailable(exc) from exc
        elapsed = int((time.perf_counter() - started) * 1000)

        content = (data.get("message") or {}).get("content") or ""
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            # 구조적 출력을 걸었는데도 JSON 이 아니면 조용히 0점 처리하지
            # 않는다 — 그 문항은 영원히 평균을 갉아먹으면서 아무도 이유를
            # 모른다(eval/gold.py 가 스니펫 0건에서 죽는 것과 같은 판단).
            raise JudgeUnavailable(
                ValueError(f"JSON 이 아닌 응답: {content[:200]!r}")
            ) from exc
        return JudgeReply(payload=payload, latency_ms=elapsed)


def parse_verdicts(payload: dict, dimensions: tuple[str, ...]) -> list[l2.DimensionVerdict]:
    """Turn the model's JSON into verdicts, refusing anything malformed.

    스키마를 강제했어도 파싱은 따로 검사한다. 강제가 풀리는 경로가 있고(모델
    교체·서버 버전·format 미지원), 그때 조용히 0 이 들어가면 그 실행의 평균은
    설명 없이 낮아진다.
    """
    verdicts: list[l2.DimensionVerdict] = []
    for dimension in dimensions:
        raw = payload.get(dimension)
        if not isinstance(raw, dict):
            raise ValueError(f"{dimension} 판정이 없다: {payload!r}")
        score = raw.get("score")
        if score not in l2.SCALE:
            raise ValueError(
                f"{dimension} 점수 {score!r} 가 척도 밖이다 (허용 {list(l2.SCALE)})"
            )
        evidence = str(raw.get("evidence") or "").strip()
        if not evidence:
            # 근거 없는 점수는 이 설계의 목적을 정확히 놓친 출력이다.
            raise ValueError(f"{dimension} 에 근거가 비어 있다")
        verdicts.append(
            l2.DimensionVerdict(dimension=dimension, evidence=evidence, score=int(score))
        )
    return verdicts


async def judge_one(
    item: dict, judge: LocalJudge | None = None, *, variant: str = "server"
) -> l2.L2Record:
    """Score one answer against the rubric. Returns an unvalidated record."""
    judge = judge or LocalJudge()
    dimensions = l2.applicable_dimensions(item.get("type", "unknown"))
    reply = await judge.chat(
        SYSTEM_PROMPT.format(rubric=l2.rubric_text()),
        build_user_prompt(item, dimensions),
        response_schema(dimensions),
    )
    return l2.L2Record(
        question_id=str(item.get("id") or item.get("question_id") or item["question"]),
        question_type=item.get("type", "unknown"),
        variant=variant,
        rubric_version=l2.RUBRIC_VERSION,
        rubric_sha256=l2.rubric_sha256(),
        judge_provider=judge.provider,
        judge_model=judge.model,
        verdicts=parse_verdicts(reply.payload, dimensions),
        validation=l2.Validation(),  # 기본값이 unvalidated 다. 채우려면 kappa 가 필요하다.
        order="single",
        latency_ms=reply.latency_ms,
        created_at=l2.now_iso(),
        question=item.get("question"),
    )


# --- A/B 비교 (위치 편향) ---

PAIRWISE_SYSTEM = """당신은 한국어 문서 RAG 시스템의 답변 두 개를 비교하는 평가자다.

아래 루브릭을 그대로 적용해 **어느 답변이 더 나은지** 고른다. 답변의 길이나
문체가 아니라 루브릭의 차원으로 판단한다.

{rubric}

출력 규칙:
1. **근거를 먼저** 쓴다. 두 답변의 어느 부분이 루브릭의 어느 기준에서 갈렸는지
   구체적으로 적는다.
2. 그 다음에 winner 를 쓴다: "first" · "second" · "tie" 중 하나.
3. 지정된 JSON 스키마로만 출력한다."""

PAIRWISE_SCHEMA = {
    "type": "object",
    "properties": {
        "evidence": {"type": "string"},
        "winner": {"type": "string", "enum": ["first", "second", "tie"]},
    },
    "required": ["evidence", "winner"],
}


def build_pairwise_prompt(item: dict, first: str, second: str) -> str:
    """Two candidate answers with no hint of which system produced which.

    변형 이름(서버 생성/클라이언트 생성)을 프롬프트에 넣지 않는다. 넣으면
    판정자가 이름의 함의로 고를 수 있고, 그 순간 우리는 답변이 아니라 라벨을
    비교하게 된다.
    """
    contexts = item.get("contexts") or []
    rendered = "\n\n".join(f"[{i}] {c}" for i, c in enumerate(contexts)) or (
        "(검색된 컨텍스트 없음)"
    )
    reference = item.get("reference") or item.get("reference_answer") or ""
    return (
        f"문항 유형: {item.get('type', 'unknown')}\n\n"
        f"질문:\n{item['question']}\n\n"
        f"검색된 컨텍스트:\n{rendered}\n\n"
        f"답변 first:\n{first}\n\n"
        f"답변 second:\n{second}\n\n"
        f"기준답변:\n{reference or '(없음)'}"
    )


async def judge_pairwise(
    item: dict,
    answers: dict[str, str],
    variant_a: str,
    variant_b: str,
    judge: LocalJudge | None = None,
) -> l2.PairwiseOutcome:
    """Compare two variants twice, swapping which one is shown first.

    W6: "비교 평가는 A/B 순서를 바꿔 두 번 돌린다 (위치 편향)." 한 번만 돌리면
    판정자가 답변을 골랐는지 자리를 골랐는지 구분할 수 없다. 두 판정이 같은
    자리를 고르면 그 문항은 승부가 아니라 편향이고,
    ``PairwiseOutcome.position_biased`` 가 그것을 이름 붙여 준다.
    """
    judge = judge or LocalJudge()
    system = PAIRWISE_SYSTEM.format(rubric=l2.rubric_text())
    picks: dict[str, str] = {}
    for order in l2.ORDERS:
        first_variant, second_variant = l2.order_positions(variant_a, variant_b, order)
        reply = await judge.chat(
            system,
            build_pairwise_prompt(
                item, answers[first_variant], answers[second_variant]
            ),
            PAIRWISE_SCHEMA,
        )
        winner = reply.payload.get("winner")
        if winner not in ("first", "second", "tie"):
            raise ValueError(f"알 수 없는 winner {winner!r}")
        picks[order] = (
            "tie"
            if winner == "tie"
            else (first_variant if winner == "first" else second_variant)
        )
    return l2.PairwiseOutcome(
        question_id=str(item.get("id") or item["question"]),
        variant_a=variant_a,
        variant_b=variant_b,
        pick_ab=picks["ab"],
        pick_ba=picks["ba"],
    )
