"""Authentication endpoints: signup, login, logout, me."""

import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Request, Response
from fastapi import status as http_status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.deps import get_current_user
from app.models import User
from app.schemas import LoginRequest, SignupRequest, UserOut, VerifyRequest
from app.services import auth, mailer, verification
from app.services.ratelimit import auth_limiter, verify_resend_limiter

logger = logging.getLogger(__name__)

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


async def _deliver_verification(email: str, raw_token: str) -> None:
    """응답 밖에서 실행된다. 여기서 나는 예외는 요청에 영향을 주지 않는다.

    대가로 사용자는 발송 실패를 알 수 없다 — /auth/resend-verification 이
    그 유일한 복구 경로이므로 화면에서 항상 닿을 수 있어야 한다.
    """
    subject, html, text = verification.build_email(
        verification.build_link(raw_token)
    )
    try:
        await mailer.get_mailer().send(
            to=email, subject=subject, html=html, text=text
        )
    except Exception:  # noqa: BLE001 - 배달 실패가 가입을 되돌리면 안 된다
        logger.exception("인증 메일 발송 실패: %s", email)


@router.post("/signup", status_code=http_status.HTTP_201_CREATED, response_model=UserOut)
async def signup(
    body: SignupRequest,
    request: Request,
    response: Response,
    background: BackgroundTasks,
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

    # 계정 행이 생긴 뒤로는 가입이 성공해야 한다. 세션만 예외다 — 세션이
    # 없으면 응답에 실을 쿠키가 없고, 쿠키 없는 201 은 거짓말이 된다. 그래서
    # "반드시 필요한" 이 호출을 가장 먼저 끝내, 뒤에 오는 어떤 실패도 이미
    # 만든 응답을 되돌릴 수 없게 한다.
    row = await auth.create_session(session, user.id)
    _set_session_cookie(response, row.id)

    # 토큰 발급·메일 예약은 "있으면 좋은" 단계다(DB 는 토큰 발급에만
    # 필요하고, 발송 자체는 밖에서 한다) — 여기서 나는 예외가 이미 만든
    # 계정·세션을 500 으로 되돌리면 안 된다. 실패해도 이 세션으로 더 할
    # 일이 없으므로 rollback 은 부르지 않는다: 부르면 위에서 커밋된 user
    # 객체가 만료되고, expire_on_commit=False 로도 못 막는 재조회가 응답
    # 직렬화 시점에 AsyncSession 밖에서 일어나 MissingGreenlet 으로 죽는다.
    # 복구 경로는 /auth/resend-verification(로그인 뒤 재발송 버튼)이다.
    try:
        raw = await verification.issue_token(session, user.id)
    except Exception:  # noqa: BLE001 - 토큰 발급 실패가 가입 성공을 되돌리면 안 된다
        logger.exception(
            "가입 직후 인증 토큰 발급 실패: user_id=%s email=%s", user.id, user.email
        )
    else:
        background.add_task(_deliver_verification, user.email, raw)

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


@router.post("/verify", response_model=UserOut)
async def verify(
    body: VerifyRequest,
    session: AsyncSession = Depends(get_session),
) -> User:
    """세션을 요구하지 않는다 — 메일 링크는 다른 브라우저에서 열린다."""
    result, user = await verification.consume_token(session, body.token)

    if result is verification.VerifyResult.ALREADY:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="email already verified",
        )
    if result is not verification.VerifyResult.OK or user is None:
        # 만료와 "그런 토큰 없음"을 나누지 않는다. 나누면 임의의 문자열을
        # 던져 토큰의 존재 여부를 물을 수 있게 된다.
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired token",
        )
    return user


@router.post("/resend-verification", status_code=http_status.HTTP_204_NO_CONTENT)
async def resend_verification(
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """이메일 주소가 아니라 세션을 받는다.

    주소를 받으면 "이 주소가 가입돼 있나"를 묻는 열거 창구가 하나 더 생긴다.
    """
    if user.email_verified:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="email already verified",
        )
    if not verify_resend_limiter.allow(f"user:{user.id}"):
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="verification email rate limit exceeded",
            headers={"Retry-After": "60"},
        )

    raw = await verification.issue_token(session, user.id)
    background.add_task(_deliver_verification, user.email, raw)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
