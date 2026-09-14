"""Tool-design variants for the M7 W4 A/B experiment, and W6's generation site.

W2 split the tool description out into ``descriptions.py`` so that a variant
would be "a change to a string and nothing else". This module is the other half
of that promise: it names the variants, says which experiment each one is a
condition of, and is the **only** place that decides what a server exposes.

Three rules hold it together.

**Production is a variant like any other, and it is the default.** ``PRODUCTION``
is built from the same constants ``tools.py`` used before this module existed,
and ``register(mcp)`` with no argument registers it. Nothing in the application
passes a variant, so the deployed server cannot drift with the experiment —
``tests/test_mcp_variants.py`` pins the default's wire shape (names, titles,
descriptions, input schemas) against a frozen snapshot so that "the experiment
changed production" fails the suite rather than the deploy.

**A condition changes one thing.** Each experiment below is a pair of variants
that differ in exactly one axis, and the axis is the experiment's name. Where a
condition needs a description rewritten, ``descriptions.py`` carries both sides
written to the same fact set and length so the measured difference is the axis
and not the prose.

**Roles, not names, are what a scenario labels.** A scenario says "the right
thing here is a search", not "the right thing here is ``search_documents``";
the tool that plays that role is named ``search_documents`` in one condition and
``search`` in another, and a metric keyed on the literal name would count a
rename as a behaviour change. ``ToolVariant.role_map()`` resolves the role at
scoring time — see ``eval/l3.py``.

**M7 W6 widened what a variant may change.** Until W6 every condition differed
in how a tool was *described* or *shaped*; ``SERVER_ANSWER`` differs in what the
tool *does* — it generates the answer instead of stopping at retrieval. The
three rules above still hold, and the second one is why W6's pair is kept out of
``EXPERIMENTS``: it is not a tool-design axis and is not scored by W4's metrics.
The comment on ``GENERATION_SITE`` says the rest.
"""

from dataclasses import dataclass, replace

from app.mcp.descriptions import (
    ANSWER_QUESTION_DESCRIPTION,
    ANSWER_SERVER_INSTRUCTIONS,
    SEARCH_COLLECTION_ENUM_DESCRIPTION,
    SEARCH_DOCUMENTS_DESCRIPTION,
    SEARCH_FUNCTIONAL_DESCRIPTION,
    SEARCH_QUERY_ONLY_DESCRIPTION,
    SEARCH_USE_WHEN_DESCRIPTION,
    SERVER_INSTRUCTIONS,
    SPLIT_FETCH_DESCRIPTION,
    SPLIT_LIST_COLLECTIONS_DESCRIPTION,
    SPLIT_SEARCH_DESCRIPTION,
    SPLIT_SERVER_INSTRUCTIONS,
)

# --- 역할 ------------------------------------------------------------------
#
# 시나리오가 라벨링하는 단위. 툴 이름이 아니라 역할인 이유는 이 모듈 docstring
# 의 세 번째 규칙에 있다.
ROLE_SEARCH = "search"
ROLE_FETCH = "fetch"
ROLE_LIST = "list"
# M7 W6. "검색"과 다른 역할인 이유는 멈추는 지점이 다르기 때문이다 — 이 역할의
# 툴은 생성까지 가고 답을 돌려준다. 같은 역할로 묶으면 "에이전트가 검색을
# 골랐는가"를 세는 W4 의 지표가 두 모드에서 같은 것을 세는 척하게 된다.
ROLE_ANSWER = "answer"
# "이 발화에는 툴을 부르지 않는 것이 정답" — 역할의 부재도 하나의 정답이다.
# 이것이 없으면 "툴 선택 정확도"가 "툴을 불렀는가"와 같은 말이 된다.
ROLE_NONE = "none"

ROLES = frozenset({ROLE_SEARCH, ROLE_FETCH, ROLE_LIST, ROLE_ANSWER, ROLE_NONE})


# --- 입력 스키마 종류 ------------------------------------------------------
#
# 실험 3(파라미터 스키마)의 축. ``full`` 이 프로덕션이다.
SCHEMA_FULL = "full"                # question + document_id + max_results
SCHEMA_QUERY_ONLY = "query_only"    # question 만 — 좁힐 손잡이가 없다
SCHEMA_COLLECTION_ENUM = "collection_enum"  # question + collection(열거형)


@dataclass(frozen=True)
class ToolSpec:
    """One tool as a variant exposes it."""

    role: str
    name: str
    title: str
    description: str


@dataclass(frozen=True)
class ToolVariant:
    """A complete tool surface: what exists, what it is called, what it says."""

    name: str
    experiment: str
    tools: tuple[ToolSpec, ...]
    instructions: str
    # 실험 4(출력 길이). 0 은 자르지 않음 = 프로덕션.
    snippet_chars: int = 0
    schema: str = SCHEMA_FULL
    # ``query_contains`` 채점이 들여다볼 인자 이름. Notion 은 "query" 라고 쓰지만
    # 우리 툴의 인자 이름은 ``question`` 이고, 이름을 바꾸는 것 자체가 변수
    # 하나이므로 바꾸지 않았다 — 대신 채점기가 어느 인자를 볼지 여기서 말한다.
    query_arg: str = "question"
    # 실험 3 의 열거형에 실릴 값. 실행 시점에 이 사용자의 문서 이름으로 채운다.
    collections: tuple[str, ...] = ()

    def role_map(self) -> dict[str, str]:
        """role -> tool name, for the roles this variant actually exposes."""
        return {spec.role: spec.name for spec in self.tools}

    def tool_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.tools)

    def with_collections(self, names: tuple[str, ...]) -> "ToolVariant":
        return replace(self, collections=tuple(names))


# --- 프로덕션 --------------------------------------------------------------
#
# ⚠️ 이 값은 실험의 일부가 아니라 **배포되는 것**이다. 여기 손대는 것은 곧
# 프로덕션 동작을 바꾸는 것이고, tests/test_mcp_variants.py 의 스냅샷이 깨진다.
SEARCH_DOCUMENTS = ToolSpec(
    role=ROLE_SEARCH,
    name="search_documents",
    title="Search the user's documents",
    description=SEARCH_DOCUMENTS_DESCRIPTION,
)

PRODUCTION = ToolVariant(
    name="production",
    experiment="",
    tools=(SEARCH_DOCUMENTS,),
    instructions=SERVER_INSTRUCTIONS,
)


# --- 실험 1: 툴 분해 -------------------------------------------------------
#
# 조건 A 는 프로덕션 그대로다. 새로 쓴 "단일 툴 대조군"을 만들지 않은 이유는,
# 그렇게 하면 결과가 우리가 실제로 배포한 것에 대해 아무 말도 하지 않기
# 때문이다. 조건 A 가 배포본이어야 "분해가 배포본보다 나은가"가 답해진다.
MONOLITHIC = replace(PRODUCTION, name="monolithic", experiment="tool_decomposition")

DECOMPOSED = ToolVariant(
    name="decomposed",
    experiment="tool_decomposition",
    tools=(
        ToolSpec(ROLE_SEARCH, "search", "Search the user's documents",
                 SPLIT_SEARCH_DESCRIPTION),
        ToolSpec(ROLE_FETCH, "fetch", "Fetch one passage by id",
                 SPLIT_FETCH_DESCRIPTION),
        ToolSpec(ROLE_LIST, "list_collections", "List the user's documents",
                 SPLIT_LIST_COLLECTIONS_DESCRIPTION),
    ),
    instructions=SPLIT_SERVER_INSTRUCTIONS,
    # ⚠️ 분해 조건도 스니펫을 자르지 않는다. 자르면 `fetch` 에 존재 이유가
    # 생기지만, 그 순간 이 조건은 "툴이 셋"과 "출력이 짧음" 둘을 동시에 바꾼
    # 것이 된다 — 그리고 출력 길이는 실험 4 가 따로 잰다. 여기서 `fetch` 의
    # 존재 이유는 "이미 본 청크를 id 로 다시 읽는 것"뿐이고, 그게 분해의 값이
    # 얼마인지를 재는 것이 이 실험이다.
    snippet_chars=0,
)


# --- 실험 2: description 문구 ---------------------------------------------
#
# 두 조건 모두 툴은 ``search_documents`` 하나이고 스키마도 같다. 바뀌는 것은
# description 문자열 하나뿐이다.
FUNCTIONAL = ToolVariant(
    name="functional",
    experiment="description_phrasing",
    tools=(replace(SEARCH_DOCUMENTS, description=SEARCH_FUNCTIONAL_DESCRIPTION),),
    instructions=SERVER_INSTRUCTIONS,
)

USE_WHEN = ToolVariant(
    name="use_when",
    experiment="description_phrasing",
    tools=(replace(SEARCH_DOCUMENTS, description=SEARCH_USE_WHEN_DESCRIPTION),),
    instructions=SERVER_INSTRUCTIONS,
)


# --- 실험 3: 파라미터 스키마 (코드만 — 한 번도 실행하지 않았다) -----------
QUERY_ONLY = ToolVariant(
    name="query_only",
    experiment="parameter_schema",
    tools=(replace(SEARCH_DOCUMENTS, description=SEARCH_QUERY_ONLY_DESCRIPTION),),
    instructions=SERVER_INSTRUCTIONS,
    schema=SCHEMA_QUERY_ONLY,
)

COLLECTION_ENUM = ToolVariant(
    name="collection_enum",
    experiment="parameter_schema",
    tools=(replace(SEARCH_DOCUMENTS, description=SEARCH_COLLECTION_ENUM_DESCRIPTION),),
    instructions=SERVER_INSTRUCTIONS,
    schema=SCHEMA_COLLECTION_ENUM,
)


# --- 실험 4: 출력 길이 (코드만 — 한 번도 실행하지 않았다) -----------------
#
# description 은 프로덕션 그대로다. 길이만이 축이므로 문구를 건드리면 안 된다.
# 치르는 값은 작은 불일치 하나다: 잘린 응답에서도 `content` 필드 설명은 여전히
# "The full text of the passage" 라고 말한다. 실제로 돌리기 전에 그 한 줄은
# 고쳐야 하고, 고치는 순간 그것이 이 실험의 두 번째 변수가 되지 않도록 A·B
# 양쪽 문구를 함께 바꿔야 한다.
SNIPPET_300 = replace(
    PRODUCTION, name="snippet_300", experiment="output_length", snippet_chars=300
)
SNIPPET_1000 = replace(
    PRODUCTION, name="snippet_1000", experiment="output_length", snippet_chars=1000
)


# --- M7 W6: 생성 위치 ------------------------------------------------------
#
# Notion W6 의 표 그대로다 — 모드 A 는 서버 안의 Gemini 가 답을 쓰고(인용
# 형식·거부 가드레일을 서버가 통제), 모드 B 는 청크만 건네고 호출한 에이전트가
# 스스로 답을 쓴다(대화 맥락을 쓰고 서버 생성 비용은 0).
#
# **조건 B 가 배포본 그대로인 것이 요점이다.** MONOLITHIC 이 그랬던 것과 같은
# 이유로, 대조군이 우리가 실제로 배포한 것이어야 결과가 "A 를 병행 제공할까"에
# 답한다. 새로 쓴 "클라이언트 생성 대조군"을 만들면 그 결과는 배포본에 대해
# 아무 말도 하지 않는다.
SERVER_ANSWER = ToolVariant(
    name="server_answer",
    experiment="generation_site",
    tools=(
        ToolSpec(
            role=ROLE_ANSWER,
            name="answer_question",
            title="Answer from the user's documents",
            description=ANSWER_QUESTION_DESCRIPTION,
        ),
    ),
    instructions=ANSWER_SERVER_INSTRUCTIONS,
)

# 모드 B = 배포본. ``search_documents`` 하나이고 검색에서 멈춘다.
CLIENT_ANSWER = replace(
    PRODUCTION, name="client_answer", experiment="generation_site"
)

# 이 쌍이 ``EXPERIMENTS`` 에 들어가지 **않는** 이유.
#
# ``EXPERIMENTS`` 는 W4 의 툴 설계 A/B 등록부이고, ``eval/l3_run.py`` 가 그것을
# 훑으면서 L3 지표(툴 선택 정확도·인자 정확도·호출 수)로 채점한다. 생성 위치는
# 툴 설계의 축이 아니라 **아키텍처의 축**이라, 같은 지표로 채점되지 않는다 —
# 모드 A 에서 "툴 선택 정확도"는 툴이 하나뿐이라 언제나 100%이고, 재야 할 것은
# L2 품질·인용·거부·비용·지연 다섯이다. 다른 지표를 재는 조건을 같은 등록부에
# 넣으면 W4 의 표가 채점할 수 없는 행을 들고 있게 된다. 그래서 러너도 따로다
# (``eval/w6_run.py``).
GENERATION_SITE_EXPERIMENT = "generation_site"
GENERATION_SITE: tuple[str, str] = ("server_answer", "client_answer")


VARIANTS: dict[str, ToolVariant] = {
    v.name: v
    for v in (
        PRODUCTION,
        MONOLITHIC,
        DECOMPOSED,
        FUNCTIONAL,
        USE_WHEN,
        QUERY_ONLY,
        COLLECTION_ENUM,
        SNIPPET_300,
        SNIPPET_1000,
        SERVER_ANSWER,
        CLIENT_ANSWER,
    )
}

# 실험 이름 -> (조건 A, 조건 B). 표의 왼쪽이 A 다.
EXPERIMENTS: dict[str, tuple[str, str]] = {
    "tool_decomposition": ("monolithic", "decomposed"),
    "description_phrasing": ("functional", "use_when"),
    "parameter_schema": ("query_only", "collection_enum"),
    "output_length": ("snippet_300", "snippet_1000"),
}

# 예산 결정(무료 티어)으로 실제로 측정한 둘. 나머지 둘은 코드만 있고 한 번도
# 돌지 않았다 — 보고서에서 그 사실을 숨기지 않기 위해 목록을 코드에 남긴다.
MEASURED_EXPERIMENTS: tuple[str, ...] = ("tool_decomposition", "description_phrasing")


def get(name: str) -> ToolVariant:
    if name not in VARIANTS:
        raise KeyError(f"unknown variant {name!r}; choose from {sorted(VARIANTS)}")
    return VARIANTS[name]
