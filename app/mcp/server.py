"""Builds the MCP server and its ASGI app.

What ``main.py`` is to the FastAPI app, this is to the MCP one: it assembles
the pieces and makes the transport-level decisions, and holds no behaviour of
its own.

``build_mcp_server()`` is a function rather than a module-level singleton
because ``streamable_http_app()`` must be called exactly once per instance (it
is what creates the session manager, and ``session_manager.run()`` is
once-per-instance too). A factory makes that lifecycle explicit and lets the
tests build a throwaway server instead of sharing the process-wide one.
"""

from mcp.server.auth.settings import AuthSettings
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from app.config import settings
from app.mcp import tools
from app.mcp.auth import SessionTokenVerifier
from app.mcp.descriptions import SERVER_INSTRUCTIONS

# 설정이 비었을 때 쓰는 로컬호스트 허용 목록. SDK 가 host="127.0.0.1" 기본값
# 에서 만들어 주는 것과 같은 값에, 포트 없는 형태를 더했다 — SDK 의 "base:*"
# 와일드카드는 포트가 **있을 때만** 맞아서, 80/443 으로 들어온 Host: localhost
# 는 걸러진다. 우리는 transport_security 를 항상 직접 넘기므로 그 기본값을
# 물려받지 못하고, 여기 적힌 것이 곧 기본값이다.
_LOCALHOST_HOSTS = [
    "127.0.0.1:*",
    "localhost:*",
    "[::1]:*",
    "127.0.0.1",
    "localhost",
    "[::1]",
]
_LOCALHOST_ORIGINS = [
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
]


def _transport_security() -> TransportSecuritySettings:
    """Host/Origin policy for the streamable HTTP transport.

    DNS rebinding protection is always on. The only question this answers is
    *what* is allowed, and the answer when nothing is configured is
    "localhost" — so a clone runs locally with no setup, and a public
    deployment that forgot to configure it fails loudly with 421 rather than
    quietly accepting any Host. See the ``mcp_allowed_hosts`` comment in
    ``config.py`` for why that direction was chosen.
    """
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.mcp_allowed_hosts or _LOCALHOST_HOSTS,
        allowed_origins=settings.mcp_allowed_origins or _LOCALHOST_ORIGINS,
    )


def _auth_settings() -> AuthSettings:
    """Auth wiring for a server that issues nothing.

    ``resource_server_url=None`` is the SDK's switch for "do not publish RFC
    9728 protected-resource metadata", which is right while the token *is* a
    session row: there is no authorization server to point a client at. W7
    turns this on by filling in ``MCP_RESOURCE_SERVER_URL`` — no code change,
    which is the point of routing it through settings now.

    ``validate_token_resource`` stays off for the same reason: our tokens carry
    no ``resource`` claim to validate against.
    """
    return AuthSettings(
        issuer_url=settings.mcp_issuer_url,
        resource_server_url=settings.mcp_resource_server_url or None,
        validate_token_resource=False,
    )


def _cache_hints() -> dict[str, CacheHint]:
    """The ``ttlMs`` / ``cacheScope`` hints M7 W2 requires on list responses.

    Scope is ``"private"``, and that is a decision rather than an oversight:
    our tool list happens to be static today (one tool, a fixed schema, nothing
    read from the user's data), so ``"public"`` would be accurate *right now*.
    It is a bet that it stays static, and the list already sits behind a
    per-user bearer token. The moment the list becomes user-dependent — W6 may
    add a generation tool, and a verification-gated or plan-gated tool set is
    an obvious way for that to land — a ``"public"`` hint already sitting in a
    shared cache serves one user's tool list to another, with no error
    anywhere. ``"private"`` costs one cache entry per user and nothing else.
    Same judgement as ``session_cookie_secure``: fail safe when someone
    forgets. (W7 확인 항목: cacheScope private.)

    Only ``tools/list`` is hinted. ``server/discover`` is left at the SDK's own
    default rather than given a TTL here — it is answered once per connection,
    so caching it buys nothing measurable and would only add a second number to
    keep in step with deploys.
    """
    return {
        "tools/list": CacheHint(
            ttl_ms=settings.mcp_tools_cache_ttl_ms, scope="private"
        )
    }


def build_mcp_server() -> MCPServer:
    """Create the server with its tools registered, but no ASGI app yet."""
    mcp = MCPServer(
        name="docs-rag",
        title="Docs Q&A RAG",
        instructions=SERVER_INSTRUCTIONS,
        version="0.1.0",
        # auth 와 token_verifier 는 반드시 짝이다 — auth 없이 검증기만 주면
        # SDK 가 생성 시점에 ValueError 를 던진다.
        auth=_auth_settings(),
        token_verifier=SessionTokenVerifier(),
        cache_hints=_cache_hints(),
    )
    tools.register(mcp)
    return mcp


# 이 엔드포인트가 실제로 받는 HTTP 메서드. SDK 소스에서 확정한 것이지 추측이
# 아니다 — 우리 구성(modern era + stateless_http=True)에서 일하는 것은 POST
# 하나뿐이다:
#
#   modern (2026-07-28, 지금 클라이언트들이 쓰는 경로)
#     _streamable_http_modern.py: `if request.method != "POST"` 면 JSON-RPC
#     파싱 전에 405(Allow: POST). 애초에 POST 전용이다.
#   legacy (≤2025-11-25 클라이언트가 붙었을 때. stateless 라 이 경로로 온다)
#     DELETE → _handle_delete_request 가 mcp_session_id 없음을 보고 항상 405
#              ("Session termination not supported"). 무세션이라 끝낼 세션이
#              없다.
#     GET    → 독립 SSE 스트림을 여는 용도인데, streamable_http_manager.py 의
#              주석이 그대로 말한다: "the legacy stateless path never opens a
#              GET stream". 열어 두면 아무도 쓰지 않을 스트림이 매달린다.
#
# 그래서 POST 만 연다. GET/DELETE 는 Starlette 라우터가 405(Allow: POST)로
# 거절하는데, 이는 서브앱에 들여보냈을 때 나올 답과 **같은 답**이다 — 다만
# 인증 미들웨어와 전송 계층을 거치지 않고 더 일찍 끝난다. 쓰지도 않을 메서드를
# 열어 두면 405 로 거절돼야 할 요청이 서브앱까지 들어간다.
MCP_HTTP_METHODS = ["POST"]


def build_mcp_app(mcp: MCPServer) -> Starlette:
    """Create the ASGI app for ``mcp``. Call once per server instance.

    Calling this is also what creates ``mcp.session_manager``, which the host
    application's lifespan has to run — see ``main.py``. Reaching for
    ``session_manager`` before this returns is a ``RuntimeError``.
    """
    return mcp.streamable_http_app(
        # 서브앱 **안에서의** 경로를 실제 공개 경로와 같게 둔다. 이 앱은
        # Mount 가 아니라 Route 로 걸리는데(근거는 main.py), Route 는 Mount 와
        # 달리 접두사를 떼지 않고 원래 scope 를 그대로 넘긴다 — 즉 서브앱이
        # 보는 경로가 곧 공개 경로다. 두 값이 어긋나면 서브앱 내부 라우터가
        # 자기 요청을 못 알아본다.
        streamable_http_path=settings.mcp_path,
        # 2026-07-28 경로에서는 no-op 이다(modern era 는 구조적으로 무세션이고
        # Mcp-Session-Id 를 아예 보내지 않는다). 레거시(≤2025-11-25)
        # 클라이언트까지 무세션으로 받으려고 켜 둔다 — W2 의 목표가 "핸드셰이크·
        # 세션 없음"이므로, 오래된 클라이언트가 붙었을 때만 그 조건이 깨지는
        # 상태를 남겨 둘 이유가 없다.
        stateless_http=True,
        transport_security=_transport_security(),
    )
