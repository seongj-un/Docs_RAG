"""Authentication endpoints: signup, login, logout, me."""

import uuid

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from fastapi import status as http_status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.deps import get_current_user
from app.models import User
from app.schemas import LoginRequest, SignupRequest, UserOut
from app.services import auth

router = APIRouter(prefix="/auth", tags=["auth"])

_INVALID_CREDENTIALS = HTTPException(
    status_code=http_status.HTTP_401_UNAUTHORIZED, detail="invalid email or password"
)


def _set_session_cookie(response: Response, session_id: uuid.UUID) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=str(session_id),
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        path="/",
    )


@router.post("/signup", status_code=http_status.HTTP_201_CREATED, response_model=UserOut)
async def signup(
    body: SignupRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> User:
    try:
        user = await auth.create_user(session, body.email, body.password)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="email already registered",
        ) from None

    row = await auth.create_session(session, user.id)
    _set_session_cookie(response, row.id)
    return user


@router.post("/login", response_model=UserOut)
async def login(
    body: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> User:
    user = await auth.authenticate(session, body.email, body.password)
    if user is None:
        raise _INVALID_CREDENTIALS
    row = await auth.create_session(session, user.id)
    _set_session_cookie(response, row.id)
    return user


@router.post("/logout", status_code=http_status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    session_id: str | None = Cookie(default=None, alias=settings.session_cookie_name),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Revoke the current session. Idempotent: always clears the cookie."""
    if session_id:
        try:
            await auth.delete_session(session, uuid.UUID(session_id))
        except ValueError:
            pass  # malformed cookie: nothing to revoke
    response.delete_cookie(settings.session_cookie_name, path="/")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
