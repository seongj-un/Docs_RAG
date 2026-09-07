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
