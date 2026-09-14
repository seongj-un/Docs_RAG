"""OpenTelemetry: only what the MCP SDK does not already do for us.

**The boundary matters more than the code.** ``mcp/server/_otel.py`` ships an
``OpenTelemetryMiddleware`` that is on by default and already:

* opens a ``SpanKind.SERVER`` span per inbound message, named ``tools/call
  search_documents``;
* stamps ``mcp.method.name``, ``mcp.protocol.version``, ``jsonrpc.request.id``,
  ``gen_ai.operation.name`` and ``gen_ai.tool.name``;
* **extracts the W3C trace context from ``params._meta``** (``traceparent`` /
  ``tracestate``, via ``mcp/shared/_otel.py``), so a client's trace is the
  parent of ours without us touching the carrier;
* marks the span failed for ``MCPError``, malformed params, and an ``isError``
  tool result.

So this module adds exactly two things the SDK cannot know about: the *inside*
of a tool call (which stages ran, how long each took) and the *domain*
attributes (tenant, top_k, chunk and token counts). Instrumenting the tool call
itself again would produce two spans for one call.

**Off by default, and off means no-op — not "cheap".** ``opentelemetry-api``
resolves ``get_tracer`` against a proxy that stays a no-op until somebody
installs a ``TracerProvider``. ``setup_tracing()`` installs one only when
``OTEL_ENABLED`` is true, so a clone with no collector creates no spans, opens
no sockets and allocates nothing per request. Failing to configure an exporter
is logged and survived rather than raised: observability must never take down
the thing it observes (same rule as ``tracing.record``).
"""

import hashlib
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Span

from app.config import settings

logger = logging.getLogger(__name__)

SERVICE_NAME = "docs-rag"

# 계측 이름. 스팬을 낸 라이브러리를 가리키는 값이라 SDK 의 "mcp-python-sdk" 와
# 달라야 한다 — 같게 두면 어느 쪽이 만든 스팬인지 백엔드에서 구별되지 않는다.
tracer = trace.get_tracer(SERVICE_NAME)

# 우리가 붙이는 속성의 접두사. mcp.* 와 gen_ai.* 는 각각 SDK 와 시맨틱 컨벤션이
# 소유한 이름 공간이라, 거기에 우리 뜻을 덧씌우면 표준 대시보드가 조용히 틀린
# 것을 그린다.
ATTR_PREFIX = "docs_rag."


def tenant_tag(user_id: uuid.UUID | str) -> str:
    """A stable, non-reversible stand-in for a user id, for span attributes.

    W7 함정 항목: 테넌트 식별자를 원문으로 남기지 않는다. 트레이스는 보통
    외부 백엔드로 나가고, 거기에 ``users.id`` 를 적는 순간 그 백엔드는 우리
    기본키의 사본을 갖게 된다 — 조회 권한이 갈라지는 지점에서 조인이 가능한
    값을 흘리는 것이 문제이지, id 자체가 비밀이어서가 아니다.

    blake2s 를 고른 이유:

    * 표준 라이브러리다. 이 한 줄을 위해 의존성을 늘리지 않는다.
    * ``person=`` (개인화)을 받는다. 같은 UUID 라도 다른 용도의 blake2s 다이
      제스트와 값이 달라서, 다른 곳에서 새어 나간 해시와 대조되지 않는다.
    * ``digest_size=`` 로 길이를 고를 수 있다. 8바이트(16 hex)면 트레이스 UI
      한 줄에 들어가고, 우리 규모에서 충돌 확률은 무시할 수 있다.

    솔직한 한계: **키가 없는 해시다.** 사용자 id 목록을 이미 가진 사람은 추측을
    확인할 수 있다. 목적은 비밀 유지가 아니라 비식별화 — 관측 백엔드가 우리
    DB 로 되돌아가는 열쇠를 보관하지 않게 하는 것이다. 키를 쓰면 더 강해지지만
    키를 돌리는 순간 과거 트레이스와 그룹이 갈라지고, 그 운영 비용을 낼 이유가
    지금은 없다.
    """
    return hashlib.blake2s(
        str(user_id).encode("utf-8"), digest_size=8, person=b"docs-rag"
    ).hexdigest()


@contextmanager
def stage_span(name: str) -> Iterator[Span | None]:
    """Open a child span for one pipeline stage — but only under a live parent.

    The parent check is what keeps the HTTP query paths from emitting three
    unrelated root traces (embed, retrieve, generate) that belong to one
    request. Nothing instruments FastAPI here — adding an ASGI instrumentation
    is a separate decision with its own cost — so on ``POST /query`` there is no
    ambient span and this stays silent. On the MCP path the SDK's ``tools/call``
    span *is* ambient, so the stages nest under it and the W7 span structure
    (툴 호출 → 검색 → (생성)) falls out with no call-site changes.

    The day someone does instrument the HTTP surface, these light up on their
    own.
    """
    if not trace.get_current_span().get_span_context().is_valid:
        yield None
        return
    with tracer.start_as_current_span(f"{ATTR_PREFIX}{name}") as span:
        yield span


def annotate_request(
    *,
    source: str,
    user_id: uuid.UUID,
    top_k: int | None,
    chunk_count: int,
    tokens_in: int,
    tokens_out: int,
    total_ms: int,
    hybrid: bool,
    cached: bool,
    refused: bool,
) -> None:
    """Stamp this request's domain facts on whatever span is current.

    On the MCP path that span is the SDK's ``tools/call search_documents`` — so
    the attributes land on the tool call itself, which is what W7 asks for,
    without us creating a span to hold them. Everywhere else the current span is
    invalid and every line below is a no-op.

    ``user_id`` arrives raw and leaves hashed. The raw value must not survive
    past this function.
    """
    span = trace.get_current_span()
    if not span.get_span_context().is_valid:
        return
    try:
        attributes: dict[str, str | int | bool] = {
            f"{ATTR_PREFIX}source": source,
            f"{ATTR_PREFIX}tenant": tenant_tag(user_id),
            f"{ATTR_PREFIX}chunks": chunk_count,
            f"{ATTR_PREFIX}tokens_in": tokens_in,
            f"{ATTR_PREFIX}tokens_out": tokens_out,
            f"{ATTR_PREFIX}total_ms": total_ms,
            f"{ATTR_PREFIX}hybrid": hybrid,
            f"{ATTR_PREFIX}cached": cached,
            f"{ATTR_PREFIX}refused": refused,
        }
        if top_k is not None:
            attributes[f"{ATTR_PREFIX}top_k"] = top_k
        span.set_attributes(attributes)
    except Exception:
        # tracing.record 와 같은 규칙: 관측이 관측 대상을 내리지 않는다. 이
        # 함수는 사용자의 질의 한가운데(커밋 직전)에서 불리므로, 여기서 터지면
        # 성공한 검색이 500 이 된다.
        logger.exception("failed to annotate the current span")


def setup_tracing() -> None:
    """Install a TracerProvider when — and only when — one is asked for.

    Called once from the app lifespan. Returning early leaves the API's proxy
    tracer in place, which is a genuine no-op: ``start_as_current_span`` hands
    back the invalid span and every helper above short-circuits on it.
    """
    if not settings.otel_enabled:
        return

    # 늦은 import 다. opentelemetry-sdk 는 계측을 켠 배포에서만 필요하고,
    # 모듈 최상단에서 끌어오면 끄고 쓰는 쪽까지 import 비용을 낸다.
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )

    exporter = None
    if settings.otel_exporter == "console":
        exporter = ConsoleSpanExporter()
    elif settings.otel_exporter == "otlp":
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter()
        except ImportError:
            # 죽이지 않는다. 관측이 관측 대상을 내리면 안 된다는 규칙이
            # tracing.record 와 같다 — 대신 무엇을 설치해야 하는지 이름을
            # 대고 계측 없이 계속 간다.
            logger.error(
                "OTEL_EXPORTER=otlp 인데 opentelemetry-exporter-otlp-proto-http 가 "
                "없다. 스팬을 만들되 내보내지 않는다."
            )
    else:
        logger.error("알 수 없는 OTEL_EXPORTER=%r — 스팬을 내보내지 않는다.",
                     settings.otel_exporter)

    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    logger.info(
        "OpenTelemetry 켜짐 (service=%s exporter=%s)",
        settings.otel_service_name,
        settings.otel_exporter,
    )
