"""Keep the test suite from leaving anything behind.

Tests go through the real HTTP API, which writes uploaded PDFs to
``STORAGE_DIR`` and creates user rows. Cleaning those up is easy to forget in
any single test, and forgetting is silent — the run passes and the mess only
shows up later as a directory full of ``broken.pdf`` and a user table nobody
can read. Two runs left 42 accounts and 23 files behind before this existed.

So the cleanup lives here instead of in each test: storage is redirected to a
temporary directory for the whole session, and any account created against
``@example.com`` is swept afterwards. Individual tests can still clean up
eagerly — this is the net under them, not a replacement.
"""

import asyncio
import tempfile

import pytest
from sqlalchemy import text

from app.config import settings
from app.services.ratelimit import (
    auth_limiter,
    query_limiter,
    upload_limiter,
    verify_resend_limiter,
)


@pytest.fixture(autouse=True, scope="session")
def isolated_storage():
    """Point uploads at a temp directory so the repo's storage/ stays clean.

    ``_storage_path`` reads ``settings.storage_dir`` per call, so swapping the
    value is enough — no monkeypatching of the route.
    """
    original = settings.storage_dir
    with tempfile.TemporaryDirectory(prefix="docs-rag-test-") as tmp:
        settings.storage_dir = tmp
        yield tmp
    settings.storage_dir = original


@pytest.fixture(autouse=True, scope="session")
def sweep_test_accounts(isolated_storage):
    """Remove accounts the suite created, after it finishes.

    Only ``@example.com`` — the seed account that owns the evaluation corpus
    uses a different domain and must survive. Documents, chunks, conversations,
    usage and traces all cascade from the user row.
    """
    yield

    async def sweep() -> int:
        # Imported late: this runs after the tests, and importing the engine at
        # module scope would bind it to whichever loop imported it first.
        from app.db import SessionLocal, engine

        try:
            async with SessionLocal() as session:
                result = await session.execute(
                    text("DELETE FROM users WHERE email LIKE '%@example.com'")
                )
                await session.commit()
                return result.rowcount or 0
        finally:
            await engine.dispose()

    try:
        removed = asyncio.run(sweep())
    except Exception:
        return  # No database: nothing was created, nothing to sweep.

    if removed:
        print(f"\n[conftest] 테스트 계정 {removed}개 정리")


@pytest.fixture(autouse=True, scope="session")
def http_session_cookies():
    """테스트 클라이언트가 세션 쿠키를 들고 다닐 수 있게 한다.

    ``session_cookie_secure`` 기본값이 True 로 바뀐 뒤(보안 수정), 쿠키에
    ``Secure`` 가 붙는다. 그런데 httpx 의 ASGI 클라이언트는 ``http://test``
    를 베이스로 쓰므로 그런 쿠키를 **저장은 하되 되보내지 않는다** — 로그인
    직후의 ``GET /auth/me`` 가 401 로 돌아오고, 그 응답에는 ``id`` 가 없어
    테스트는 ``KeyError: 'id'`` 로 죽는다. 원인이 인증과 무관해 보여서
    한참을 엉뚱한 곳에서 찾게 되는 종류의 실패다.

    프로덕션 기본값은 건드리지 않는다. 여기서만 내려서 테스트 전송이
    평문이라는 사실과 맞춘다 — ``isolated_storage`` 가 저장 경로에 대해
    하는 일과 같다.
    """
    original = settings.session_cookie_secure
    settings.session_cookie_secure = False
    yield
    settings.session_cookie_secure = original


@pytest.fixture(autouse=True, scope="session")
def no_real_email():
    """메일 프로바이더를 콘솔로 고정한다 — 진짜로 보내면 안 되니까.

    ``app/config.py`` 는 앱과 테스트가 같은 ``.env`` 를 읽는다. 누군가
    README 대로 배포 준비를 하며 ``MAIL_PROVIDER=resend`` 와
    ``RESEND_API_KEY`` 를 채워두면, 다음 ``pytest`` 실행이 그대로
    ``api.resend.com`` 에 실제 발송을 시도한다. 이 스위트만 해도 가입을
    수십 번 만든다 — ``test_auth_ratelimit.py`` 하나가 열두 번(레이트리밋
    한도 + 2), ``/auth/signup`` 을 부르는 테스트는 아홉 개 파일에 흩어져
    있다. 존재하지 않는 ``@example.com`` 주소로 나가는 진짜 메일은 Resend
    무료 한도(하루 100통)를 태우고 발신 도메인에 하드 바운스를 남긴다.

    ``isolated_storage`` 가 저장 경로에 대해 하는 일과 같다 — 실제 자원을
    설정값 하나 잘못 둔 것만으로 건드리지 못하게 세션 내내 못박는다.
    """
    original_provider = settings.mail_provider
    original_key = settings.resend_api_key
    settings.mail_provider = "console"
    settings.resend_api_key = ""
    yield
    settings.mail_provider = original_provider
    settings.resend_api_key = original_key


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """레이트리밋 버킷을 테스트마다 비운다.

    버킷은 프로세스 안에 살아서(``services/ratelimit.py``) 테스트 사이를
    넘어간다. 인증 제한이 분당 10회인데 스위트는 그보다 훨씬 많은 계정을
    한 프로세스에서 만들기 때문에, 뒤쪽 테스트의 가입이 429 로 막히고
    그 응답에는 ``id`` 가 없어 ``KeyError: 'id'`` 로 죽는다. 혼자 돌리면
    통과하고 전체로 돌리면 깨지는, 원인을 찾기 어려운 실패다.

    한도 자체를 검사하는 테스트는 자기 안에서 버킷을 소진시키므로, 시작
    전에 비우는 것은 그쪽에도 안전하다 — 오히려 앞 테스트가 남긴 소비량에
    좌우되지 않게 해준다.
    """
    for limiter in (query_limiter, upload_limiter, auth_limiter, verify_resend_limiter):
        limiter.reset()
    yield
