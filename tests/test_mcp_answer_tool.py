"""The server-generating MCP tool, and the proof it stayed out of production (W6).

W6 adds ``answer_question``: the first tool on this server that does not stop at
retrieval. Three things have to hold, and they are what this file checks.

**Production did not move.** The tool exists only inside a variant. The frozen
snapshot in ``tests/test_mcp_variants.py`` already fails if the default surface
changes at all; here the same claim is made from the other direction — the name
``answer_question`` must not appear anywhere in what the deployed server lists.
Two witnesses because the failure is the expensive kind: an agent in production
reaching a tool that spends the user's LLM budget, with no symptom until a bill.

**It reuses the production answer path.** The tool must drive the same
``QueryRunner``/``generate`` pair that ``POST /query`` drives — the refusal
sentence, the grounding floor and the ``[p.N]`` contract come from there, not
from ``tools.py``. The tests below prove it by changing nothing but the chunk
score and watching the refusal guardrail fire without an LLM call.

**It is distinguishable forever.** ``traces.source`` is ``mcp_answer``, which is
what lets W6's comparison be recomputed from the database after the report is
gone (migration 0015).

The LLM is stubbed. A test that called Gemini would be a test that costs money
and fails when a free tier runs out, and nothing here is about the model — it is
about which code path was taken and what it recorded.
"""

import uuid

import pytest
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal
from app.mcp import variants
from app.mcp.scopes import REQUIRED_SCOPE_META, SCOPE_SEARCH
from app.mcp.server import build_mcp_server
from app.services import generate, llm
from app.services.tracing import (
    MCP_SOURCES,
    SERVER_GENERATED_SOURCES,
    SOURCE_MCP_ANSWER,
    SOURCE_MCP_SEARCH,
    TRACE_SOURCES,
)
from tests.mcp_fixture import (
    call_tool,
    db_available,
    drop_users,
    make_chunk,
    make_session,
    make_user,
    mcp_client,
    run_async,
    stub_models,  # noqa: F401 - 픽스처를 이 모듈 이름공간으로 끌어온다
)

pytestmark = pytest.mark.skipif(not db_available(), reason="Postgres not reachable")

ANSWER = "답은 [p.1] 에 있다."


def _result(response):
    """The structured payload of a successful tools/call."""
    body = response.json()
    assert "error" not in body, body
    result = body["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def _error_text(response) -> str:
    result = response.json()["result"]
    assert result.get("isError"), result
    return "\n".join(b.get("text", "") for b in result.get("content") or [])


@pytest.fixture
def stub_llm(monkeypatch):
    """Answer without calling a provider, and count the calls.

    ``app.services.llm.generate`` 를 갈아 끼운다 — ``generate.py`` 가 호출
    시점에 속성을 찾으므로 이 한 곳이면 충분하고, 프로덕션 코드에 테스트용
    분기를 넣지 않아도 된다(eval/w6_run.py 가 같은 수법을 쓴다).
    """
    calls: list[tuple[str, str]] = []

    async def fake_generate(system_prompt, user_prompt, *, model=None):
        calls.append((system_prompt, user_prompt))
        return llm.Generation(text=ANSWER, tokens_in=11, tokens_out=7)

    monkeypatch.setattr(llm, "generate", fake_generate)
    return calls


# --- 프로덕션이 안 바뀐다 -------------------------------------------------


def test_production_does_not_expose_the_answering_tool():
    """배포되는 서버에는 이 툴이 존재하지 않는다."""

    async def listed(variant=None):
        server = build_mcp_server(variant)
        return [t.model_dump(by_alias=True, exclude_none=True)
                for t in await server.list_tools()]

    default = run_async(listed)
    assert [t["name"] for t in default] == ["search_documents"]
    assert "answer_question" not in {t["name"] for t in default}

    # 조건 B 는 배포본 그대로여야 한다 — 아니면 결과가 우리가 배포한 것에
    # 대해 아무 말도 하지 않는다(MONOLITHIC 과 같은 판단).
    client_mode = run_async(lambda: listed(variants.CLIENT_ANSWER))
    assert client_mode == default


def test_the_answering_variant_exposes_exactly_one_tool_and_declares_its_scope():
    """스코프 선언을 빠뜨린 툴은 아무에게도 안 보인다(app/mcp/scopes.py)."""

    async def listed():
        server = build_mcp_server(variants.SERVER_ANSWER)
        return [t.model_dump(by_alias=True, exclude_none=True)
                for t in await server.list_tools()]

    tools = run_async(listed)
    assert [t["name"] for t in tools] == ["answer_question"]
    assert tools[0]["_meta"] == {REQUIRED_SCOPE_META: SCOPE_SEARCH}
    # 컨텍스트 크기 손잡이는 열지 않는다 — 근거는 _register_answer 의 docstring.
    assert set(tools[0]["inputSchema"]["properties"]) == {"question", "document_id"}


def test_the_two_modes_are_the_deployed_surface_and_one_extra_tool():
    """W6 의 두 조건이 실제로 '생성 위치 하나'만 다른지."""
    server_side = variants.SERVER_ANSWER.role_map()
    client_side = variants.CLIENT_ANSWER.role_map()
    assert server_side == {variants.ROLE_ANSWER: "answer_question"}
    assert client_side == {variants.ROLE_SEARCH: "search_documents"}
    assert variants.GENERATION_SITE == ("server_answer", "client_answer")
    # W4 의 등록부에는 들어가지 않는다 — 같은 지표로 채점되지 않기 때문이다.
    assert variants.GENERATION_SITE_EXPERIMENT not in variants.EXPERIMENTS


# --- 트레이스: W6 의 비교축이 DB 에 남는다 -------------------------------


def test_the_two_modes_leave_different_trace_sources(stub_models, stub_llm):
    """같은 사용자·같은 질문이 어느 모드였는지 traces 만으로 갈린다.

    이것이 마이그레이션 0015 의 존재 이유 전체다. 두 모드가 같은 source 를
    남기면 오늘의 표를 잃는 순간 그 비교는 영영 복원 불가능해진다.
    """

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "mcp-answer")
            session_id = await make_session(db, user)
            await make_chunk(db, user, "학생 목록 조회 오류 코드는 E4012 다.")

        async with mcp_client(variant=variants.SERVER_ANSWER) as client:
            answered = await call_tool(
                client, str(session_id), "answer_question", question="오류 코드"
            )
        async with mcp_client() as client:
            searched = await call_tool(
                client, str(session_id), "search_documents", question="오류 코드"
            )

        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT source, llm_model, answer, tokens_in, tokens_out, "
                        "generate_ms FROM traces WHERE user_id = :uid "
                        "ORDER BY created_at"
                    ).bindparams(uid=user.id)
                )
            ).mappings().all()
        await drop_users(user)
        return answered, searched, [dict(r) for r in rows]

    answered, searched, rows = run_async(scenario)
    payload = _result(answered)
    assert payload["answer"] == ANSWER
    assert payload["refused"] is False
    assert payload["citations"], "접지된 청크가 있으면 인용이 붙어야 한다"
    assert _result(searched)["hits"]

    assert [r["source"] for r in rows] == [SOURCE_MCP_ANSWER, SOURCE_MCP_SEARCH]
    answer_row, search_row = rows
    # 서버 생성 쪽만 모델·답변·토큰·생성 시간을 남긴다. 이 넷이 W6 의
    # 비용·지연 축을 리포트 없이 DB 에서 되살릴 수 있게 하는 값들이다.
    assert answer_row["llm_model"] == settings.llm_model
    assert answer_row["answer"] == ANSWER
    assert (answer_row["tokens_in"], answer_row["tokens_out"]) == (11, 7)
    assert answer_row["generate_ms"] is not None
    assert search_row["llm_model"] is None
    assert search_row["tokens_in"] == 0 and search_row["tokens_out"] == 0
    assert search_row["generate_ms"] is None


def test_the_source_list_says_which_side_generated():
    """값을 더할 때 어느 편인지 정하게 만드는 잠금.

    ``mcp_answer`` 를 ``MCP_SOURCES`` 에만 넣고 서버 생성 집합에 넣는 것을
    잊으면, "MCP 트래픽"과 "서버가 생성한 트래픽"이 조용히 같은 뜻이 된다.
    """
    assert SOURCE_MCP_ANSWER in TRACE_SOURCES
    assert MCP_SOURCES == {SOURCE_MCP_SEARCH, SOURCE_MCP_ANSWER}
    assert SOURCE_MCP_ANSWER in SERVER_GENERATED_SOURCES
    # 유일하게 호출자가 생성하는 소스.
    assert SOURCE_MCP_SEARCH not in SERVER_GENERATED_SOURCES


# --- 가드레일이 프로덕션 것 그대로다 --------------------------------------


def test_ungrounded_context_refuses_without_calling_the_model(stub_models, stub_llm):
    """접지선 아래면 LLM 을 부르지 않고 거부한다 — services/generate.py 의 규칙.

    이 경로가 살아 있다는 것이 "서버 생성은 거부를 통제할 수 있다"는 W6 의
    주장 자체다. 여기서 모델을 부르면 그 주장은 프롬프트에 대한 희망이 된다.
    """

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "mcp-refuse")
            session_id = await make_session(db, user)
            await make_chunk(db, user, "무관한 내용")

        # 접지선을 코사인 상한 위로 올리면 어떤 점수도 넘지 못한다(스텁
        # 임베딩은 질의와 청크가 같은 벡터라 유사도가 정확히 1.0 이므로
        # 1.0 으로는 모자란다 — 부등호가 >= 다). 청크를 고치는 대신 문턱을
        # 올리는 이유는, 검색이 실제로 무언가를 찾은 상태에서 **생성 단계의
        # 가드레일만** 발동시키기 위해서다.
        original = settings.min_score
        settings.min_score = 2.0
        try:
            async with mcp_client(variant=variants.SERVER_ANSWER) as client:
                response = await call_tool(
                    client, str(session_id), "answer_question", question="무엇"
                )
        finally:
            settings.min_score = original

        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT source, refused, answer FROM traces "
                        "WHERE user_id = :uid"
                    ).bindparams(uid=user.id)
                )
            ).mappings().all()
        await drop_users(user)
        return response, [dict(r) for r in rows]

    response, rows = run_async(scenario)
    payload = _result(response)
    assert payload["refused"] is True
    assert payload["answer"] == generate.REFUSAL_TEXT
    assert payload["citations"] == []
    assert stub_llm == [], "거부는 모델을 부르지 않고 나야 한다"
    assert rows and rows[0]["source"] == SOURCE_MCP_ANSWER
    assert rows[0]["refused"] is True


def test_an_unowned_document_is_not_found_not_forbidden(stub_models, stub_llm):
    """스코프 판정도 파이프라인의 것 그대로다 — 남의 문서는 '없는 것'이다."""

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "mcp-answer-scope")
            session_id = await make_session(db, user)
            await make_chunk(db, user, "내 문서")

        async with mcp_client(variant=variants.SERVER_ANSWER) as client:
            response = await call_tool(
                client,
                str(session_id),
                "answer_question",
                question="무엇",
                document_id=str(uuid.uuid4()),
            )
        async with SessionLocal() as db:
            traces = (
                await db.execute(
                    text("SELECT count(*) FROM traces WHERE user_id = :uid")
                    .bindparams(uid=user.id)
                )
            ).scalar_one()
            usage = (
                await db.execute(
                    text("SELECT count(*) FROM usage_events WHERE user_id = :uid")
                    .bindparams(uid=user.id)
                )
            ).scalar_one()
        await drop_users(user)
        return response, traces, usage

    response, traces, usage = run_async(scenario)
    assert "No such document" in _error_text(response)
    assert stub_llm == []
    assert traces == 0
    # 일어나지 않은 질의에 쿼터가 매겨지면 안 된다 — release_reservation 이
    # 모든 실패 경로에서 불린다는 주장의 증인.
    assert usage == 0


def test_a_provider_failure_says_the_model_is_down_not_the_search(
    stub_models, monkeypatch
):
    """생성 경로에만 있는 실패를 검색용 문구로 안내하지 않는다.

    ``_to_tool_error`` 의 503 안내문은 "검색 백엔드가 죽었다"고 말한다. 생성이
    죽었을 때 그 말을 에이전트가 사용자에게 옮기면, 우리가 거짓말을 시킨 것이
    된다. 그래서 생성 전용 번역기가 따로 있고, 이것이 그 증인이다.
    """
    from fastapi import HTTPException, status as http_status

    async def dead_model(system_prompt, user_prompt, *, model=None):
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="model unavailable",
        )

    monkeypatch.setattr(llm, "generate", dead_model)

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "mcp-answer-503")
            session_id = await make_session(db, user)
            await make_chunk(db, user, "문서 내용")

        async with mcp_client(variant=variants.SERVER_ANSWER) as client:
            response = await call_tool(
                client, str(session_id), "answer_question", question="무엇"
            )
        async with SessionLocal() as db:
            usage = (
                await db.execute(
                    text(
                        "SELECT count(*) FROM usage_events WHERE user_id = :uid "
                        "AND settled_at IS NOT NULL"
                    ).bindparams(uid=user.id)
                )
            ).scalar_one()
        await drop_users(user)
        return response, usage

    response, settled = run_async(scenario)
    message = _error_text(response)
    assert "answering model is unavailable" in message
    assert "search backend" not in message
    # 생성이 실패한 질의는 쿼터를 확정하지 않는다.
    assert settled == 0
