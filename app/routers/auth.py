"""Authentication endpoints: signup, login, logout, me."""

import uuid

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi import status as http_status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.deps import get_current_user
from app.models import User
from app.schemas import LoginRequest, SignupRequest, UserOut
from app.services import auth
from app.services.ratelimit import auth_limiter

router = APIRouter(prefix="/auth", tags=["auth"])

_INVALID_CREDENTIALS = HTTPException(
    status_code=http_status.HTTP_401_UNAUTHORIZED, detail="invalid email or password"
)


def _guard_attempts(request: Request, email: str | None = None) -> None:
    """Throttle credential attempts before any hashing happens.

    Uploads and queries were limited but authentication was not, so an 8-character
    password could be guessed without any ceiling. Two buckets, because they stop
    different attacks: the address bucket stops one machine grinding through many
    accounts, the email bucket stops many machines grinding through one.

    Checked before argon2 runs — verifying is deliberately expensive, and paying
    that cost for an attacker is itself the denial of service.
    """
    client_ip = request.client.host if request.client else "unknown"
    keys = [f"ip:{client_ip}"]
    if email:
        keys.append(f"email:{auth.normalize_email(email)}")
    # Every bucket is consumed, not short-circuited: an attempt should cost a
    # token on each axis it belongs to.
    if not all([auth_limiter.allow(key) for key in keys]):
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="auth rate limit exceeded",
            headers={"Retry-After": "60"},
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
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> User:
    _guard_attempts(request, body.email)
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
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> User:
    _guard_attempts(request, body.email)
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
