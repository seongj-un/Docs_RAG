"""Tool-design variants, and the proof that production did not move (M7 W4).

W4 adds a way to swap the tool surface. The risk it creates is the expensive
kind — an experiment quietly changing what the deployed server exposes, with no
symptom until an agent in production reads a description written for a sweep.
So the first half of this file is a **frozen snapshot of the default**: names,
titles, descriptions, and the exact input schema an agent receives. Any edit to
the production tool surface fails here, which is the point: that edit is a
deploy, and it should have to be typed twice.

The second half checks the properties a variant must have to be usable at all:
its tools declare a scope (a tool that forgets vanishes from every list —
``app/mcp/scopes.py``), and the two conditions of the phrasing experiment are
matched in length so the measured difference is framing and not volume.

Nothing here needs Postgres: ``MCPServer.list_tools()`` is answered from the
registry, and no tool body runs. The wire-level half lives in
``tests/test_l3_client.py``, which does need it.
"""

import pytest

from app.mcp import descriptions, variants
from app.mcp.scopes import REQUIRED_SCOPE_META, SCOPE_SEARCH
from app.mcp.server import build_mcp_server
from tests.mcp_fixture import run_async

# --- 프로덕션 스냅샷 -------------------------------------------------------

PRODUCTION_INPUT_SCHEMA = {
    "properties": {
        "question": {
            "description": "The user's question, in their own words. Not "
            "keywords — the retriever is built for natural questions.",
            "maxLength": 2000,
            "minLength": 1,
            "title": "Question",
            "type": "string",
        },
        "document_id": {
            "anyOf": [{"format": "uuid", "type": "string"}, {"type": "null"}],
            "default": None,
            "description": "Restrict the search to one document, using a "
            "`document_id` from an earlier result. Omit to search every "
            "document this user has uploaded.",
            "title": "Document Id",
        },
        "max_results": {
            "anyOf": [
                {"maximum": 20, "minimum": 1, "type": "integer"},
                {"type": "null"},
            ],
            "default": None,
            "description": "How many passages to return. Omit for the "
            "server's default, which is tuned for answering one question.",
            "title": "Max Results",
        },
    },
    "required": ["question"],
    "title": "search_documentsArguments",
    "type": "object",
}


async def _listed(variant=None):
    server = build_mcp_server(variant)
    return [t.model_dump(by_alias=True, exclude_none=True)
            for t in await server.list_tools()]


def test_the_default_is_exactly_what_w2_shipped():
    tools = run_async(_listed)
    assert [t["name"] for t in tools] == ["search_documents"]
    tool = tools[0]
    assert tool["title"] == "Search the user's documents"
    # 해시가 아니라 상수와의 동일성으로 못박는다. 해시는 깨졌을 때 무엇이
    # 달라졌는지 말해 주지 않는다.
    assert tool["description"] == descriptions.SEARCH_DOCUMENTS_DESCRIPTION
    assert tool["inputSchema"] == PRODUCTION_INPUT_SCHEMA
    assert tool["_meta"] == {REQUIRED_SCOPE_META: SCOPE_SEARCH}


def test_the_default_instructions_are_the_production_ones():
    server = build_mcp_server()
    assert server.instructions == descriptions.SERVER_INSTRUCTIONS


def test_condition_a_of_the_decomposition_experiment_is_the_deployed_surface():
    """대조군이 배포본이 아니면 그 실험은 우리가 배포한 것에 대해 말하지 않는다."""
    default = run_async(_listed)
    monolithic = run_async(lambda: _listed(variants.MONOLITHIC))
    assert default == monolithic


def test_production_never_carries_an_experimental_description():
    experimental = {
        descriptions.SEARCH_FUNCTIONAL_DESCRIPTION,
        descriptions.SEARCH_USE_WHEN_DESCRIPTION,
        descriptions.SPLIT_SEARCH_DESCRIPTION,
        descriptions.SEARCH_QUERY_ONLY_DESCRIPTION,
        descriptions.SEARCH_COLLECTION_ENUM_DESCRIPTION,
    }
    assert descriptions.SEARCH_DOCUMENTS_DESCRIPTION not in experimental
    for spec in variants.PRODUCTION.tools:
        assert spec.description not in experimental


# --- 변형이 갖춰야 할 성질 -------------------------------------------------


def _all_variants():
    for name, variant in variants.VARIANTS.items():
        if variant.schema == variants.SCHEMA_COLLECTION_ENUM:
            variant = variant.with_collections(("a.pdf", "b.pdf"))
        yield name, variant


@pytest.mark.parametrize("name, variant", list(_all_variants()))
def test_every_variant_registers_every_tool_it_declares(name, variant):
    """스코프 선언을 빠뜨린 툴은 **아무에게도 안 보인다**(app/mcp/scopes.py).

    조용히 사라지므로, 목록이 선언과 같은지 보지 않으면 그 조건은 "툴이 하나
    적은 조건"이 되어 있고 표는 멀쩡해 보인다.
    """
    tools = run_async(lambda: _listed(variant))
    assert [t["name"] for t in tools] == list(variant.tool_names())
    for tool in tools:
        assert tool["_meta"] == {REQUIRED_SCOPE_META: SCOPE_SEARCH}


def test_the_decomposed_variant_splits_into_three_distinct_roles():
    role_map = variants.DECOMPOSED.role_map()
    assert role_map == {
        variants.ROLE_SEARCH: "search",
        variants.ROLE_FETCH: "fetch",
        variants.ROLE_LIST: "list_collections",
    }
    descs = [spec.description for spec in variants.DECOMPOSED.tools]
    assert len(set(descs)) == 3


def test_the_phrasing_conditions_are_matched_in_length():
    """길이가 다르면 프레이밍이 이겼는지 분량이 이겼는지 가를 수 없다."""
    a = descriptions.SEARCH_FUNCTIONAL_DESCRIPTION.split()
    b = descriptions.SEARCH_USE_WHEN_DESCRIPTION.split()
    assert abs(len(a) - len(b)) / max(len(a), len(b)) <= 0.10


def test_the_phrasing_conditions_differ_only_in_framing():
    """같은 사실을 말해야 한다. 한쪽에만 있는 사실은 정보량의 차이가 된다."""
    a = descriptions.SEARCH_FUNCTIONAL_DESCRIPTION.lower()
    b = descriptions.SEARCH_USE_WHEN_DESCRIPTION.lower()
    for fact in ("chunk_id", "document_id", "score", "max_results", "korean", "20"):
        assert fact in a and fact in b, fact
    # 그리고 조건 B 만이 "언제"를 말한다.
    assert b.count("use it") + b.count("use this") >= 2
    assert "use this when" not in a


def test_the_untouched_experiments_are_declared_but_not_measured():
    """예산으로 줄인 사실이 코드에 남아 있어야 보고서가 거짓말을 하지 않는다."""
    assert set(variants.MEASURED_EXPERIMENTS) == {
        "tool_decomposition", "description_phrasing"
    }
    unmeasured = set(variants.EXPERIMENTS) - set(variants.MEASURED_EXPERIMENTS)
    assert unmeasured == {"parameter_schema", "output_length"}


def test_the_collection_enum_variant_refuses_to_register_without_values():
    """값이 빈 열거형은 '좁힐 손잡이'가 아니라 고장 난 스키마다."""
    from mcp.server.mcpserver import MCPServer

    from app.mcp import tools as mcp_tools

    with pytest.raises(ValueError):
        mcp_tools.register(MCPServer(name="t"), variants.COLLECTION_ENUM)


def test_the_output_length_variants_only_change_the_length():
    for variant in (variants.SNIPPET_300, variants.SNIPPET_1000):
        assert [s.description for s in variant.tools] == [
            descriptions.SEARCH_DOCUMENTS_DESCRIPTION
        ]
    lengths = (variants.SNIPPET_300.snippet_chars, variants.SNIPPET_1000.snippet_chars)
    assert lengths == (300, 1000)


@pytest.mark.parametrize(
    "text, limit, expected",
    [
        ("abcdef", 0, "abcdef"),
        ("abcdef", 10, "abcdef"),
        ("abcdef", 3, "abc…"),
        ("ab cdef", 3, "ab…"),
    ],
)
def test_snippet_marks_what_it_cut(text, limit, expected):
    """조용히 자르면 모델은 문장이 거기서 끝난 줄 알고 뒤를 지어낸다."""
    from app.mcp.tools import _snippet

    assert _snippet(text, limit) == expected
