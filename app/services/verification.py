"""이메일 인증 토큰: 발급 · 소비 · 무효화.

토큰은 세션과 같은 방식으로 다룬다 — 서버에 행이 있고, 지우면 즉시 죽는다.
서명 토큰(JWT/itsdangerous)을 쓰지 않은 이유도 세션에서와 같다: 발급한
링크를 취소할 수 없기 때문이다.

저장하는 것은 sha256 해시다. argon2 가 아닌 이유는 토큰이 사용자가 고른
비밀번호가 아니라 256비트 난수라서 사전 공격 대상이 아니고, 솔트 때문에
인덱스 조회가 불가능해지면 검증마다 미소비 토큰 전체를 훑어야 하기 때문이다.

이 모듈은 DB 만 안다. HTTP 상태 코드로의 번역은 라우터가 한다.
"""

import enum
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import EmailVerificationToken, User

# 256비트. 무차별 대입이 의미를 갖지 못하는 크기라서 해시를 느리게 만들
# 필요가 없다.
_TOKEN_BYTES = 32


class VerifyResult(enum.Enum):
    OK = "ok"
    ALREADY = "already"
    INVALID = "invalid"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def issue_token(session: AsyncSession, user_id: uuid.UUID) -> str:
    """새 토큰을 발급하고 **원문**을 돌려준다. 원문은 여기서만 존재한다.

    아직 쓰지 않은 이전 토큰은 지운다. 살아 있는 링크가 둘이면 어느 쪽이
    유효한지 사용자가 알 수 없다. 이미 소비된 행은 남긴다 — 그게 두 번째
    클릭에 "이미 인증하셨다"고 말할 수 있는 근거다.
    """
    await session.execute(
        delete(EmailVerificationToken).where(
            EmailVerificationToken.user_id == user_id,
            EmailVerificationToken.consumed_at.is_(None),
        )
    )

    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    session.add(
        EmailVerificationToken(
            user_id=user_id,
            token_hash=hash_token(raw),
            expires_at=datetime.now(timezone.utc)
            + timedelta(hours=settings.verify_token_ttl_hours),
        )
    )
    await session.commit()
    return raw


async def consume_token(
    session: AsyncSession, raw: str
) -> tuple[VerifyResult, User | None]:
    """토큰을 한 번 쓰고 사용자를 인증 처리한다.

    만료와 "그런 토큰 없음"을 같은 INVALID 로 묶는 것은 의도된 것이다.
    나누면 임의의 문자열을 던져 토큰의 존재 여부를 물을 수 있게 된다.
    """
    result = await session.execute(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == hash_token(raw)
        )
    )
    row = result.scalars().first()
    if row is None:
        return VerifyResult.INVALID, None

    if row.consumed_at is not None:
        return VerifyResult.ALREADY, None

    now = datetime.now(timezone.utc)
    if row.expires_at <= now:
        return VerifyResult.INVALID, None

    user = await session.get(User, row.user_id)
    if user is None:  # 계정이 그사이 지워졌다
        return VerifyResult.INVALID, None

    row.consumed_at = now
    user.email_verified_at = now
    await session.commit()
    await session.refresh(user)
    return VerifyResult.OK, user


def build_link(raw: str) -> str:
    """프론트엔드의 착지 페이지를 가리킨다. 백엔드가 아니다.

    메일 클라이언트와 보안 스캐너가 본문 링크를 미리 GET 으로 밟는다. 링크가
    곧 검증 엔드포인트면 사용자가 클릭하기 전에 토큰이 소진되고, 화면에는
    "이미 사용된 링크"가 뜬다.
    """
    return f"{settings.app_base_url.rstrip('/')}/verify?token={quote(raw)}"


def build_email(link: str) -> tuple[str, str, str]:
    """(제목, HTML, 평문). 평문은 HTML 을 막아둔 클라이언트를 위한 것이다."""
    subject = "이메일 주소를 확인해 주세요"
    hours = settings.verify_token_ttl_hours
    plain = (
        "아래 주소를 열면 이메일 확인이 끝납니다.\n\n"
        f"{link}\n\n"
        f"이 링크는 {hours}시간 뒤에 닫힙니다.\n"
        "이 가입을 요청한 적이 없다면 아무것도 하지 않으셔도 됩니다."
    )
    html = (
        '<div style="font-family:system-ui,sans-serif;line-height:1.6">'
        "<p>아래 버튼을 누르면 이메일 확인이 끝납니다.</p>"
        f'<p><a href="{link}">이메일 확인하기</a></p>'
        f"<p>이 링크는 {hours}시간 뒤에 닫힙니다.</p>"
        "<p>이 가입을 요청한 적이 없다면 아무것도 하지 않으셔도 됩니다.</p>"
        "</div>"
    )
    return subject, html, plain
