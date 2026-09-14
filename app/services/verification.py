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

    **호출자 주의:** OK 가 아닌 경로에서는 세션을 rollback 한다. 이 함수의
    클레임만이 아니라 그 세션의 **트랜잭션 전체**가 되돌아간다. 라우터는
    이 함수를 부르기 전에 자기 작업을 커밋해 두어야 한다 — 커밋하지 않은
    session.add() 를 들고 들어와서 ALREADY/INVALID 를 받으면 그 작업이
    조용히 사라진다. 지금 호출자(auth 라우터)는 모두 자기 단위를 먼저
    커밋하므로 안전하다.
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
    """프론트엔드의 착지 페이지를 가리킨다. 백엔드가 아니다. 그리고 토큰은
    쿼리스트링이 아니라 **URL 프래그먼트**(``/verify#token=…``)에 싣는다.

    **프론트를 가리키는 이유(그대로 유지):** 메일 클라이언트와 보안 스캐너가
    본문 링크를 미리 GET 으로 밟는다. 링크가 곧 검증 엔드포인트면 사용자가
    클릭하기 전에 토큰이 소진되고, 화면에는 "이미 사용된 링크"가 뜬다.

    **프래그먼트인 이유:** ``?token=`` 은 사용자가 링크를 여는 순간 요청줄에
    실려 서버까지 간다. 그러면 DB 에 sha256 해시만 두고 "DB 가 통째로 새도
    링크는 만들 수 없다"고 해둔 설계(app/models.py 의
    EmailVerificationToken)가 액세스 로그 한 줄로 무너진다 — 그 줄을 읽을 수
    있으면 남의 계정을 인증할 수 있다. Caddyfile 의 redact_verify_token 이
    요청 URI · Referer · 오류 로거 세 갈래를 막고 있지만 그건 방어 계층이지
    근본 수정이 아니다. 토큰이 서버에 도달하는 한 (a) 브라우저 히스토리에
    그대로 남고, (b) Caddy 앞에 CDN/WAF 를 두면 우리가 손댈 수 없는 그쪽
    로그에 남고, (c) 누가 리댁션 설정을 건드리면 즉시 재발한다.

    프래그먼트는 브라우저가 **서버로 보내지 않는다**(RFC 3986 §3.5 — 오직
    클라이언트가 해석한다). 요청 URI 에도 Referer 에도 들어가지 않으므로
    프록시·CDN·오류 로그 어디에도 애초에 도달하지 않는다. 토큰은 착지
    페이지가 치는 ``POST /auth/verify`` 의 **본문**으로만 백엔드에 온다 —
    본문을 찍는 로거는 이 저장소에 없다(app/logging.py 는 요청 id 만 찍고,
    Caddy 는 URI·헤더만 찍는다).

    **감수한 위험 — 링크 재작성기가 프래그먼트를 떨어뜨리는 경우.** 기업
    메일 게이트웨이(Microsoft Defender Safe Links, Proofpoint URL Defense,
    Mimecast)는 본문 링크를 자기 도메인으로 갈아끼운다. 프래그먼트를 보존하지
    않는 구현을 만나면 인증이 **아예 안 된다**. 그래도 프래그먼트를 택했다:

    1. 실패가 조용하지 않고 되돌릴 수 있다. 프래그먼트가 잘리면 사용자는
       ``/verify`` 에 착지해 "링크가 올바르지 않습니다 / 주소가 잘렸을 수
       있습니다"와 **새 링크 받기** 버튼을 본다. 토큰은 소진되지 않았으니
       원본 메일을 다시 열거나 재발송으로 회복된다.
    2. 반대쪽 실패는 조용하고 되돌릴 수 없다. 로그로 샌 토큰은 화면 어디에도
       나타나지 않고, 로그가 이미 어딘가로 복제된 뒤에야 알게 된다.
    3. 수신자 구성. 이건 개인 포트폴리오 규모의 서비스이고 링크를 재작성하는
       것은 회사 메일 게이트웨이다. 여기 가입자가 회사 주소를 쓸 가능성은
       낮고, 쓰더라도 (1) 때문에 막다른 길이 아니다.
    4. 우리가 통제하는 구간에서는 재작성이 없다. 발송은 Resend 인데 클릭
       트래킹을 켜지 않았고(ResendMailer 가 보내는 페이로드에 관련 필드가
       없다), 켜지 않는 한 Resend 는 본문 링크를 그대로 내보낸다.

    트래킹을 켜거나 수신자가 기업 메일 쪽으로 기울면 이 판단은 다시 해야
    한다. 그때의 대안은 쿼리 복귀가 아니라, 짧은 조회 id 만 주소에 싣고
    토큰은 한 번 더 사용자 행동(버튼 클릭)으로 받는 쪽이다.
    """
    # quote() 는 지금은 아무 일도 하지 않는 보험이다 — token_urlsafe 가
    # 만드는 [A-Za-z0-9_-] 는 전부 인코딩 대상이 아니다. 남겨두는 이유는
    # 토큰 생성기를 바꾸는 날 여기가 조용히 깨지지 않게 하기 위해서다.
    # safe="" 를 준 것은 기본값 safe="/" 가 "/" 하나를 통과시키는데, 이
    # 값은 프래그먼트를 key=value 로 읽는 쪽(web/lib/verifyLink.ts 의
    # URLSearchParams)에서 한 파라미터의 **값**이라 경로 구분자로 남겨둘
    # 이유가 없어서다.
    return f"{settings.app_base_url.rstrip('/')}/verify#token={quote(raw, safe='')}"


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
