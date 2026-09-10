"""Shared FastAPI dependencies.

``get_current_user`` is the single place a request turns into a ``user_id``.
Every isolated read path takes that id as a required argument, so a route that
forgets to authenticate fails loudly instead of silently querying everyone's
data.
"""

import uuid

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import User
from app.services import auth

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
)

# 403 이고 429 가 아닌 이유: 기다린다고 풀리지 않는다. 사용자가 메일의
# 링크를 눌러야 한다. 429 로 두면 프론트가 "잠시 뒤에 다시"라는 틀린
# 조언을 하게 된다.
VERIFICATION_REQUIRED = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN, detail="email verification required"
)


async def get_current_user(
    session_id: str | None = Cookie(default=None, alias=settings.session_cookie_name),
    session: AsyncSession = Depends(get_session),
) -> User:
    if not session_id:
        raise _UNAUTHENTICATED
    try:
        parsed = uuid.UUID(session_id)
    except ValueError:
        raise _UNAUTHENTICATED from None

    user = await auth.get_session_user(session, parsed)
    if user is None:
        raise _UNAUTHENTICATED
    return user
