"""RAG quality metrics via an LLM judge (Gemini).

Implements the four RAGAS metrics directly rather than through the ragas
package, which cannot be installed here: every ragas release imports
``langchain_community.chat_models.vertexai``, a langchain-0.x path that
conflicts with the langchain-1.x stack the Gemini adapter needs.

**One judge call per record.** The reference implementation decomposes each
metric into its own LLM pass (~10-15 calls per question); at the Gemini free
tier's 5 requests/minute that is over an hour per evaluation run, which would
make the before/after comparisons this milestone exists for impractical. So the
four judgements share a single structured response.

To keep that from collapsing into a vibe score, the judge returns *counted
sub-judgements* — claims marked supported/unsupported, contexts marked
relevant/irrelevant, reference facts marked present/absent — and the metrics are
computed from those counts here, in code. The reasoning stays auditable and the
arithmetic is not the model's to get wrong.

Because the prompts differ from ragas's, absolute values are not comparable to
published ragas numbers. They are comparable *to each other*, which is what
before/after evaluation needs.
"""

import json
from dataclasses import dataclass

from google import genai

from app.config import settings
from eval.throttle import Throttle, call_with_retry

# Industry-typical alarm levels quoted in the M4 spec. Alarms, not laws.
THRESHOLDS = {
    "faithfulness": 0.75,
    "answer_relevancy": 0.80,
    "context_precision": 0.70,
    "context_recall": 0.80,
}

SYSTEM_PROMPT = """당신은 RAG 시스템 평가자다. 주어진 질문·검색된 컨텍스트·생성된 답변·기준답변을 보고 아래 4가지를 판정한다.

1. claims: 답변을 검증 가능한 주장 단위로 분해하고, 각 주장이 **컨텍스트만으로** 뒷받침되는지 판정한다.
   - supported=true는 컨텍스트에 근거가 있을 때만. 사실이지만 컨텍스트에 없으면 false.
2. contexts: 제공된 컨텍스트 각각이 이 질문에 답하는 데 관련이 있는지 판정한다(인덱스 순서대로 전부).
3. reference_facts: 기준답변을 사실 단위로 분해하고, 각 사실이 **컨텍스트 안에 존재**하는지 판정한다.
4. answer_relevancy: 답변이 질문의 의도에 맞게 응답했는지 0.0~1.0으로 판정한다.
   - 정확성이 아니라 '질문에 대한 응답인가'를 본다. 얼버무리거나 다른 걸 답하면 낮다.

답변이 "제공된 문서에서 찾을 수 없습니다" 같은 거부인 경우:
- claims는 빈 배열, answer_relevancy는 질문이 실제로 문서 범위 밖이면 1.0, 답할 수 있었는데 거부했으면 0.0.

반드시 지정된 JSON 스키마로만 출력한다."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "supported": {"type": "boolean"},
                    "why": {"type": "string"},
                },
                "required": ["claim", "supported"],
            },
        },
        "contexts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "relevant": {"type": "boolean"},
                },
                "required": ["index", "relevant"],
            },
        },
        "reference_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "present_in_context": {"type": "boolean"},
                },
                "required": ["fact", "present_in_context"],
            },
        },
        "answer_relevancy": {"type": "number"},
    },
    "required": ["claims", "contexts", "reference_facts", "answer_relevancy"],
}


@dataclass
class Scores:
    faithfulness: float | None
    answer_relevancy: float
    context_precision: float | None
    context_recall: float | None
    detail: dict


def _client() -> genai.Client:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=settings.gemini_api_key)


def _build_prompt(record: dict) -> str:
    contexts = "\n\n".join(
        f"[{i}] {c}" for i, c in enumerate(record["contexts"])
    ) or "(검색된 컨텍스트 없음)"
    return (
        f"질문:\n{record['question']}\n\n"
        f"검색된 컨텍스트:\n{contexts}\n\n"
        f"생성된 답변:\n{record['answer']}\n\n"
        f"기준답변:\n{record['reference']}"
    )


def _ratio(items: list, key: str) -> float | None:
    """Fraction of ``items`` whose ``key`` is true; None when there is nothing
    to judge (an empty set is not a zero score — it is no measurement)."""
    if not items:
        return None
    return sum(1 for item in items if item.get(key)) / len(items)


def score_from_judgement(payload: dict) -> Scores:
    claims = payload.get("claims") or []
    contexts = payload.get("contexts") or []
    facts = payload.get("reference_facts") or []
    relevancy = float(payload.get("answer_relevancy", 0.0))
    return Scores(
        faithfulness=_ratio(claims, "supported"),
        answer_relevancy=max(0.0, min(1.0, relevancy)),
        context_precision=_ratio(contexts, "relevant"),
        context_recall=_ratio(facts, "present_in_context"),
        detail={
            "n_claims": len(claims),
            "n_contexts": len(contexts),
            "n_reference_facts": len(facts),
            "unsupported_claims": [c["claim"] for c in claims if not c.get("supported")],
            "missing_facts": [
                f["fact"] for f in facts if not f.get("present_in_context")
            ],
        },
    )


async def judge_record(record: dict, throttle: Throttle) -> Scores:
    client = _client()

    async def call():
        return await client.aio.models.generate_content(
            model=settings.eval_llm_model,
            contents=_build_prompt(record),
            config=genai.types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
            ),
        )

    resp = await call_with_retry(call, throttle)
    payload = json.loads(resp.text or "{}")
    return score_from_judgement(payload)


def diagnose(scores: dict[str, float | None]) -> str:
    """Name the likely failure locus from the metric combination.

    Follows the M4 spec's diagnosis table: the point of four metrics is that
    one alone cannot separate a retrieval failure from a generation failure.
    """
    faith = scores.get("faithfulness")
    relevancy = scores.get("answer_relevancy")
    precision = scores.get("context_precision")
    recall = scores.get("context_recall")

    low = lambda v, key: v is not None and v < THRESHOLDS[key]  # noqa: E731

    if low(recall, "context_recall") and not low(faith, "faithfulness"):
        return "검색이 근거를 못 찾음 (청킹·임베딩·top-K)"
    if low(precision, "context_precision"):
        return "검색 노이즈 과다 (리랭킹·후보 수)"
    if not low(recall, "context_recall") and low(faith, "faithfulness"):
        return "생성 환각 (프롬프트·모델·temperature)"
    if low(relevancy, "answer_relevancy"):
        return "질문 의도 이해 실패 (프롬프트·쿼리 재작성)"
    return "정상"
