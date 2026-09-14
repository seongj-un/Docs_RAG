"""MCP bearer tokens are this project's existing server-side sessions.

M7 deliberately introduces no second credential. A token here is the id of a
row in ``sessions`` — the same value the browser carries in its cookie, moved
to ``Authorization: Bearer <session_id>`` because MCP clients speak headers,
not cookies. Only the transport changes.

That is the whole design. A new token type would mean a second expiry to keep
in step, a second revocation path, and a second place to get tenant scoping
wrong. Reusing the session row means logging out kills the agent's access in
the same DELETE that kills the browser's (``services/auth.py``: 폐기가 곧
DELETE), and the M3 spec's reason for choosing sessions over JWT keeps
applying to a surface it was written before.
"""

import logging
import uuid

from mcp.server.auth.provider import AccessToken, TokenVerifier

from app.db import SessionLocal
from app.models import Session
from app.services import auth

logger = logging.getLogger(__name__)


class SessionTokenVerifier(TokenVerifier):
    """Resolve a bearer token to the user of the session row it names."""

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the caller's identity, or None (→ 401) for anything else.

        Every rejection returns the same ``None``: a malformed token, an
        unknown session, an expired one, and a session whose user has been
        deleted are indistinguishable to the caller. That matches
        ``deps.get_current_user``, which raises the same 401 for all four —
        an MCP client must not be able to learn that a session id once
        existed.

        **Why this opens its own database session.** The MCP SDK calls this
        from its bearer-auth ASGI middleware, before any route matches, so
        there is no FastAPI dependency graph to take ``Depends(get_session)``
        from. ``SessionLocal`` is the same factory ``db.get_session`` yields
        out of, so this borrows a connection from the same pool under the same
        settings; the ``async with`` is what guarantees it goes back, on the
        error paths as much as on the success one. The scope is two
        primary-key reads and no ``await`` on anything external, so the
        connection is returned long before the tool body runs — the tool then
        opens its own, and no connection is held across retrieval's several
        seconds.
        """
        try:
            session_id = uuid.UUID(token)
        except ValueError:
            # 토큰이 UUID 가 아니면 우리 세션 id 일 수 없다. DB 를 건드리지
            # 않고 끝내므로, 아무 문자열이나 던져 커넥션을 소모시키는 것도
            # 안 된다.
            return None

        async with SessionLocal() as db:
            # 만료 판정과 만료 행 삭제까지 기존 함수가 한다. 여기서 다시
            # 구현하면 쿠키 경로와 MCP 경로의 만료 규칙이 갈라진다 — 그
            # 순간 "같은 만료, 같은 폐기 경로"라는 이 파일의 전제가 거짓이
            # 된다.
            user = await auth.get_session_user(db, session_id)
            if user is None:
                return None

            # 위에서 만료가 아님을 확인한 직후이므로 이 행은 방금 읽혀
            # 식별 맵에 있다 — 추가 쿼리가 아니라 캐시 조회다.
            row = await db.get(Session, session_id)
            if row is None:  # pragma: no cover - 위 호출이 이미 보장한다
                return None

            return AccessToken(
                token=token,
                # OAuth 의 client_id 에 해당하는 것이 우리에겐 없다. 에이전트는
                # 사용자를 대신해 부르는 것이므로 주체와 같은 값을 넣는다 —
                # 없는 개념을 그럴듯한 가짜 값으로 채우지 않는다.
                client_id=str(user.id),
                scopes=[],
                # 세션 행의 만료를 그대로 싣는다. 미들웨어가 이 값을 보고
                # 과거면 거부하므로, 세션이 만료되는 순간 토큰도 만료된다 —
                # 두 개의 수명을 따로 관리하지 않는다는 것이 이 파일의 요점.
                expires_at=int(row.expires_at.timestamp()),
                subject=str(user.id),
                # 툴이 get_access_token() 으로 꺼내 읽는 값들. 인가 판단은
                # 여기 실린 subject 로만 하고, 요청 헤더로는 절대 하지 않는다
                # (SDK 주석: "Headers are client-supplied input — never treat
                # one as an identity assertion").
                claims={
                    "email": user.email,
                    "email_verified": user.email_verified,
                    "session_id": str(session_id),
                },
            )
