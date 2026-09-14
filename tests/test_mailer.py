"""메일 전송 어댑터. 인프라가 필요 없다 — 네트워크는 가짜로 대체한다."""

import asyncio
import contextlib
import logging

from app.config import settings
from app.services import mailer


@contextlib.contextmanager
def _mail_settings(**overrides):
    """설정을 잠깐 바꾸고 반드시 되돌린다.

    ``settings`` 는 프로세스 전역이라 하나라도 새면 다음 테스트가 이유 없이
    깨진다. 이 파일의 검사는 거의 전부 설정 **조합**에 걸려 있어서 특히 그렇다
    — 예를 들어 RESEND_API_KEY 가 남아 있으면 콘솔 폴백 경로가 통째로 안 돈다.
    """
    original = {name: getattr(settings, name) for name in overrides}
    for name, value in overrides.items():
        setattr(settings, name, value)
    try:
        yield
    finally:
        for name, value in original.items():
            setattr(settings, name, value)


def test_missing_key_falls_back_to_console():
    """키 없이 clone 해도 앱이 뜨고 테스트가 돌아야 한다."""
    with _mail_settings(mail_provider="resend", resend_api_key=""):
        assert isinstance(mailer.get_mailer(), mailer.ConsoleMailer)


def test_resend_is_used_when_configured():
    with _mail_settings(mail_provider="resend", resend_api_key="re_test_key"):
        assert isinstance(mailer.get_mailer(), mailer.ResendMailer)


def test_console_mailer_logs_the_link(caplog):
    """개발 중에는 로그가 메일함이다. 링크가 보이지 않으면 쓸모가 없다.

    레벨을 INFO 로 낮춰 잡지 않는다. 그렇게 하면 "호출이 일어났다"만
    증명되고 "실제 서버에서 보인다"는 증명되지 않는다 — uvicorn 기본
    설정에서 앱 로거 유효 레벨은 WARNING 이라, INFO 로 찍던 시절 이 줄은
    로컬 서버 어디에도 나타나지 않았고 계정을 인증할 방법이 없었다.

    APP_BASE_URL 을 명시로 고정한다. 본문을 찍을지 말지가 이제 그 값에
    달려 있어서, 기본값에 기대면 이 테스트가 무엇을 보장하는지 흐려진다.
    """
    with _mail_settings(app_base_url="http://localhost:3000"):
        with caplog.at_level("WARNING"):
            asyncio.run(
                mailer.ConsoleMailer().send(
                    to="who@example.com",
                    subject="제목",
                    html="<a href='http://localhost:3000/verify#token=abc'>x</a>",
                    text="http://localhost:3000/verify#token=abc",
                )
            )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "who@example.com" in logged
    assert "http://localhost:3000/verify#token=abc" in logged
    assert caplog.records, "기록이 하나도 없으면 링크는 아무 데도 안 나온다"
    assert all(r.levelno >= logging.WARNING for r in caplog.records)


def test_console_mailer_keeps_the_body_out_of_a_public_deployments_log(caplog):
    """본문에는 인증 토큰 **원문**이 들어 있다. 공개 배포 로그에 남으면 안 된다.

    DB 에는 sha256 해시만 두고 "DB 가 새도 링크는 만들 수 없다"고 해둔
    설계를 이 한 줄이 무효로 만들었다. 게다가 MAIL_PROVIDER 기본값이
    console 이라 예외 경로가 아니라 설정을 안 바꾼 배포의 기본 동작이었다.
    """
    link = "https://docs-rag.example.com/verify#token=SECRET-TOKEN-VALUE"
    with _mail_settings(
        mail_provider="console", app_base_url="https://docs-rag.example.com"
    ):
        with caplog.at_level("WARNING"):
            asyncio.run(
                mailer.ConsoleMailer().send(
                    to="who@example.com",
                    subject="이메일 주소를 확인해 주세요",
                    html=f"<a href='{link}'>x</a>",
                    text=link,
                )
            )

    assert "SECRET-TOKEN-VALUE" not in caplog.text
    assert "/verify" not in caplog.text, "링크 조각도 남기지 않는다"
    # 조용히 사라지지는 않는다: 무엇이 배달되지 않았는지와 고치는 법은 남는다.
    assert "who@example.com" in caplog.text
    assert "이메일 주소를 확인해 주세요" in caplog.text
    assert "MAIL_PROVIDER" in caplog.text
    assert caplog.records
    assert all(r.levelno >= logging.WARNING for r in caplog.records)


def test_console_mailer_still_prints_the_link_for_a_local_https_deployment(caplog):
    """로컬을 https 로 띄웠다고 메일함이 사라지면 안 된다.

    문자열 앞부분("http://localhost")으로 로컬을 판정하던 시절 이 조합은
    공개로 분류됐다. 로컬 판정이 틀리면 여기서는 링크를 못 보고, 반대
    방향으로 틀리면 토큰이 로그에 남는다 — 그래서 호스트를 파싱한다.
    """
    with _mail_settings(app_base_url="https://localhost:3000"):
        with caplog.at_level("WARNING"):
            asyncio.run(
                mailer.ConsoleMailer().send(
                    to="who@example.com",
                    subject="제목",
                    html="<a href='https://localhost:3000/verify#token=abc'>x</a>",
                    text="https://localhost:3000/verify#token=abc",
                )
            )

    assert "https://localhost:3000/verify#token=abc" in caplog.text


def test_console_mailer_treats_a_localhost_lookalike_domain_as_public(caplog):
    """localhost 로 **시작하는** 남의 도메인이 앞부분 검사를 통과했었다."""
    link = "http://localhost.evil.example/verify#token=SECRET-TOKEN-VALUE"
    with _mail_settings(app_base_url="http://localhost.evil.example"):
        with caplog.at_level("WARNING"):
            asyncio.run(
                mailer.ConsoleMailer().send(
                    to="who@example.com", subject="제목", html=link, text=link
                )
            )

    assert "SECRET-TOKEN-VALUE" not in caplog.text


def test_warns_when_resend_is_live_but_the_link_still_points_at_localhost(caplog):
    """이 조합이면 발송은 200으로 성공하면서 모든 수신자가 죽은 링크를 받는다."""
    with _mail_settings(
        mail_provider="resend",
        resend_api_key="re_test_key",
        app_base_url="http://localhost:3000",
    ):
        with caplog.at_level("WARNING"):
            mailer.warn_about_mail_configuration()
        assert "APP_BASE_URL" in caplog.text


def test_warns_when_a_public_deployment_runs_the_console_mailer(caplog):
    """짝이 되는 반대 실수다: 공개 주소인데 메일이 아무에게도 가지 않는다.

    이것도 실패로 보이지 않는다 — 가입 응답은 201 로 끝난다. 그리고 이
    조합에서는 콘솔 메일러가 본문을 빼므로 링크조차 남지 않는다. 기동
    로그의 이 한 줄이 아니면 알아챌 방법이 없다.

    main.py 의 lifespan 이 부르는 이름(warn_about_mail_configuration)으로
    호출한다 — 검사가 실제로 기동 훅에 물려 있는지까지 같이 본다.
    """
    with _mail_settings(
        mail_provider="console", app_base_url="https://docs-rag.example.com"
    ):
        with caplog.at_level("WARNING"):
            mailer.warn_about_mail_configuration()

    assert "콘솔 메일러" in caplog.text
    assert "docs-rag.example.com" in caplog.text


def test_warns_when_resend_is_selected_but_the_key_is_missing_on_a_public_host(caplog):
    """설정값만 보면 resend 라 멀쩡해 보이는데 실제로 도는 것은 콘솔이다.

    그래서 판정을 settings 가 아니라 get_mailer() 에 맡긴다.
    """
    with _mail_settings(
        mail_provider="resend",
        resend_api_key="",
        app_base_url="https://docs-rag.example.com",
    ):
        with caplog.at_level("WARNING"):
            mailer.warn_about_mail_configuration()

    assert "콘솔 메일러" in caplog.text


def test_does_not_warn_once_app_base_url_is_a_real_domain(caplog):
    """키까지 채운다. 키가 비면 resend 설정이어도 콘솔로 내려가 다른 경고가 난다."""
    with _mail_settings(
        mail_provider="resend",
        resend_api_key="re_test_key",
        app_base_url="https://docs-rag.example.com",
    ):
        with caplog.at_level("WARNING"):
            mailer.warn_about_mail_configuration()
        assert caplog.records == []


def test_does_not_warn_in_the_default_local_dev_configuration(caplog):
    """가장 흔한 상태(둘 다 기본값)에서 매 기동마다 경고가 찍히면 안 된다."""
    with _mail_settings(mail_provider="console", app_base_url="http://localhost:3000"):
        with caplog.at_level("WARNING"):
            mailer.warn_about_mail_configuration()
        assert caplog.records == []


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
