"""OAuth resource-server behaviour (M7 W7). Requires Postgres.

⚠️ **이 파일은 IdP 를 흉내 내지 않는다.** 아래 ``_TestIssuer`` 는 검증 로직에
입력을 만들어 주기 위한 테스트 픽스처이고, 인가 서버의 대역이 아니다. 구별이
중요한 이유: W7 은 인가 서버를 직접 만드는 것을 명시적으로 범위 밖에 뒀고,
``app/`` 안에는 토큰을 발급할 수 있는 코드가 한 줄도 없다. 여기 있는 서명 키는
테스트 프로세스 안에서 태어나 프로세스와 함께 사라진다.

그래서 이 파일이 증명하는 것과 못 하는 것을 갈라 적는다.

증명한다 — 서명·발급자·대상(aud)·만료·스코프를 우리가 실제로 검사한다는 것,
거절이 전부 구별 불가능한 401 이라는 것, 세션 베어러와의 공존 규칙, RFC 9728
문서가 켜질 때만 켜지고 401 이 그 위치를 안내한다는 것.

증명 못 한다 — 진짜 IdP 와의 상호운용. JWKS 회전, 알고리즘 협상, ID 토큰과
액세스 토큰의 구분 같은 것은 IdP 를 붙이는 날 처음 겪는다.
"""

import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import settings
from app.db import SessionLocal
from app.mcp import oauth
from app.mcp.oauth import OAuthConfigError
from app.mcp.scopes import SCOPE_SEARCH, SCOPE_WRITE
from tests.mcp_fixture import (
    call,
    db_available,
    drop_users,
    make_session,
    make_user,
    mcp_client,
    run_async,
    stub_models,  # noqa: F401 - 픽스처를 이 모듈 이름공간으로 끌어온다
)

pytestmark = pytest.mark.skipif(not db_available(), reason="Postgres not reachable")

ISSUER = "https://idp.example.test/"
RESOURCE = "https://docs.example.test/mcp"
OTHER_RESOURCE = "https://someone-else.example.test/mcp"


class _TestIssuer:
    """Signs tokens so the verifier has something to verify. **Not an IdP.**

    프로덕션에서 이 자리에 오는 것은 ``jwt.PyJWKClient`` 이고, 그것은 설정된
    JWKS 문서에서 키를 가져온다. 여기서는 그 한 이음매만 갈아끼워, 네트워크 없이
    ``JwtTokenVerifier`` 의 검증 경로 전체(서명→발급자→대상→만료→클레임→로컬
    계정 조회)를 실제로 통과시킨다.
    """

    def __init__(self) -> None:
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._pem = self._private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self.public = self._private.public_key()

    def sign(self, claims: dict) -> str:
        return jwt.encode(claims, self._pem, algorithm="RS256")

    def jwk_client(self):
        public = self.public

        class _Client:
            @staticmethod
            def get_signing_key_from_jwt(token):  # noqa: ARG004 - 키가 하나뿐이다
                return type("_Key", (), {"key": public})

        return _Client()


_ISSUER = _TestIssuer()
# 다른 사람의 키. "서명이 맞는지"를 실제로 보는지 확인하는 데만 쓴다.
_IMPOSTOR = _TestIssuer()


def _claims(email: str, **overrides) -> dict:
    import time

    base = {
        "iss": ISSUER,
        "sub": f"idp|{uuid.uuid4().hex[:8]}",
        "aud": RESOURCE,
        "exp": int(time.time()) + 600,
        "iat": int(time.time()),
        "email": email,
        "email_verified": True,
        "scope": SCOPE_SEARCH,
        "client_id": "some-agent",
    }
    base.update(overrides)
    return base


@pytest.fixture
def oauth_mode(monkeypatch):
    """Put the server in pure OAuth mode with the test issuer behind it."""
    monkeypatch.setattr(settings, "mcp_auth_mode", "oauth")
    monkeypatch.setattr(settings, "mcp_oauth_issuer", ISSUER)
    monkeypatch.setattr(settings, "mcp_oauth_jwks_url", "https://idp.example.test/jwks")
    monkeypatch.setattr(settings, "mcp_resource_server_url", RESOURCE)
    monkeypatch.setattr(settings, "mcp_oauth_audience", "")
    monkeypatch.setattr(oauth, "build_jwk_client", _ISSUER.jwk_client)


@pytest.fixture
def both_mode(oauth_mode, monkeypatch):
    monkeypatch.setattr(settings, "mcp_auth_mode", "both")


# --- 설정이 모자라면 기동하지 않는다 --------------------------------------


def test_oauth_mode_without_configuration_refuses_to_start(monkeypatch):
    """위험한 상태는 "검증하고 있다고 믿는데 사실 아무거나 받는" 쪽이다.

    그 상태가 생기려면 oauth 모드에서 설정이 비어 있어야 하는데, 그 조합은
    기동 자체가 안 된다. 기본값(session)이 안전한 쪽인 근거의 나머지 절반.
    """
    monkeypatch.setattr(settings, "mcp_auth_mode", "oauth")
    monkeypatch.setattr(settings, "mcp_oauth_issuer", "")
    monkeypatch.setattr(settings, "mcp_oauth_jwks_url", "")
    monkeypatch.setattr(settings, "mcp_resource_server_url", "")

    with pytest.raises(OAuthConfigError) as exc:
        oauth.validate_settings()
    assert "MCP_OAUTH_ISSUER" in str(exc.value)


def test_unknown_auth_mode_refuses_to_start(monkeypatch):
    monkeypatch.setattr(settings, "mcp_auth_mode", "oath")  # 오타
    with pytest.raises(OAuthConfigError):
        oauth.validate_settings()


def test_default_mode_is_session_and_publishes_no_metadata():
    """기본값은 오늘 동작하는 것이다. 그리고 없는 인가 서버를 광고하지 않는다."""
    assert settings.mcp_auth_mode == "session"

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "mcp-default")
            session_id = await make_session(db, user)

        async with mcp_client() as client:
            listed = await call(client, "tools/list", str(session_id))
            metadata = await client.get(
                "/.well-known/oauth-protected-resource/mcp"
            )
            anon = await call(client, "tools/list", None)

        await drop_users(user)
        return listed, metadata, anon

    listed, metadata, anon = run_async(scenario)
    # 세션 베어러는 여전히 유일하게 동작하는 인증이다.
    assert listed.status_code == 200
    assert metadata.status_code == 404
    # 가리킬 곳이 없으므로 401 도 메타데이터 위치를 말하지 않는다. 거짓 주소를
    # 주는 것보다 아무 말도 안 하는 편이 낫다 — 클라이언트는 실패할 OAuth
    # 디스커버리 대신 쓸 수 있는 베어러로 간다.
    assert anon.status_code == 401
    assert "resource_metadata" not in anon.headers.get("www-authenticate", "")


# --- RFC 9728 -------------------------------------------------------------


def test_protected_resource_metadata_is_published_in_oauth_mode(oauth_mode):
    async def scenario():
        async with mcp_client() as client:
            document = await client.get("/.well-known/oauth-protected-resource/mcp")
            anon = await call(client, "tools/list", None)
        return document, anon

    document, anon = run_async(scenario)
    assert document.status_code == 200
    payload = document.json()
    assert payload["resource"] == RESOURCE
    # 클라이언트가 토큰을 받으러 갈 곳. 이것이 "우리는 발급하지 않는다"의
    # 기계가 읽을 수 있는 형태다.
    assert payload["authorization_servers"] == [ISSUER]

    # 401 이 그 문서의 위치를 알려준다 — W7 의 두 번째 항목.
    challenge = anon.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert (
        'resource_metadata="https://docs.example.test/'
        '.well-known/oauth-protected-resource/mcp"' in challenge
    )


def test_metadata_route_is_reachable_through_our_mount(oauth_mode):
    """SDK 는 이 라우트를 **서브앱 안**에 등록한다. 우리 마운트는 POST /mcp
    하나만 들여보내므로, 호스트 앱이 같은 경로를 한 번 더 걸지 않으면 등록은
    됐지만 닿을 수 없는 라우트가 된다 — 앱은 멀쩡히 뜨고 401 은 정확한 주소를
    안내하는데 그 주소가 404 를 내는, 증상 없는 고장이다.

    위 테스트가 200 을 보는 것으로 이미 증명되지만, 그게 **왜** 필요한지가
    마운트 방식(app/main.py)에 달려 있어서 별도의 증인을 세운다.
    """

    async def scenario():
        async with mcp_client() as client:
            # 인증 없이 읽을 수 있어야 한다. 토큰을 받는 방법을 알려주는
            # 문서를 토큰으로 잠그면 아무도 시작할 수 없다.
            return await client.get("/.well-known/oauth-protected-resource/mcp")

    assert run_async(scenario).status_code == 200


# --- 토큰 검증 ------------------------------------------------------------


def _with_token(claims_fn, *, mode_user_verified=True):
    """Create a user, mint a token from their email, and call ``tools/list``."""

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "mcp-oauth", verified=mode_user_verified)

        token = claims_fn(user)
        async with mcp_client() as client:
            response = await call(client, "tools/list", token)

        await drop_users(user)
        return response

    return run_async(scenario)


def test_valid_token_from_the_configured_idp_is_accepted(oauth_mode):
    response = _with_token(lambda u: _ISSUER.sign(_claims(u.email)))
    assert response.status_code == 200
    names = [t["name"] for t in response.json()["result"]["tools"]]
    assert names == ["search_documents"]


def test_token_for_another_audience_is_rejected(oauth_mode):
    """W7 항목: 우리 서버용으로 발급된 토큰인지 확인한다.

    이것이 없으면, 같은 IdP 를 쓰는 **다른** 서비스용 토큰을 들고 와서 우리
    문서를 읽을 수 있다. 그 서비스는 우리와 아무 신뢰 관계가 없어도 된다.
    """
    response = _with_token(
        lambda u: _ISSUER.sign(_claims(u.email, aud=OTHER_RESOURCE))
    )
    assert response.status_code == 401


def test_token_from_another_issuer_is_rejected(oauth_mode):
    response = _with_token(
        lambda u: _ISSUER.sign(_claims(u.email, iss="https://evil.example.test/"))
    )
    assert response.status_code == 401


def test_token_signed_by_another_key_is_rejected(oauth_mode):
    """클레임이 전부 맞아도 서명이 남의 것이면 거절된다."""
    response = _with_token(lambda u: _IMPOSTOR.sign(_claims(u.email)))
    assert response.status_code == 401


def test_expired_token_is_rejected(oauth_mode):
    import time

    response = _with_token(
        lambda u: _ISSUER.sign(_claims(u.email, exp=int(time.time()) - 1))
    )
    assert response.status_code == 401


def test_unverified_email_claim_is_rejected(oauth_mode):
    """IdP 에서 이메일을 스스로 적을 수 있으면 남의 계정을 지목할 수 있다."""
    response = _with_token(
        lambda u: _ISSUER.sign(_claims(u.email, email_verified=False))
    )
    assert response.status_code == 401


def test_token_for_an_account_we_do_not_have_is_rejected(oauth_mode):
    """계정을 만들지 않는다. 자동 생성은 IdP 에 메일을 적을 수 있는 누구에게나
    테넌트를 하나씩 내주는 것과 같다."""
    response = _with_token(lambda u: _ISSUER.sign(_claims("nobody@example.com")))
    assert response.status_code == 401


def test_token_maps_to_the_local_user_not_the_idp_subject(oauth_mode, stub_models):
    """주체는 우리 users.id 로 돌아와야 한다.

    이 필드가 어떤 요청에서는 IdP 의 sub 이고 어떤 요청에서는 우리 id 라면,
    그 순간 테넌트 경계가 "가끔 맞는 조인"이 된다. 검색이 실제로 그 사용자의
    문서를 찾아오는지로 증명한다.
    """
    from tests.mcp_fixture import make_chunk, search

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "mcp-oauth-sub")
            chunk = await make_chunk(db, user, "OAuth 로 들어온 사용자의 문서")

        token = _ISSUER.sign(_claims(user.email))
        async with mcp_client() as client:
            response = await search(client, token, question="문서")

        await drop_users(user)
        return response, chunk

    response, chunk = run_async(scenario)
    hits = response.json()["result"]["structuredContent"]["hits"]
    assert [h["chunk_id"] for h in hits] == [str(chunk.id)]


# --- 두 검증기의 공존 -----------------------------------------------------


def test_session_bearer_is_rejected_in_pure_oauth_mode(oauth_mode):
    """oauth 는 "IdP 토큰만"이라는 뜻이어야 한다. 아니면 모드가 거짓말이다."""

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "mcp-oauth-sess")
            session_id = await make_session(db, user)

        async with mcp_client() as client:
            response = await call(client, "tools/list", str(session_id))

        await drop_users(user)
        return response

    assert run_async(scenario).status_code == 401


def test_both_mode_accepts_either_credential(both_mode):
    """이행 구간. 같은 계정이 세션으로도 JWT 로도 들어온다."""

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "mcp-both")
            session_id = await make_session(db, user)

        token = _ISSUER.sign(_claims(user.email))
        async with mcp_client() as client:
            by_session = await call(client, "tools/list", str(session_id))
            by_jwt = await call(client, "tools/list", token)
            garbage = await call(client, "tools/list", "neither-of-those")

        await drop_users(user)
        return by_session, by_jwt, garbage

    by_session, by_jwt, garbage = run_async(scenario)
    assert by_session.status_code == 200
    assert by_jwt.status_code == 200
    # 둘 다 아닌 것은 여전히 401 — "둘 중 하나"가 "아무거나"가 되면 안 된다.
    assert garbage.status_code == 401


def test_scopes_come_from_the_token_not_from_a_header(oauth_mode):
    """스코프를 헤더로 올려 받을 수 있으면 스코프는 존재하지 않는 것과 같다.

    SDK 주석 그대로: "Headers are client-supplied input — never treat one as an
    identity assertion". 읽기 스코프만 든 토큰에 쓰기 스코프를 헤더로 덧붙여
    보낸다.
    """
    from tests.mcp_fixture import body, headers

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "mcp-scope-hdr")

        token = _ISSUER.sign(_claims(user.email, scope=SCOPE_SEARCH))
        async with mcp_client() as client:
            response = await client.post(
                settings.mcp_path,
                json=body("tools/list"),
                headers={
                    **headers("tools/list", token),
                    "X-Scopes": f"{SCOPE_SEARCH} {SCOPE_WRITE}",
                    "Authorization-Scope": SCOPE_WRITE,
                },
            )

        await drop_users(user)
        return response

    result = run_async(scenario).json()["result"]
    # 쓰기 툴은 애초에 없으므로 목록이 늘어날 수는 없지만, 요점은 헤더가 스코프
    # 판단에 **닿지 않는다**는 것이다. 스코프별 목록 차이는
    # tests/test_mcp_scopes.py 가 가짜 쓰기 툴로 증명한다.
    assert [t["name"] for t in result["tools"]] == ["search_documents"]
