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

from sqlalchemy import delete, select, update
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

    행을 SELECT 로 읽어 Python 에서 판단한 뒤 다시 써서 커밋하는
    check-then-act 는 두 가지를 깬다.

    (1) 이중 소비. 같은 토큰으로 동시에 두 요청이 오면(중복 제출, 두 탭)
    둘 다 SELECT 에서 "아직 안 썼다"를 보고 둘 다 커밋해버린다 — 1회용이
    이 모듈이 존재하는 이유인데, 그게 두 세션이 얽히지 않을 때만 성립하게
    된다.

    (2) 재발급과의 경쟁. 이 함수가 행을 읽어 파이썬 객체로 들고 있는 동안
    issue_token 이 같은(미소비) 행을 지우고 커밋하면, 나중에 이 함수가
    `row.consumed_at = now` 를 커밋할 때 나가는 `UPDATE ... WHERE id=:id`
    가 0행에 매치되어 SQLAlchemy 가 StaleDataError 를 던진다 — "절대 예외를
    던지지 않는다"는 이 함수의 계약을 깬다.

    그래서 읽기와 쓰기를 한 번의 조건부 UPDATE ... RETURNING 으로 합쳐
    "클레임"으로 만든다. WHERE 에 consumed_at IS NULL / expires_at > now 를
    걸어두면, 매치되는 행에 Postgres 가 거는 잠금 덕분에 동시 호출 중
    정확히 하나만 행을 실제로 바꿀 수 있다 — 나머지는 그 커밋이 끝난 뒤
    재평가된 WHERE 에서 더는 조건이 참이 아니라 0행 매치로 끝난다. 같은
    이유로 재발급이 먼저 행을 지우고 커밋해버린 경우에도 이 UPDATE 는
    그저 0행에 매치될 뿐이라 아래의 "못 가져갔다" 분기로 흡수되고,
    StaleDataError 는 애초에 던져질 자리가 없다.

    만료와 "그런 토큰 없음"을 같은 INVALID 로 묶는 것은 의도된 것이다.
    나누면 임의의 문자열을 던져 토큰의 존재 여부를 물을 수 있게 된다.
    """
    now = datetime.now(timezone.utc)

    claimed = await session.execute(
        update(EmailVerificationToken)
        .where(
            EmailVerificationToken.token_hash == hash_token(raw),
            EmailVerificationToken.consumed_at.is_(None),
            EmailVerificationToken.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(EmailVerificationToken.user_id)
        .execution_options(synchronize_session=False)
    )
    user_id = claimed.scalar_one_or_none()

    if user_id is None:
        # 못 가져갔다: 이미 소비됐거나, 만료됐거나, 애초에 없거나, 방금
        # 재발급이 지웠다. 어느 쪽이든 위 UPDATE 는 이미 0행이라 커밋할
        # 것이 없다 — 존재 여부만 다시 물어 ALREADY 와 INVALID 를 가른다.
        existing = await session.execute(
            select(EmailVerificationToken.consumed_at).where(
                EmailVerificationToken.token_hash == hash_token(raw)
            )
        )
        consumed_at = existing.scalars().first()
        await session.rollback()
        if consumed_at is not None:
            return VerifyResult.ALREADY, None
        return VerifyResult.INVALID, None

    user = await session.get(User, user_id)
    if user is None:  # 계정이 그사이 지워졌다 — 방금 튄 클레임도 되돌린다
        await session.rollback()
        return VerifyResult.INVALID, None

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
