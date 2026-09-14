"""The L3 MCP client, over the real wire (M7 W4).

``eval/l3_client.py`` builds modern-era (2026-07-28) requests by hand instead of
importing ``tests/mcp_fixture``, because an eval runner should not depend on the
test package. That duplication is only safe if it is watched, so the first test
here asserts the two produce **identical** headers and bodies. If W7's fixture
learns something new about the protocol, this fails rather than the harness
silently speaking last month's dialect.

The rest drives the decomposed variant's three tools end to end — request built
here, answered by the real server through the real auth middleware and scope
gate — because that is the surface the W4 sweep measures and the only way to
know ``fetch`` and ``list_collections`` work is to call them.

Requires Postgres. The embedding server is stubbed the way every other MCP test
stubs it; retrieval itself is real.
"""

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.routing import Route

from app.config import settings
from app.db import SessionLocal
from app.mcp import variants
from app.mcp.server import MCP_HTTP_METHODS, build_mcp_app, build_mcp_server
from eval import l3, l3_client
from tests import mcp_fixture
from tests.mcp_fixture import (
    db_available,
    drop_users,
    make_chunk,
    make_session,
    make_user,
    run_async,
    stub_models,  # noqa: F401 - 픽스처를 이 모듈 이름공간으로 끌어온다
)

pytestmark = pytest.mark.skipif(not db_available(), reason="Postgres not reachable")


# --- 재사용 대신 감시 ------------------------------------------------------


@pytest.mark.parametrize(
    "method, name",
    [("tools/list", None), ("tools/call", "search_documents")],
)
def test_requests_match_the_w7_fixture_byte_for_byte(method, name):
    params = {"name": name, "arguments": {"question": "q"}} if name else None
    assert l3_client.body(method, params) == mcp_fixture.body(method, params)
    assert l3_client.headers(method, "tok", name) == mcp_fixture.headers(
        method, "tok", name
    )


def test_the_protocol_version_is_the_modern_one_everywhere():
    assert l3_client.PROTOCOL_VERSION == mcp_fixture.PROTOCOL_VERSION == "2026-07-28"
    sent = l3_client.body("tools/list")["params"]["_meta"]
    # 둘 다 없으면 서버가 -32020 으로 거절한다. 하나만 넣는 실수가 흔하다.
    assert set(sent) == {l3_client.META_PROTOCOL, l3_client.META_CAPABILITIES}


# --- 실제 호출 -------------------------------------------------------------


async def _live(variant, token, fn):
    server = build_mcp_server(variant)
    host = FastAPI()
    app = build_mcp_app(server)
    host.router.routes.append(
        Route(settings.mcp_path, endpoint=app, methods=MCP_HTTP_METHODS)
    )
    async with server.session_manager.run():
        transport = ASGITransport(app=host)
        async with AsyncClient(transport=transport,
                               base_url=mcp_fixture.BASE_URL) as http:
            return await fn(l3_client.McpClient(http, token, path=settings.mcp_path))


def test_the_decomposed_variant_answers_all_three_tools(stub_models):
    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "l3-client")
            token = str(await make_session(db, user))
            chunk = await make_chunk(db, user, "학생 목록 조회의 분당 한도는 31회다.")
            chunk_id, document_id = str(chunk.id), str(chunk.document_id)

        async def calls(mcp):
            listed = await mcp.list_tools()
            return (
                listed,
                await mcp.call_tool("list_collections", {}),
                await mcp.call_tool("fetch", {"chunk_id": chunk_id}),
                await mcp.call_tool("search", {"question": "분당 한도"}),
                await mcp.call_tool("fetch", {"chunk_id": str(uuid.uuid4())}),
            )

        out = await _live(variants.DECOMPOSED, token, calls)
        await drop_users(user)
        return out, chunk_id, document_id

    (listed, listing, fetched, searched, missing), chunk_id, document_id = run_async(
        scenario
    )

    assert [t["name"] for t in listed] == ["search", "fetch", "list_collections"]

    documents = listing.structured["documents"]
    assert [d["document_id"] for d in documents] == [document_id]
    assert documents[0]["passages"] == 1

    assert fetched.structured["chunk_id"] == chunk_id
    assert "31회" in fetched.structured["content"]

    assert searched.structured["hits"][0]["chunk_id"] == chunk_id

    # 남의(또는 없는) 청크는 "없다"로 답한다 — routers/chunks.py 와 같은 판단이다.
    assert missing.is_error is True
    assert "No such passage" in missing.text


def test_a_tool_refusal_is_a_result_not_an_exception(stub_models):
    """거절은 모델이 읽고 행동을 바꿔야 하는 결과다. 예외로 만들면 그 턴이 사라진다."""

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "l3-refusal")
            token = str(await make_session(db, user))

        async def calls(mcp):
            return await mcp.call_tool(
                "search_documents",
                {"question": "q", "document_id": str(uuid.uuid4())},
            )

        out = await _live(variants.PRODUCTION, token, calls)
        await drop_users(user)
        return out

    result = run_async(scenario)
    assert result.is_error is True
    assert "No such document" in result.text
    # 그리고 모델에게 갈 때는 실패라고 이름표가 붙어야 한다.
    assert set(result.for_model()) == {"error"}


def test_an_unauthenticated_call_is_a_transport_error():
    async def scenario():
        async def calls(mcp):
            with pytest.raises(l3_client.McpError) as err:
                await mcp.list_tools()
            return str(err.value)

        return await _live(variants.PRODUCTION, "not-a-session-id", calls)

    assert "401" in run_async(scenario)


# --- MCP 스키마 -> Gemini 함수 선언 ---------------------------------------


def test_declarations_keep_the_schema_and_drop_what_gemini_rejects():
    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "l3-decl")
            token = str(await make_session(db, user))

        listed = await _live(variants.PRODUCTION, token, lambda m: m.list_tools())
        await drop_users(user)
        return listed

    listed = run_async(scenario)
    decls = l3.to_declarations(listed)
    assert [d.name for d in decls] == ["search_documents"]

    schema = decls[0].parameters_json_schema
    assert "title" not in schema
    assert set(schema["properties"]) == {"question", "document_id", "max_results"}
    # format: uuid 는 Gemini 가 모르는 값이라 지운다. 지워도 타입은 string 이고,
    # "uuid 를 넣어라"는 필드 description 에 이미 적혀 있다.
    assert schema["properties"]["document_id"]["anyOf"] == [
        {"type": "string"}, {"type": "null"}
    ]
    # 반면 anyOf·default·범위는 남긴다 — 실험 3 이 재려는 것이 그 모양이다.
    assert schema["properties"]["max_results"]["default"] is None
    assert schema["required"] == ["question"]
    assert decls[0].description.startswith("Search the user's own uploaded")


def test_a_tool_with_no_arguments_declares_no_parameters():
    """빈 object 스키마를 그대로 넘기면 SDK 가 거절한다."""
    decls = l3.to_declarations(
        [{"name": "list_collections", "description": "d",
          "inputSchema": {"type": "object", "properties": {}}}]
    )
    assert decls[0].parameters_json_schema is None
