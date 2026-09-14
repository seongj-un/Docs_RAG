"""L3: run an agent against our MCP tools and score what it *did*.

L1 scores retrieval, L2 scores answers, L3 scores **behaviour** — which tool the
agent reached for, what it passed, how many times it called, and how much of
that was wasted. The whole point of M7 W4 is that those numbers move when only
the tool's description moves, so everything here is built to hold every other
variable still.

## What is held fixed, and where it is recorded

Notion W4's first trap is that a mid-experiment model change invalidates every
earlier number. So a run record carries the model name, the sha256 of the system
prompt, the sha256 of the scenario file, the variant name, the experiment name,
the replicate index and the git sha — and ``eval/l3_run.py`` writes one record
per scenario execution, not one per sweep, so a crash in the middle keeps
everything already paid for.

The system prompt is deliberately thin and **says nothing about tools**. It is
the control: if it told the agent when to search, that instruction would compete
with the description we are measuring, and a strong system prompt would flatten
the very difference the experiment exists to see. For the same reason the
harness never reads ``server/discover`` — the server instructions are a second
block of tool prose, and W4 changes one thing at a time.

## What is *not* reported

Latency and cost. Other work is running on this machine while these sweeps run,
so wall-clock numbers here would describe the contention, not the tool design.
Every metric below is a count, and counts do not care what else the GPU is
doing. Token totals are reported because they are counts too.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from google import genai
from google.genai import types

from eval import scenarios as scen
from eval.l3_client import McpClient
from eval.scenarios import Scenario, normalize
from eval.throttle import Throttle, call_with_retry

from app.mcp.variants import ROLE_NONE, ToolVariant

# --- 통제 변수 -------------------------------------------------------------

# 툴에 대해 한 마디도 하지 않는다. 근거는 모듈 docstring.
SYSTEM_PROMPT = """\
You are the assistant inside a document Q&A product. The person you are talking \
to has uploaded their own documents to it, and writes to you in Korean.

Answer them. Tools may or may not be available to you; whether to use one, \
which one, and what to pass it are entirely your decisions, and the tool \
descriptions are the only guidance you will get.

Reply in Korean, and keep it short.\
"""

# 한 시나리오가 쓸 수 있는 모델 왕복과 툴 호출의 하드 상한. 시나리오의
# ``max_calls`` 는 **채점 기준**이고 이것은 **비용 차단기**다. 둘을 같은 값으로
# 두면 예산 초과를 재는 대신 예산 초과를 막아 버려서, "불필요 호출 비율"이
# 구조적으로 0 에 붙는다.
MAX_TURNS = 6
HARD_CALL_CAP = 6


def system_prompt_sha256() -> str:
    return hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()


# --- MCP 툴 -> Gemini 함수 선언 -------------------------------------------

# Gemini 가 이해하지 못하는 JSON Schema 키. 남겨 두면 요청이 400 으로 죽는다.
_DROP_KEYS = frozenset({"title", "$schema", "additionalProperties"})
# Gemini Schema 가 아는 format 값. 그 밖의 값(예: "uuid")은 지운다 — 지워도
# 타입은 여전히 string 이고, 모델에게 필요한 "uuid 를 넣어라"는 필드 description
# 에 이미 적혀 있다.
_KEEP_FORMATS = frozenset({"date-time", "enum", "int32", "int64", "float", "double"})


def gemini_schema(node: Any) -> Any:
    """Strip a JSON Schema down to what the Gemini API accepts.

    재귀적으로 훑되 **구조는 바꾸지 않는다**. anyOf/enum/default 는 그대로
    남긴다 — 그 셋이 실험 3(파라미터 스키마)의 측정 대상이라, 여기서 평평하게
    펴면 그 실험은 자기 변수를 잃는다.
    """
    if isinstance(node, list):
        return [gemini_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key in _DROP_KEYS:
            continue
        if key == "format" and value not in _KEEP_FORMATS:
            continue
        out[key] = gemini_schema(value)
    return out


def to_declarations(tools: list[dict]) -> list[types.FunctionDeclaration]:
    """MCP ``tools/list`` entries -> Gemini function declarations.

    ``parameters_json_schema`` 로 넘긴다(``parameters`` 가 아니라). 그래야 MCP 가
    내놓은 스키마가 우리 손으로 다시 조립되지 않고 그대로 간다 — 우리가 옮겨
    적는 순간, 에이전트가 본 스키마와 서버가 검증하는 스키마가 갈릴 수 있다.
    """
    out = []
    for tool in tools:
        schema = gemini_schema(tool.get("inputSchema") or {"type": "object"})
        # 인자가 없는 툴(list_collections)은 properties 가 비어 있다. 빈 object
        # 스키마를 그대로 넘기면 SDK 가 거절하므로 생략한다.
        if not schema.get("properties"):
            schema = None
        out.append(
            types.FunctionDeclaration(
                name=tool["name"],
                description=tool.get("description") or "",
                parameters_json_schema=schema,
            )
        )
    return out


# --- 에이전트 루프 ---------------------------------------------------------


@dataclass
class Call:
    name: str
    args: dict
    is_error: bool = False
    error_text: str = ""


@dataclass
class Episode:
    """Everything one scenario execution produced."""

    scenario_id: str
    calls: list[Call] = field(default_factory=list)
    turns: int = 0
    final_text: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    # 하드 상한에 걸려 잘렸다. 잘린 에피소드는 호출 수 분포의 오른쪽 꼬리를
    # 잘라내므로, 평균을 읽을 때 이 개수를 함께 봐야 한다.
    truncated: bool = False
    failure: str = ""


def _contents(scenario: Scenario) -> list[types.Content]:
    turns = [
        types.Content(role=t.role, parts=[types.Part.from_text(text=t.text)])
        for t in scenario.history
    ]
    turns.append(
        types.Content(
            role="user", parts=[types.Part.from_text(text=scenario.utterance)]
        )
    )
    return turns


async def run_episode(
    scenario: Scenario,
    mcp: McpClient,
    declarations: list[types.FunctionDeclaration],
    *,
    client: genai.Client,
    model: str,
    throttle: Throttle,
) -> Episode:
    """One utterance, start to finish, against one tool surface."""
    episode = Episode(scenario_id=scenario.id)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.0,
        tools=(
            [types.Tool(function_declarations=declarations)] if declarations else None
        ),
        # 자동 함수 호출은 파이썬 콜러블을 넘겼을 때만 도는 기능이지만, 명시적으로
        # 끈다 — 켜져 있으면 SDK 가 루프를 대신 돌아서 우리가 세려던 턴이 우리
        # 눈앞을 지나가지 않는다.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    contents = _contents(scenario)

    for _ in range(MAX_TURNS):
        response = await call_with_retry(
            lambda: client.aio.models.generate_content(
                model=model, contents=contents, config=config
            ),
            throttle,
        )
        episode.turns += 1
        usage = getattr(response, "usage_metadata", None)
        episode.tokens_in += getattr(usage, "prompt_token_count", 0) or 0
        episode.tokens_out += getattr(usage, "candidates_token_count", 0) or 0

        candidates = response.candidates or []
        if not candidates or candidates[0].content is None:
            episode.failure = "모델이 후보를 내지 않았다"
            break
        content = candidates[0].content
        parts = content.parts or []
        calls = [p.function_call for p in parts if p.function_call is not None]

        if not calls:
            episode.final_text = (response.text or "").strip()
            break

        contents.append(content)
        responses = []
        for fc in calls:
            args = dict(fc.args or {})
            if len(episode.calls) >= HARD_CALL_CAP:
                episode.truncated = True
                break
            result = await mcp.call_tool(fc.name, args)
            episode.calls.append(
                Call(
                    name=fc.name,
                    args=args,
                    is_error=result.is_error,
                    error_text=result.text if result.is_error else "",
                )
            )
            responses.append(
                types.Part.from_function_response(
                    name=fc.name, response=result.for_model()
                )
            )
        if episode.truncated:
            break
        contents.append(types.Content(role="user", parts=responses))
    else:
        episode.truncated = True

    return episode


# --- 채점 ------------------------------------------------------------------


@dataclass
class Score:
    scenario_id: str
    kind: str
    expected_role: str
    expected_tool: str | None
    tool_choice: bool
    # None = 채점 대상 아님. 왜 0 이 아닌지는 score_arguments 참조.
    arguments: bool | None
    calls: int
    turns: int
    unnecessary_calls: int
    over_budget: bool
    first_tool: str | None
    truncated: bool


def _query_of(call: Call, query_arg: str) -> str:
    value = call.args.get(query_arg)
    return value if isinstance(value, str) else ""


def score_arguments(
    scenario: Scenario, matching: list[Call], query_arg: str
) -> bool | None:
    """Binary argument accuracy over the declared constraints.

    **왜 이진인가.** 제약의 종류가 이질적이다 — 부분 문자열 포함, 정확한 UUID,
    인자의 부재. 이것들에 부분 점수를 매기면 "0.5"가 시나리오마다 다른 뜻이 되고,
    두 조건의 평균을 빼는 순간 그 차이는 아무것도 의미하지 않는다. 20문항 × n=2
    에서 소수점을 만들어 봤자 정밀도의 환상일 뿐이다.

    **왜 부분 문자열 포함인가.** ``query_contains`` 의 항목은 한국어 명사이고
    실제 질의에는 조사가 붙는다("학생" ⊂ "학생의"). 토큰 동일성을 요구하면 옳은
    질의가 오답이 된다. 반대로 공백은 정규화 단계에서 지운다 — "대기 순번"과
    "대기순번"은 검색기에게 같은 것이고, 라벨이 띄어쓰기를 심판할 이유가 없다.

    **왜 `query_contains` 가 AND 인가.** Notion 은 목록만 줬다. OR 로 읽으면 가장
    뻔한 단어 하나로 만족되어 아무것도 재지 않는다 — 목록으로 적은 이유가 사라진다.

    **왜 첫 호출인가.** 인자 정확도는 "설명을 읽고 무엇을 넘겼는가"를 재는 것이고,
    그 판단은 첫 호출에서 내려진다. 뒤의 호출들은 첫 결과를 보고 고친 것이라
    설명이 아니라 피드백의 효과다 — 그쪽은 호출 수와 불필요 호출 비율이 잰다.
    다만 ``any_call_contains`` 는 예외다: 여러 번 부르는 것이 정답인 문항에서
    "각각 찾았는가"는 정의상 호출 전체에 걸린 조건이다. 그 묶음들은 **서로 다른
    호출**에서 만족돼야 한다 — 이유는 ``_matched_by_distinct_calls``.
    """
    if not matching:
        # 툴 선택이 이미 틀렸다. 여기서 0 을 주면 같은 실패가 두 지표에 두 번
        # 세어져, 두 숫자가 독립적으로 읽히지 않는다.
        return None

    spec = scenario.expected_args
    first = matching[0]
    # query_arg 는 변형이 정한다(우리 툴에서는 "question"). Notion 의 "query" 와
    # 이름이 다른 이유는 app/mcp/variants.py 의 query_arg 주석에 있다.
    query = normalize(_query_of(first, query_arg))

    for needle in spec.get("query_contains", []):
        if normalize(needle) not in query:
            return False
    for needle in spec.get("query_not_contains", []):
        if normalize(needle) in query:
            return False
    for name, expected in (spec.get("args_exact") or {}).items():
        actual = first.args.get(name)
        if actual is None or (
            str(actual).strip().lower() != str(expected).strip().lower()
        ):
            return False
    for name in spec.get("args_absent", []):
        if first.args.get(name) is not None:
            return False
    groups = spec.get("any_call_contains", [])
    if groups and not _matched_by_distinct_calls(groups, matching, query_arg):
        return False
    return True


def _matched_by_distinct_calls(
    groups: list[list[str]], calls: list[Call], query_arg: str
) -> bool:
    """Each group satisfied by a **different** call.

    ⚠️ 이 "서로 다른 호출" 조건이 이 제약의 핵심이다. 한 호출이 두 묶음을 동시에
    만족할 수 있게 두면 ``any_call_contains`` 는 ``query_contains`` 와 같은 것이
    되고, "여러 번 부르는 것이 정답인 문항"이 측정을 그만둔다.

    그리고 이 엄격함은 우리 구현 사정이 아니라 사용자 쪽 실패 모드에서 나온다.
    "A 와 B 를 비교해줘"를 한 질의로 합치면 리랭커가 돌려주는 세 구절이 전부 A
    쪽일 수 있고, 그러면 답은 B 를 조용히 빠뜨린 채 완성된 모양으로 나간다 —
    사용자가 알아챌 수 없는 종류의 절반짜리 답이다.

    묶음이 서너 개뿐이라 완전 탐색으로 충분하다.
    """
    if len(groups) > len(calls):
        return False
    queries = [normalize(_query_of(c, query_arg)) for c in calls]

    def assign(index: int, used: frozenset[int]) -> bool:
        if index == len(groups):
            return True
        for i, query in enumerate(queries):
            if i in used:
                continue
            if all(normalize(n) in query for n in groups[index]):
                if assign(index + 1, used | {i}):
                    return True
        return False

    return assign(0, frozenset())


def score(scenario: Scenario, episode: Episode, variant: ToolVariant) -> Score:
    role_map = variant.role_map()
    expected_role = scenario.expected_role_for(role_map)
    expected_tool = role_map.get(expected_role)

    calls = episode.calls
    first_tool = calls[0].name if calls else None

    if expected_role == ROLE_NONE:
        tool_choice = not calls
        needed = 0
        budget = 0
    else:
        tool_choice = first_tool == expected_tool
        needed = scenario.min_calls
        budget = scenario.max_calls

    matching = [c for c in calls if c.name == expected_tool] if expected_tool else []
    arguments = (
        None
        if expected_role == ROLE_NONE
        else score_arguments(scenario, matching, variant.query_arg)
    )

    return Score(
        scenario_id=scenario.id,
        kind=scenario.kind,
        expected_role=expected_role,
        expected_tool=expected_tool,
        tool_choice=tool_choice,
        arguments=arguments,
        calls=len(calls),
        turns=episode.turns,
        # 불필요 호출 = 이 시나리오가 필요로 한 것을 넘은 호출. 부르지 않는 것이
        # 정답인 문항에서는 호출 전부가 불필요하다 — 그 다섯 문항이 없으면 이
        # 지표는 "많이 부르는가"밖에 재지 못한다.
        unnecessary_calls=max(0, len(calls) - needed),
        over_budget=len(calls) > budget,
        first_tool=first_tool,
        truncated=episode.truncated,
    )


# --- 집계 ------------------------------------------------------------------


def aggregate(rows: list[Score]) -> dict:
    """Condition-level numbers. Counts only — no latency, no cost."""
    n = len(rows)
    if not n:
        return {}
    scored_args = [r.arguments for r in rows if r.arguments is not None]
    total_calls = sum(r.calls for r in rows)
    return {
        "n": n,
        "tool_choice_accuracy": sum(r.tool_choice for r in rows) / n,
        "argument_accuracy": (
            sum(scored_args) / len(scored_args) if scored_args else None
        ),
        "argument_scored_n": len(scored_args),
        "mean_calls": total_calls / n,
        "mean_turns": sum(r.turns for r in rows) / n,
        "unnecessary_call_rate": (
            sum(r.unnecessary_calls for r in rows) / total_calls if total_calls else 0.0
        ),
        "over_budget_n": sum(r.over_budget for r in rows),
        "truncated_n": sum(r.truncated for r in rows),
        "calls_on_no_tool_scenarios": sum(
            r.calls for r in rows if r.expected_role == ROLE_NONE
        ),
    }


def record(
    *,
    provenance: dict,
    scenario: Scenario,
    episode: Episode,
    scored: Score,
) -> dict:
    """One jsonl line: everything needed to re-read this execution later."""
    return {
        **provenance,
        "scenario_id": scenario.id,
        "kind": scenario.kind,
        "utterance": scenario.utterance,
        "score": asdict(scored),
        "episode": {
            "turns": episode.turns,
            "truncated": episode.truncated,
            "failure": episode.failure,
            "final_text": episode.final_text,
            "tokens_in": episode.tokens_in,
            "tokens_out": episode.tokens_out,
            "calls": [asdict(c) for c in episode.calls],
        },
    }


def load_records(path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


__all__ = [
    "Call",
    "Episode",
    "MAX_TURNS",
    "HARD_CALL_CAP",
    "SYSTEM_PROMPT",
    "Score",
    "aggregate",
    "gemini_schema",
    "load_records",
    "record",
    "run_episode",
    "score",
    "score_arguments",
    "system_prompt_sha256",
    "to_declarations",
]
