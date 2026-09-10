"""메일 전송. 전송만 한다 — 무슨 내용을 보낼지는 호출자가 정한다.

어댑터가 둘인 이유는 개발과 운영이 서로를 막지 않게 하기 위해서다.
RESEND_API_KEY 가 없으면 콘솔로 내려오므로, 키 없이 clone 한 사람도 앱을
띄우고 전체 테스트를 돌릴 수 있다.

SMTP 가 아니라 HTTP API 를 쓰는 이유: 많은 호스팅이 25/465/587 포트를
막아두는데, 그 경우 SMTP 는 타임아웃으로만 실패해서 원인을 찾기 어렵다.
"""

import logging
from typing import Protocol

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
    """

    async def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        logger.warning("[mail] to=%s subject=%s\n%s", to, subject, text)


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


def _looks_local(url: str) -> bool:
    return url.startswith(("http://localhost", "http://127.0.0.1"))


def warn_if_base_url_looks_local() -> None:
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
