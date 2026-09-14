"""OAuth 2.0 resource-server token verification (M7 W7).

**This server is a resource server. It issues nothing.** There is no
``/authorize``, no ``/token``, no client registration and no consent screen
anywhere in this repo, and W7 puts building one explicitly out of scope. What
lives here is only the other half: given a bearer token somebody else minted,
decide whether it was minted *for us*, by an issuer we trust, and what it is
allowed to do.

Validation is local (JWT + JWKS), not introspection. The trade is one
configuration value more (a JWKS URL) against an HTTP round-trip to the IdP on
every single MCP call — and the MCP path is already 8.8s of reranking
(``config.cand_k``), so the thing worth protecting is not latency but the
dependency: an IdP introspection endpoint being down would take our search down
with it, while a cached JWKS does not.

**There is no IdP in this project, so this path has never run end to end.** The
tests sign their own tokens with a throwaway key to exercise the verification
logic — that fixture is a *test double, not a substitute IdP*, and nothing in
``app/`` can issue a token. If that ever stops being true, this docstring is
wrong and the thing that made it wrong is a production token-issuing path that
W7 told us not to build.

Confused deputy (the W7 "절대 하지 말 것"): the token verified here is used for
exactly one thing — deciding *which local user* the request acts as. It is never
attached to a call to TEI, the reranker or Gemini. Those are reached through
``services/embeddings.py``, ``services/rerank.py`` and ``services/llm.py``,
none of which take a credential from the request; ``tests/test_mcp_w7.py``
pins that by recording every outbound HTTP request made during a tool call.
"""

import asyncio
import logging
from typing import Any

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

from app.config import settings
from app.db import SessionLocal
from app.services import auth

logger = logging.getLogger(__name__)

AUTH_MODE_SESSION = "session"
AUTH_MODE_OAUTH = "oauth"
AUTH_MODE_BOTH = "both"
AUTH_MODES = (AUTH_MODE_SESSION, AUTH_MODE_OAUTH, AUTH_MODE_BOTH)


class OAuthConfigError(RuntimeError):
    """Auth is configured to verify OAuth tokens but cannot: refuse to start."""


def oauth_enabled() -> bool:
    return settings.mcp_auth_mode in (AUTH_MODE_OAUTH, AUTH_MODE_BOTH)


def audience() -> str:
    """The ``aud`` a token must carry to be accepted here.

    RFC 8707 의 리소스 지시자는 곧 리소스 서버의 URL 이므로 기본은
    ``mcp_resource_server_url`` 이다. 따로 둘 수 있게 한 것은 aud 를 다른 값
    (클라이언트 id, API 식별자)으로 채우는 IdP 가 있어서다.
    """
    return settings.mcp_oauth_audience or settings.mcp_resource_server_url


def validate_settings() -> None:
    """Fail at startup, not at the first request, when auth cannot work.

    The dangerous state this exists to make impossible: a deployment that
    believes it validates IdP tokens while some value is blank and the check
    silently degrades. There is no degraded mode here — either every piece is
    present or the process does not come up.
    """
    mode = settings.mcp_auth_mode
    if mode not in AUTH_MODES:
        raise OAuthConfigError(
            f"MCP_AUTH_MODE={mode!r} 은 없는 값이다. 하나를 고를 것: "
            + ", ".join(AUTH_MODES)
        )
    if mode == AUTH_MODE_SESSION:
        if settings.mcp_resource_server_url:
            # 조용히 무시하지 않는다. 켰다고 믿는 사람이 있을 수 있다.
            logger.warning(
                "MCP_RESOURCE_SERVER_URL 이 설정돼 있지만 MCP_AUTH_MODE=session "
                "이라 RFC 9728 메타데이터를 내보내지 않는다 — 가리킬 인가 서버가 "
                "없기 때문이다. 켜려면 MCP_AUTH_MODE 를 oauth 또는 both 로."
            )
        return

    missing = [
        name
        for name, value in (
            ("MCP_OAUTH_ISSUER", settings.mcp_oauth_issuer),
            ("MCP_OAUTH_JWKS_URL", settings.mcp_oauth_jwks_url),
            # 둘 중 하나만 있어도 audience() 가 값을 만든다.
            ("MCP_RESOURCE_SERVER_URL", settings.mcp_resource_server_url),
            ("MCP_OAUTH_AUDIENCE 또는 MCP_RESOURCE_SERVER_URL", audience()),
        )
        if not value
    ]
    if missing:
        raise OAuthConfigError(
            f"MCP_AUTH_MODE={mode!r} 인데 다음이 비어 있다: {', '.join(missing)}. "
            "리소스 서버는 발급자·서명 키·대상 없이는 토큰을 검증할 수 없고, "
            "검증할 수 없는 채로 요청을 받는 것보다 기동하지 않는 편이 낫다."
        )
    if not settings.mcp_oauth_algorithms:
        raise OAuthConfigError(
            "MCP_OAUTH_ALGORITHMS 가 비었다. 비워 두면 토큰이 스스로 고른 alg 를 "
            "믿게 되고, 그게 alg=none 과 HS256 혼동 공격이 들어오는 문이다."
        )


class JwtTokenVerifier(TokenVerifier):
    """Verify a JWT access token from the configured authorization server.

    Every rejection is the same ``None`` — a bad signature, a wrong audience, an
    expired token and an unknown account are indistinguishable to the caller, as
    in ``SessionTokenVerifier`` and ``deps.get_current_user``.

    **Subject mapping.** ``AccessToken.subject`` comes back as *our*
    ``users.id``, not the IdP's ``sub``. Everything downstream (the tool,
    ``QueryRunner``, the tenant-scoped SQL) already reads that field as a local
    user id, and having two meanings for one field is how a tenant boundary gets
    crossed by accident. The link is made through a verified ``email`` claim and
    an account that already exists — this never creates one. That is a
    deliberate limitation, not an oversight: proper account linking wants an
    explicit identity table and a linking flow the user consents to, which is a
    milestone of its own. An IdP that does not assert ``email_verified`` cannot
    be used here, and it should not be: without it, anyone who can set their own
    email address at the IdP can name someone else's account.
    """

    def __init__(
        self,
        *,
        issuer: str,
        aud: str,
        algorithms: list[str],
        jwk_client: Any,
    ) -> None:
        self._issuer = issuer
        self._audience = aud
        self._algorithms = list(algorithms)
        # 서명 키를 어디서 가져오는지가 이 클래스의 유일한 이음매다. 프로덕션은
        # jwt.PyJWKClient, 테스트는 키 하나를 들고 있는 가짜다 — 그래서 IdP
        # 없이도 **검증 로직만** 시험할 수 있다.
        self._jwk_client = jwk_client

    async def verify_token(self, token: str) -> AccessToken | None:
        if token.count(".") != 2:
            # JWS compact 형태가 아니면 우리 토큰일 수 없다. 세션 id(UUID)가
            # both 모드에서 여기 먼저 닿으므로, 네트워크도 DB 도 건드리지 않고
            # 빠져나가는 값싼 경로가 필요하다.
            return None

        try:
            # PyJWKClient 는 동기 HTTP(urllib)다. 첫 호출 이후에는 캐시라
            # 사실상 메모리 조회지만, 그 첫 호출이 이벤트 루프를 세우지 않도록
            # 스레드로 민다. anyio 가 아니라 표준 라이브러리를 쓰는 이유는
            # anyio 가 이 저장소의 선언된 의존성이 아니기 때문이다(starlette·
            # mcp 를 통해 들어와 있을 뿐이고, 남의 의존성에 기대면 그쪽이
            # 바뀔 때 우리가 import 에서 죽는다). 이 앱은 uvicorn 위에서만
            # 도므로 루프는 항상 asyncio 다.
            signing_key = await asyncio.to_thread(
                self._jwk_client.get_signing_key_from_jwt, token
            )
        except Exception:
            logger.info("MCP OAuth: 서명 키를 찾지 못했다")
            return None

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                # 토큰 헤더의 alg 를 믿지 않는다. 설정에 적힌 것만 받는다.
                algorithms=self._algorithms,
                # 여기가 W7 의 "토큰 audience 검증"이다. aud 가 다르면
                # InvalidAudienceError — 즉 남의 리소스 서버용으로 발급된
                # 토큰을 우리가 받아 쓰는 일이 없다.
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.InvalidTokenError as exc:
            # 사유는 로그에만. 클라이언트에게는 전부 같은 401 이어야 한다.
            logger.info("MCP OAuth: 토큰 거절 (%s)", type(exc).__name__)
            return None

        email = claims.get("email")
        if not email or claims.get("email_verified") is not True:
            logger.info("MCP OAuth: 검증된 email 클레임이 없어 거절")
            return None

        async with SessionLocal() as db:
            # 계정을 만들지 않는다. 이 서버에 이미 있는 계정에만 붙는다 —
            # 자동 생성은 IdP 에 이메일을 적을 수 있는 누구에게나 우리 테넌트를
            # 하나씩 내주는 것과 같다.
            user = await auth.get_user_by_email(db, str(email))

        if user is None:
            logger.info("MCP OAuth: 대응하는 로컬 계정이 없어 거절")
            return None

        return AccessToken(
            token=token,
            # OAuth 의 client_id 는 사람이 아니라 앱이다. 없는 IdP 도 있으므로
            # 없으면 sub 로 떨어뜨린다.
            client_id=str(claims.get("client_id") or claims["sub"]),
            scopes=_scopes(claims),
            expires_at=int(claims["exp"]),
            # 위에서 이미 검사했지만 그대로 싣는다. AuthSettings 에서
            # validate_token_resource=True 로 둔 모드에서는 SDK 미들웨어가 이
            # 값을 resource_server_url 과 한 번 더 대조한다 — 같은 사실을 두
            # 군데서 확인하는 것은 낭비가 아니라, 검증기를 갈아끼웠을 때 조용히
            # 약해지지 않게 하는 장치다.
            resource=self._audience,
            # 로컬 사용자 id. 위 docstring 의 "Subject mapping" 참조.
            subject=str(user.id),
            claims={
                "email": user.email,
                "email_verified": user.email_verified,
                # 원문 IdP 주체를 남긴다 — 감사용으로는 필요하지만 인가 판단에는
                # 쓰지 않는다. 인가는 subject(로컬 id) 하나로만 한다.
                "idp_subject": str(claims["sub"]),
                "idp_issuer": str(claims["iss"]),
            },
        )


def _scopes(claims: dict[str, Any]) -> list[str]:
    """Read scopes from either spelling IdPs use.

    RFC 9068 says ``scope`` is a space-delimited string; Entra ID and some
    others send ``scp`` as a list. Unknown scope names are kept as-is and simply
    match no tool — silently dropping them would make a misconfigured IdP look
    like a working one.
    """
    raw = claims.get("scope")
    if isinstance(raw, str):
        return raw.split()
    scp = claims.get("scp")
    if isinstance(scp, list):
        return [str(s) for s in scp]
    if isinstance(scp, str):
        return scp.split()
    return []


class CompositeTokenVerifier(TokenVerifier):
    """Try each verifier in order; the first one to recognise the token wins.

    ``both`` 모드의 전부다. 순서는 JWT 먼저인데, 이것은 우선순위가 아니라 비용
    문제다 — 세션 id 는 점이 없어 JwtTokenVerifier 가 DB 도 네트워크도 없이
    즉시 빠지고, JWT 는 UUID 가 아니라 SessionTokenVerifier 가 DB 없이 빠진다.
    두 검증기가 같은 토큰을 서로 다르게 해석할 수 있는 겹침이 없으므로, 순서가
    보안 판단을 바꾸지 않는다.
    """

    def __init__(self, *verifiers: TokenVerifier) -> None:
        self._verifiers = verifiers

    async def verify_token(self, token: str) -> AccessToken | None:
        for verifier in self._verifiers:
            access = await verifier.verify_token(token)
            if access is not None:
                return access
        return None


def build_jwk_client() -> Any:
    """The production signing-key source: the configured JWKS document."""
    return jwt.PyJWKClient(settings.mcp_oauth_jwks_url, cache_keys=True)


def build_oauth_verifier() -> JwtTokenVerifier:
    return JwtTokenVerifier(
        issuer=settings.mcp_oauth_issuer,
        aud=audience(),
        algorithms=settings.mcp_oauth_algorithms,
        jwk_client=build_jwk_client(),
    )
