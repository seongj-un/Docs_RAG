"""메일 전송 어댑터. 인프라가 필요 없다 — 네트워크는 가짜로 대체한다."""

import asyncio

from app.config import settings
from app.services import mailer


def test_missing_key_falls_back_to_console():
    """키 없이 clone 해도 앱이 뜨고 테스트가 돌아야 한다."""
    original_provider, original_key = settings.mail_provider, settings.resend_api_key
    try:
        settings.mail_provider = "resend"
        settings.resend_api_key = ""
        assert isinstance(mailer.get_mailer(), mailer.ConsoleMailer)
    finally:
        settings.mail_provider, settings.resend_api_key = original_provider, original_key


def test_resend_is_used_when_configured():
    original_provider, original_key = settings.mail_provider, settings.resend_api_key
    try:
        settings.mail_provider = "resend"
        settings.resend_api_key = "re_test_key"
        assert isinstance(mailer.get_mailer(), mailer.ResendMailer)
    finally:
        settings.mail_provider, settings.resend_api_key = original_provider, original_key


def test_console_mailer_logs_the_link(caplog):
    """개발 중에는 로그가 메일함이다. 링크가 보이지 않으면 쓸모가 없다."""

    async def scenario():
        await mailer.ConsoleMailer().send(
            to="who@example.com",
            subject="제목",
            html="<a href='https://example.test/verify?token=abc'>x</a>",
            text="https://example.test/verify?token=abc",
        )

    with caplog.at_level("INFO"):
        asyncio.run(scenario())

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "who@example.com" in logged
    assert "https://example.test/verify?token=abc" in logged


def test_resend_posts_the_expected_payload(monkeypatch):
    """계약을 고정한다. 필드 이름이 틀리면 조용히 안 보내진다."""
    captured: dict = {}

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr(mailer.httpx, "AsyncClient", _Client)

    async def scenario():
        await mailer.ResendMailer(
            api_key="re_test_key", sender="no-reply@example.test"
        ).send(to="who@example.com", subject="제목", html="<p>본문</p>", text="본문")

    asyncio.run(scenario())

    # 잡아만 두고 검사하지 않으면 timeout 을 떼어내도 아무도 모른다.
    # 매달린 배경 작업은 워커를 잡으므로 상한이 있다는 사실 자체가 계약이다.
    assert captured["timeout"] == 10.0
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer re_test_key"
    assert captured["json"]["from"] == "no-reply@example.test"
    assert captured["json"]["to"] == ["who@example.com"]
    assert captured["json"]["subject"] == "제목"
    assert captured["json"]["html"] == "<p>본문</p>"
    assert captured["json"]["text"] == "본문"
