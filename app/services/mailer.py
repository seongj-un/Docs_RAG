"""메일 전송. 전송만 한다 — 무슨 내용을 보낼지는 호출자가 정한다.

어댑터가 둘인 이유는 개발과 운영이 서로를 막지 않게 하기 위해서다.
RESEND_API_KEY 가 없으면 콘솔로 내려오므로, 키 없이 clone 한 사람도 앱을
띄우고 전체 테스트를 돌릴 수 있다.

SMTP 가 아니라 HTTP API 를 쓰는 이유: 많은 호스팅이 25/465/587 포트를
막아두는데, 그 경우 SMTP 는 타임아웃으로만 실패해서 원인을 찾기 어렵다.
"""

import logging
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"
# 발송은 요청 밖(BackgroundTasks)에서 일어나지만, 무한정 매달려 있으면
# 워커를 잡는다.
_TIMEOUT = 10.0


class Mailer(Protocol):
    async def send(self, *, to: str, subject: str, html: str, text: str) -> None: ...


class ConsoleMailer:
    """링크를 로그로 찍는다. 개발과 테스트에서 이게 메일함이다.

    INFO 가 아니라 WARNING 인 이유: uvicorn 기본 로깅 설정에서 앱 로거의
    유효 레벨은 WARNING 이고 루트에 핸들러가 없다. INFO 로 찍으면 이 줄은
    **어디에도 나타나지 않고**, 그러면 로컬에서 계정을 인증할 방법이 아예
    사라진다 — 이 어댑터가 존재하는 이유가 통째로 없어진다.

    레벨이 과하지도 않다. 이 어댑터가 도는 것 자체가 "메일이 실제로는
    배달되지 않고 있다"는 뜻이라, 운영에서 보이면 그건 알아야 할 상태다.

    다만 본문은 APP_BASE_URL 이 루프백일 때만 찍는다. 본문에는 이메일 인증
    링크가 들어 있고 그 링크에는 토큰 **원문**이 실려 있다. DB 에는 sha256
    해시만 두고 "DB 가 통째로 새도 링크는 만들 수 없다"고 해둔 설계
    (app/models.py EmailVerificationToken)가, 그 링크를 로그에 적는 순간
    무효가 된다 — 로그를 읽을 수 있는 사람은 남의 계정을 인증할 수 있다.
    게다가 MAIL_PROVIDER 기본값이 console 이라 이건 예외 상황이 아니라
    설정을 안 바꾼 배포의 **기본 동작**이었다.

    공개 배포에서는 본문을 마스킹하지 않고 통째로 뺀다. 토큰만 가리는 쪽을
    먼저 봤는데, 이 어댑터는 본문이 무엇인지 모른다 — 모듈 첫 줄이 적어둔
    대로 "무슨 내용을 보낼지는 호출자가 정한다". 지금은 토큰이 쿼리
    파라미터로 오지만 호출자가 경로든 본문이든 어디로 옮겨도 이 파일은
    바뀌지 않고, 그때 마스킹은 실패한 줄도 모르고 빗나간다. 반대로 공개
    배포에서 본문이 주는 값은 거의 없다: 이 어댑터가 돌고 있다는 건 메일이
    아무에게도 배달되지 않았다는 뜻이라, 그때 필요한 건 본문이 아니라 경보다.

    판정이 틀리는 두 방향의 값이 다르다. 로컬인데 공개로 보면 개발자가 링크를
    못 본다 — 대신 아래 로그가 이유와 고치는 법을 같이 적어주고, 되돌릴 수
    있다. 공개인데 로컬로 보면 토큰이 로그에 남고, 그건 되돌릴 수 없다.
    그래서 확실히 로컬일 때만 찍는 쪽으로 닫는다.
    """

    async def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        if _looks_local(settings.app_base_url):
            logger.warning("[mail] to=%s subject=%s\n%s", to, subject, text)
            return
        logger.warning(
            "[mail] to=%s subject=%s — 본문은 찍지 않는다. APP_BASE_URL=%s 가 "
            "공개 주소인데 콘솔 메일러가 돌고 있다: 이 메일은 아무에게도 "
            "배달되지 않았고, 본문에 든 인증 링크(=토큰 원문)를 로그에 남기면 "
            "로그를 읽을 수 있는 사람이 이 계정을 인증할 수 있다. "
            "MAIL_PROVIDER=resend 와 RESEND_API_KEY 를 설정할 것.",
            to,
            subject,
            settings.app_base_url,
        )


class ResendMailer:
    def __init__(self, api_key: str, sender: str) -> None:
        self._api_key = api_key
        self._sender = sender

    async def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                _RESEND_ENDPOINT,
                json={
                    "from": self._sender,
                    "to": [to],
                    "subject": subject,
                    "html": html,
                    "text": text,
                },
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()


def get_mailer() -> Mailer:
    """설정이 resend 라도 키가 없으면 콘솔로 내려온다.

    조용히 성공한 척하는 것보다 로그에 남기는 편이 낫고, 키가 없다는 이유로
    앱이 뜨지 않는 것보다도 낫다.
    """
    if settings.mail_provider == "resend" and settings.resend_api_key:
        return ResendMailer(settings.resend_api_key, settings.mail_from)
    if settings.mail_provider == "resend":
        logger.warning("MAIL_PROVIDER=resend 인데 RESEND_API_KEY 가 비어 콘솔로 보낸다")
    return ConsoleMailer()


# 루프백과, dev 에서 사실상 같은 뜻으로 쓰이는 0.0.0.0 까지만 로컬로 친다.
# 사설 IP(192.168.x.x 등)는 일부러 뺐다 — 그 주소로 띄운 것은 이미 나 말고도
# 닿는 사람이 있는 배포이고, 그 로그도 마찬가지다.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _looks_local(url: str) -> bool:
    """이 주소가 개발자 본인 기계를 가리키는가.

    문자열 앞부분이 아니라 호스트를 파싱해서 본다. 앞부분으로 보면
    ``http://localhost.evil.example`` 같은 남의 도메인이 로컬로 통과하고,
    반대로 로컬을 https 로 띄우면(``https://localhost:3000``) 로컬이 아닌
    것이 된다. 경고 하나를 잘못 내는 정도면 감수할 만했지만, 이제
    ConsoleMailer 가 이 판정으로 **비밀을 찍을지 말지**를 정하므로 양쪽 다
    틀리면 안 된다.

    파싱이 실패하거나 스킴이 없으면 hostname 은 None 이고, 그 경우 로컬이
    아닌 것으로 본다 — 모르는 쪽은 공개로 취급해야 닫히는 방향이다.
    """
    return (urlsplit(url).hostname or "") in _LOCAL_HOSTS


def warn_about_mail_configuration() -> None:
    """기동 훅. 메일 설정이 이 배포가 실제로 있는 곳과 맞는지 본다.

    짝인 두 검사를 모두 돈다. 같은 질문의 양쪽이라 한 훅으로 묶는 편이
    맞다 — 한쪽은 "진짜로 보내는데 링크가 로컬", 다른 쪽은 "공개인데
    아무것도 안 보낸다"이다.

    한동안 이름이 warn_if_base_url_looks_local 이었다. 그때는 첫 검사
    하나뿐이라 맞는 이름이었는데, 짝이 붙으면서 이름이 내용의 절반만
    가리키게 됐다 — 함수가 무엇을 하는지 호출부(app/main.py 의 lifespan)
    에서 읽을 수 없게 된 것이라 이름 쪽을 고쳤다.
    """
    _warn_if_link_points_at_localhost()
    warn_if_console_mailer_is_public()


def warn_if_console_mailer_is_public() -> None:
    """공개 주소로 뜬 배포가 콘솔 메일러로 돌고 있는 상태를 기동 때 알린다.

    아래 _warn_if_link_points_at_localhost 의 짝이다. 두 상태 모두 발송이
    실패로 보이지 않아서 아무도 알아채지 못한 채 지나간다 — 이쪽은 아예
    보내지 않았는데도 가입 응답은 201 로 끝난다.

    실제 유출은 이 경고가 아니라 ConsoleMailer 가 본문을 빼는 것으로 막는다.
    이 함수의 값은 시점이다: ConsoleMailer 의 경고는 누군가 가입해야 나오는데,
    그러려면 이미 인증 메일 한 통이 허공으로 날아간 뒤다. 이건 배포 직후
    기동 로그에 뜬다.

    막지 않고 경고만 하는 것은 get_mailer() 가 정해둔 태도 그대로다. 여기서
    기동을 막으면 메일 설정 하나 때문에 배포 전체가 서고, 이미 가입한
    사용자의 질의까지 같이 죽는다 — 경고보다 비싼 실패다.

    get_mailer() 로 판정하는 것은 "콘솔 메일러가 도는가"의 정답이 거기
    하나뿐이기 때문이다. MAIL_PROVIDER=resend 라도 키가 비면 콘솔로 내려오므로
    설정값만 보면 틀린다.
    """
    if _looks_local(settings.app_base_url):
        return
    if not isinstance(get_mailer(), ConsoleMailer):
        return
    logger.warning(
        "APP_BASE_URL=%s 는 공개 주소인데 콘솔 메일러가 돈다 — 인증 메일이 "
        "아무에게도 배달되지 않고, 가입자는 전원 맛보기 한도에 갇힌다. "
        "MAIL_PROVIDER=resend 와 RESEND_API_KEY 를 설정할 것. 그때까지 "
        "인증 링크는 로그에도 남기지 않는다(토큰 원문이라서다).",
        settings.app_base_url,
    )


def _warn_if_link_points_at_localhost() -> None:
    """실제로 발송하면서 링크는 여전히 localhost 를 가리키는 배포를 잡는다.

    ``MAIL_PROVIDER=resend`` 를 켰다는 것 자체가 "로컬에서 그냥 써본다"가
    아니라는 신호다 — 콘솔 폴백이 있는데 굳이 키를 넣을 이유가 없다. 그런데
    ``APP_BASE_URL`` 은 별도 설정이라 함께 바꾸는 것을 잊기 쉽고, 잊으면
    모든 인증 메일이 배포 도메인이 아니라 발신자의 localhost 를 가리키는
    죽은 링크로 나간다 — 가입자는 영원히 맛보기 한도에 갇히고, 재발송도
    같은 죽은 링크를 다시 보낼 뿐이다. 아무도 이 상태를 알아채지 못한다:
    발송 자체는 성공(200)하기 때문이다.

    ``get_mailer()`` 와 같은 태도로 막지 않고 로그에 남긴다 — 여기서 앱을
    못 띄우게 하면 설정 실수 하나가 배포 전체를 막는, 경고보다 비싼 실패가
    된다.
    """
    if settings.mail_provider == "resend" and _looks_local(settings.app_base_url):
        logger.warning(
            "MAIL_PROVIDER=resend 인데 APP_BASE_URL=%s 다 — 인증 메일 링크가 "
            "이 배포가 아니라 localhost 를 가리켜 모든 수신자에게 죽은 링크로 "
            "간다. APP_BASE_URL 을 배포 도메인으로 바꿀 것.",
            settings.app_base_url,
        )
