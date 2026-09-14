"""OpenTelemetry spans and the confused-deputy boundary (M7 W7). Requires Postgres.

⚠️ **이 모듈은 전역 TracerProvider 를 설치한다.** opentelemetry 는 한 프로세스에
하나만 두고, 한 번 설치되면 ProxyTracer 가 실제 tracer 를 캐시해 버려서 되돌릴
수 없다(``opentelemetry/trace/__init__.py`` 의 ``_TRACER_PROVIDER``, ProxyTracer.
``_real_tracer``). 즉 이 파일이 돈 뒤의 테스트들은 계측이 켜진 상태로 돈다.

그래도 되는 이유와, 그것이 오히려 유용한 이유: 켜져 있어도 앱 동작은 달라지지
않아야 한다는 것이 W7 의 요구 자체다. 뒤따라 도는 MCP 테스트들이 계측이 켜진
채로 통과하는 것이 그 주장의 덤으로 붙는 증인이다. 내보내는 곳은 메모리라
프로세스 밖으로 나가는 것은 없고, 테스트마다 비운다.

여기서 확인하는 경계:

*SDK 가 하는 것* — tools/call 스팬, 그 이름과 gen_ai.* 속성, ``_meta`` 의
``traceparent`` 추출. 우리는 이것을 **다시 하지 않는다**(중복 계측은 스팬을 두
번 만든다). 대신 SDK 가 실제로 하고 있는지를 확인한다.

*우리가 더한 것* — 단계 스팬(embed/retrieve/...)이 그 툴 스팬 아래 붙는 것,
도메인 속성(테넌트 해시·top_k·청크 수·토큰 수·지연)이 툴 스팬에 실리는 것,
그리고 계측이 꺼져 있으면 이 전부가 no-op 인 것.
"""

import re
import uuid

import httpx
import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.config import settings
from app.db import SessionLocal
from app.services import otel
from tests.mcp_fixture import (
    db_available,
    drop_users,
    make_chunk,
    make_session,
    make_user,
    mcp_client,
    run_async,
    search,
    stub_models,  # noqa: F401 - 픽스처를 이 모듈 이름공간으로 끌어온다
)

pytestmark = pytest.mark.skipif(not db_available(), reason="Postgres not reachable")

_EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def tracing_on():
    """Install the provider once for this module. See the module docstring.

    ``set_tracer_provider`` 은 once-guard 가 있어 두 번째 호출이 조용히 무시되고
    경고만 남는다. 테스트가 "provider 를 넣었다고 믿는데 안 들어간" 상태로 도는
    것이 가장 나쁘므로, 전역을 직접 잡아 그 경우를 없앤다.
    """
    provider = TracerProvider(resource=Resource.create({"service.name": "docs-rag-test"}))
    # SimpleSpanProcessor: Batch 와 달리 스팬이 끝나는 즉시 내보내므로, 테스트가
    # flush 를 기다릴 필요가 없다.
    provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
    otel_trace._TRACER_PROVIDER = provider
    yield provider


@pytest.fixture(autouse=True)
def fresh_spans():
    _EXPORTER.clear()
    yield
    _EXPORTER.clear()


def _spans_by_name() -> dict[str, object]:
    return {span.name: span for span in _EXPORTER.get_finished_spans()}


async def _one_search(question: str = "관측", *, meta=None):
    async with SessionLocal() as db:
        user = await make_user(db, "w7-otel")
        session_id = await make_session(db, user)
        chunk = await make_chunk(db, user, "관측성 시험용 문서")

    async with mcp_client() as client:
        response = await search(client, str(session_id), question=question, meta=meta)

    await drop_users(user)
    return response, user, chunk


# --- SDK 가 이미 해 주는 것 ------------------------------------------------


def test_sdk_opens_exactly_one_span_per_tool_call(stub_models):
    """중복 계측을 하지 않았다는 증인.

    SDK 의 OpenTelemetryMiddleware 가 기본으로 켜져 있어서 tools/call 스팬은
    이미 있다. 우리가 툴 호출을 또 감쌌다면 같은 호출에 스팬이 두 개 생긴다 —
    지연이 두 번 세어지고, W4 의 L3 호출 수가 두 배가 된다.
    """
    run_async(lambda: _one_search())

    calls = [
        s
        for s in _EXPORTER.get_finished_spans()
        if s.attributes.get("gen_ai.tool.name") == "search_documents"
    ]
    assert len(calls) == 1
    tool_span = calls[0]
    assert tool_span.name == "tools/call search_documents"
    assert tool_span.attributes["mcp.method.name"] == "tools/call"
    assert tool_span.attributes["gen_ai.operation.name"] == "execute_tool"


def test_client_traceparent_becomes_the_parent(stub_models):
    """W7: OTel 컨텍스트를 ``_meta`` 의 ``traceparent`` 로 전파한다.

    SDK 가 ``mcp/shared/_otel.py`` 의 extract 로 이미 한다 — 우리가 더할 코드는
    없고, 대신 그 사실이 참인지를 고정한다. 이 단언이 깨지면 에이전트의 트레이스
    와 우리 트레이스가 서로 다른 trace_id 로 갈라져, 한 요청을 끝까지 따라가는
    것이 불가능해진다.
    """
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    span_id = "00f067aa0ba902b7"
    incoming = {"traceparent": f"00-{trace_id}-{span_id}-01"}

    run_async(lambda: _one_search(meta=incoming))

    by_name = _spans_by_name()
    # 툴 호출과, 그 아래 매달린 우리 단계 스팬까지 전부 같은 트레이스여야 한다.
    # 한 요청 안에서만 본다 — 이 클라이언트가 보내는 다른 요청(tools/list 등)은
    # 자기 _meta 를 갖고 있고 이 단언의 대상이 아니다.
    for name in ("tools/call search_documents", "docs_rag.embed", "docs_rag.retrieve"):
        assert name in by_name, f"{name} 스팬이 없다"
        assert format(by_name[name].context.trace_id, "032x") == trace_id

    # 원격 부모가 실제로 붙었는지. trace_id 만 맞고 부모가 비어 있으면 스팬은
    # 같은 트레이스의 두 번째 루트가 되어, 에이전트 쪽 스팬 아래에 들어가지 않는다.
    tool = by_name["tools/call search_documents"]
    assert format(tool.parent.span_id, "016x") == span_id
    assert tool.parent.is_remote


def test_traceparent_is_never_used_for_identity(stub_models):
    """W7 필수: ``traceparent`` 는 클라이언트가 주는 입력이다.

    같은 요청에 남이 준 트레이스 컨텍스트를 실어 보내도, 테넌트 속성은 여전히
    **토큰에서** 나온 사용자여야 한다. 헤더/메타에서 신원을 읽는 경로가 하나라도
    생기면 이 단언이 먼저 깨진다.
    """
    incoming = {
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        # 진짜 클라이언트라면 여기에 무엇이든 적어 보낼 수 있다.
        "tracestate": "tenant=somebody-else",
    }
    _, user, _ = run_async(lambda: _one_search(meta=incoming))

    tool_span = next(
        s
        for s in _EXPORTER.get_finished_spans()
        if s.attributes.get("gen_ai.tool.name") == "search_documents"
    )
    assert tool_span.attributes["docs_rag.tenant"] == otel.tenant_tag(user.id)


# --- 우리가 더한 것 --------------------------------------------------------


def test_stage_spans_nest_under_the_tool_call(stub_models):
    """W7 스팬 구조: 툴 호출 → 검색 → (생성).

    생성은 MCP 경로에 없다 — 답을 쓰는 것은 호출한 에이전트다. 그래서 괄호다.
    """
    run_async(lambda: _one_search())

    by_name = _spans_by_name()
    tool = by_name["tools/call search_documents"]
    assert "docs_rag.embed" in by_name
    assert "docs_rag.retrieve" in by_name
    # MCP 툴은 생성하지 않는다. 스팬이 생겼다면 이 툴이 LLM 을 부르기 시작한
    # 것이고, 그건 W6 의 결정이지 사고로 생길 일이 아니다.
    assert "docs_rag.generate" not in by_name

    for stage in ("docs_rag.embed", "docs_rag.retrieve"):
        assert by_name[stage].parent.span_id == tool.context.span_id
        assert by_name[stage].context.trace_id == tool.context.trace_id


def test_tool_span_carries_the_w7_attributes(stub_models):
    _, user, _ = run_async(lambda: _one_search())

    tool = _spans_by_name()["tools/call search_documents"]
    attrs = tool.attributes

    # 툴명은 SDK 가 이미 넣는다 — 우리가 또 넣지 않는다.
    assert attrs["gen_ai.tool.name"] == "search_documents"
    # 나머지가 우리 몫이다.
    assert attrs["docs_rag.source"] == "mcp_search"
    assert attrs["docs_rag.top_k"] == settings.top_k  # dense 경로(stub_models)
    assert attrs["docs_rag.chunks"] == 1
    assert attrs["docs_rag.tokens_in"] == 0 and attrs["docs_rag.tokens_out"] == 0
    assert attrs["docs_rag.total_ms"] >= 0
    assert attrs["docs_rag.cached"] is False


def test_tenant_attribute_is_a_hash_and_the_raw_id_appears_nowhere(stub_models):
    """W7 함정: 테넌트 식별자를 원문으로 남기지 않는다.

    속성 하나만 보는 것으로는 부족하다 — 스팬 어딘가에 원문 UUID 가 섞여 나가면
    해싱은 의미가 없다. 그래서 모든 스팬의 모든 속성값을 문자열로 훑는다.
    """
    _, user, _ = run_async(lambda: _one_search())

    tag = otel.tenant_tag(user.id)
    assert tag != str(user.id)
    assert re.fullmatch(r"[0-9a-f]{16}", tag)
    # 같은 입력이면 같은 값이어야 그룹핑이 된다.
    assert tag == otel.tenant_tag(user.id)
    assert tag != otel.tenant_tag(uuid.uuid4())

    raw = str(user.id)
    for span in _EXPORTER.get_finished_spans():
        assert raw not in span.name
        for key, value in (span.attributes or {}).items():
            assert raw not in str(value), f"{span.name}.{key} 에 원문 user id 가 있다"
        # 이메일도 마찬가지다. 테넌트를 가리키는 다른 이름일 뿐이다.
        for value in (span.attributes or {}).values():
            assert user.email not in str(value)


def test_instrumentation_is_a_noop_when_disabled(monkeypatch):
    """수집기가 없는 클론에서 앱이 정상 동작해야 한다 — 기본값이 no-op 이다.

    provider 가 이미 전역에 꽂힌 뒤라 "정말 아무 일도 안 한다"를 통합으로
    보이기는 어렵다. 대신 스위치 자체를 본다: OTEL_ENABLED 가 false 면
    setup_tracing 은 provider 를 만들지도, 설치하지도 않는다.
    """
    made: list[object] = []
    monkeypatch.setattr(settings, "otel_enabled", False)
    monkeypatch.setattr(
        otel_trace, "set_tracer_provider", lambda p: made.append(p)
    )

    otel.setup_tracing()
    assert made == []

    # 그리고 활성 스팬이 없으면 stage_span 은 스팬을 만들지 않는다 — HTTP 경로가
    # 고아 루트 스팬 세 개를 뿌리지 않는 이유이자, 계측이 꺼졌을 때의 비용이
    # 0 인 이유다.
    before = len(_EXPORTER.get_finished_spans())
    with otel.stage_span("nothing_is_current") as span:
        assert span is None
    assert len(_EXPORTER.get_finished_spans()) == before


# --- confused deputy -------------------------------------------------------


class _Recorder(httpx.AsyncClient):
    """Record every request this app makes upstream, and answer it locally.

    ``httpx.AsyncClient`` 를 통째로 갈아끼운다. ``embeddings``/``rerank`` 는
    호출 시점에 ``httpx.AsyncClient`` 를 속성으로 찾으므로 이 교체가 닿고, 이
    파일이 위에서 ``from httpx import ...`` 로 직접 묶어 둔 테스트 클라이언트는
    닿지 않는다. 덕분에 **진짜 클라이언트 코드가 만든 진짜 요청**을 본다 —
    함수를 스텁하면 "무엇이 전송되는가"라는 질문 자체가 사라진다.
    """

    sent: list[httpx.Request] = []

    async def send(self, request: httpx.Request, **kwargs):
        _Recorder.sent.append(request)
        path = request.url.path
        if path.endswith("/embed_full"):
            return httpx.Response(
                200,
                json={
                    "dense": [[0.1] * settings.embed_dim],
                    "sparse": [{"indices": [1], "values": [0.5]}],
                },
                request=request,
            )
        if path.endswith("/embed"):
            return httpx.Response(200, json=[[0.1] * settings.embed_dim], request=request)
        if path.endswith("/rerank"):
            return httpx.Response(200, json=[{"index": 0, "score": 0.9}], request=request)
        raise AssertionError(f"예상하지 못한 업스트림 호출: {request.url}")


def test_the_client_token_never_reaches_an_upstream_model_server(monkeypatch):
    """W7 "절대 하지 말 것": 받은 토큰을 업스트림에 그대로 넘기면 confused deputy.

    스텁이 아니라 기록기다. 하이브리드를 켜서 임베딩 서버와 리랭커를 **둘 다**
    지나가게 한 뒤, 나간 요청 전부를 훑어 세션 토큰이 어디에도 없는지 본다 —
    헤더든 본문이든. 이 경로가 생기면 TEI 나 리랭커를 운영하는 쪽이 우리
    사용자의 자격증명을 손에 쥔다.
    """
    _Recorder.sent = []
    monkeypatch.setattr(httpx, "AsyncClient", _Recorder)
    monkeypatch.setattr(settings, "hybrid_enabled", True)

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "w7-deputy")
            session_id = await make_session(db, user)
            await make_chunk(db, user, "업스트림으로 새면 안 되는 문서")

        async with mcp_client() as client:
            response = await search(client, str(session_id), question="문서")

        await drop_users(user)
        return response, str(session_id)

    response, token = run_async(scenario)
    assert response.json()["result"]["isError"] is False

    upstream = [r for r in _Recorder.sent if r.url.host in ("127.0.0.1", "localhost")]
    # 실제로 나갔는지부터 확인한다. 0건이면 이 테스트는 아무것도 증명하지 않는다.
    paths = {r.url.path for r in upstream}
    assert "/embed_full" in paths and "/rerank" in paths

    for request in upstream:
        assert "authorization" not in {k.lower() for k in request.headers}
        assert "cookie" not in {k.lower() for k in request.headers}
        blob = "\n".join(f"{k}: {v}" for k, v in request.headers.items())
        assert token not in blob
        assert token not in request.content.decode("utf-8", "replace")
