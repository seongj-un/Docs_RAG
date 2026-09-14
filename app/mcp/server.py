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

from urllib.parse import urlparse

from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from app.config import settings
from app.mcp import oauth, tools, variants
from app.mcp.auth import SessionTokenVerifier
from app.mcp.oauth import AUTH_MODE_OAUTH, AUTH_MODE_SESSION, CompositeTokenVerifier
from app.mcp.scopes import ScopeGate
from app.mcp.variants import ToolVariant

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
    """Auth wiring for a server that issues nothing — in either mode.

    ``resource_server_url`` is the SDK's on/off switch for two things at once:
    the RFC 9728 ``/.well-known/oauth-protected-resource...`` route
    (``create_protected_resource_routes``) and the ``resource_metadata=``
    parameter on a 401's ``WWW-Authenticate`` (``RequireAuthMiddleware``). Both
    exist to tell a client *where to go get a token*, so they are tied to
    ``MCP_AUTH_MODE`` rather than to the URL being filled in: in ``session``
    mode there is no authorization server, and pointing a client at one that
    does not exist sends it into a discovery loop instead of the session bearer
    it could have used. ``oauth.validate_settings`` warns if the URL is set and
    the mode leaves it unused, so nothing is ignored quietly.

    ``validate_token_resource`` — the middleware check that
    ``AccessToken.resource`` equals ``resource_server_url`` — is on only in pure
    ``oauth`` mode. In ``both`` mode it cannot be: a session token carries no
    resource indicator (it is not an OAuth token and has no audience), so the
    middleware would reject every session bearer. Turning it off there is the
    SDK's own documented option for this case ("set it to False when your token
    verifier checks the token's audience itself"), and ``JwtTokenVerifier`` does
    exactly that check with ``jwt.decode(audience=...)`` — the SDK's is a second
    opinion, not the only one. That the second opinion is unavailable in
    ``both`` is a real cost of running two credential types at once, and a
    reason to treat ``both`` as a migration window rather than a destination.
    """
    mode = settings.mcp_auth_mode
    if mode == AUTH_MODE_SESSION:
        return AuthSettings(
            issuer_url=settings.mcp_issuer_url,
            resource_server_url=None,
            validate_token_resource=False,
        )
    return AuthSettings(
        issuer_url=settings.mcp_oauth_issuer,
        resource_server_url=settings.mcp_resource_server_url,
        validate_token_resource=(mode == AUTH_MODE_OAUTH),
        # ⚠️ 일부러 None 이다. 값을 넣으면 SDK 가 **전송 계층에서** 403
        # insufficient_scope 를 낸다(RequireAuthMiddleware). 그러면 읽기
        # 스코프만 든 토큰은 tools/list 조차 못 부른다 — W7 이 요구한 것은
        # 목록이 스코프에 따라 **달라지는** 것이지 목록 자체가 막히는 것이
        # 아니다. 툴 단위 게이팅은 ScopeGate + scoped_tool 이 한다.
        #
        # 치르는 값: SDK 는 이 목록을 RFC 9728 문서의 scopes_supported 로도
        # 쓰므로(lowlevel/server.py 가 그대로 넘긴다), 그 필드가 null 로
        # 나간다. 즉 메타데이터는 "어떤 스코프가 있는지"를 말하지 못하고,
        # 클라이언트는 tools/list 의 차이로 알아내거나 README 를 읽어야 한다.
        # SDK 가 두 가지를 한 값에 묶어 둔 결과이고, 둘 중 하나만 고를 수 있다면
        # 전송 계층에서 통째로 막지 않는 쪽이 낫다고 판단했다.
        required_scopes=None,
    )


def _token_verifier() -> TokenVerifier:
    """The verifier(s) this deployment accepts tokens from.

    ``session`` 은 W2 가 만든 경로 그대로다. ``oauth`` 는 그것을 끊고 IdP 토큰만
    받는다. ``both`` 는 둘 다 받되 순서는 비용 문제일 뿐이다 — 근거는
    ``oauth.CompositeTokenVerifier``.
    """
    mode = settings.mcp_auth_mode
    if mode == AUTH_MODE_SESSION:
        return SessionTokenVerifier()
    if mode == AUTH_MODE_OAUTH:
        return oauth.build_oauth_verifier()
    return CompositeTokenVerifier(
        oauth.build_oauth_verifier(), SessionTokenVerifier()
    )


def _cache_hints() -> dict[str, CacheHint]:
    """The ``ttlMs`` / ``cacheScope`` hints M7 W2 requires on list responses.

    Scope is ``"private"``. W2 chose that as a bet — the list was static then,
    so ``"public"`` would have been accurate, and private was the cheap way to
    stay safe if that ever changed. **W7 is when it changed.** ``ScopeGate``
    now filters the list by the caller's scopes, so two tokens get two
    different lists from the same URL; a ``"public"`` hint sitting in a shared
    cache would hand a read-only caller the list built for a caller with write
    scope, with no error anywhere. The bet paid off, and the assertion that
    pins it is no longer a formality — ``tests/test_mcp_scopes.py``.
    (W7 확인 항목: cacheScope private.)

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


def build_mcp_server(variant: ToolVariant | None = None) -> MCPServer:
    """Create the server with its tools registered, but no ASGI app yet.

    ``variant`` exists for the M7 W4 A/B and defaults to what production ships
    (``variants.PRODUCTION``). ``main.py`` never passes it, so the deployed
    server cannot pick up an experimental tool surface by accident — the only
    caller that passes one is ``eval/l3_run.py``. The instructions come from the
    variant for the same reason the tools do: a server that lists three tools
    while its instructions say "there is exactly one tool" is lying to the
    agent, and that lie would land in the measurement.
    """
    # 기동 시점에 판정한다. 요청이 오고 나서 "설정이 모자라 검증할 수 없다"를
    # 깨닫는 것은 이미 늦다 — 근거는 oauth.validate_settings.
    oauth.validate_settings()

    variant = variant or variants.PRODUCTION

    mcp = MCPServer(
        name="docs-rag",
        title="Docs Q&A RAG",
        instructions=variant.instructions,
        version="0.1.0",
        # auth 와 token_verifier 는 반드시 짝이다 — auth 없이 검증기만 주면
        # SDK 가 생성 시점에 ValueError 를 던진다.
        auth=_auth_settings(),
        token_verifier=_token_verifier(),
        cache_hints=_cache_hints(),
        # SDK 내장(OpenTelemetry → RequestStateBoundary) **안쪽**에서 돈다.
        # 그래서 OTel 스팬은 걸러지기 **전**의 요청을 보고, 우리가 목록에서
        # 툴을 지운 사실이 스팬 기준에 영향을 주지 않는다.
        middleware=[ScopeGate()],
    )
    tools.register(mcp, variant)
    return mcp


def resource_metadata_path() -> str | None:
    """Public path of the RFC 9728 document, or None when it is not published.

    ⚠️ 이걸 왜 밖으로 꺼내는가. SDK 는 이 라우트를 **서브앱 안에** 등록한다
    (``lowlevel/server.py``: ``routes.extend(create_protected_resource_routes(...))``).
    그런데 우리는 서브앱을 캐치올 Mount 가 아니라 ``Route("/mcp", POST)`` 하나로
    걸었다(근거는 app/main.py). 즉 서브앱에는 POST /mcp 말고는 아무 요청도
    도달하지 않고, 서브앱 안의 well-known 라우트는 등록돼 있으나 **닿을 수 없다**.
    증상이 없는 종류의 고장이다 — 앱은 멀쩡히 뜨고 401 의 WWW-Authenticate 는
    메타데이터 위치를 정확히 안내하는데, 그 주소가 404 를 낸다.

    그래서 호스트 앱이 같은 경로를 하나 더 걸어 같은 서브앱으로 넘긴다. Route 는
    접두사를 떼지 않으므로 서브앱이 보는 경로가 곧 공개 경로이고, 서브앱 내부
    라우터가 자기 것으로 알아본다. 경로 문자열은 SDK 가 쓰는 함수로 직접
    만들어서, RFC 9728 §3.1 의 삽입 규칙을 우리가 따로 구현하지 않는다.
    """
    if settings.mcp_auth_mode == AUTH_MODE_SESSION:
        return None
    from mcp.server.auth.routes import build_resource_metadata_url
    from pydantic import AnyHttpUrl

    url = build_resource_metadata_url(AnyHttpUrl(settings.mcp_resource_server_url))
    return urlparse(str(url)).path


# 메타데이터 문서가 받는 메서드. SDK 가 서브앱 안에서 여는 것과 같게 둔다
# (CORS 프리플라이트 때문에 OPTIONS 가 함께 있다).
RESOURCE_METADATA_METHODS = ["GET", "OPTIONS"]


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
