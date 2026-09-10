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
    """링크를 로그로 찍는다. 개발과 테스트에서 이게 메일함이다."""

    async def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        logger.info("[mail] to=%s subject=%s\n%s", to, subject, text)


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
