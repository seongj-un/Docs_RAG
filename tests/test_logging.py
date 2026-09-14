"""로깅 배관에 대한 증명.

여기서 막으려는 것은 두 가지다.

하나는 **재발**이다. 앱의 로깅을 실제로 설정하던 것은 ``alembic.ini`` 였고,
기동 중 마이그레이션이 그 ini 로 루트 로거를 다시 짓는다. 3988fc9 에서 그
호출이 uvicorn 로거를 통째로 꺼서 M1~M6 내내 액세스 로그도 500 트레이스백도
없었다. 그때는 인자 하나로 증상만 막았고 결합은 남았다 — ``fileConfig`` 는
프로세스의 모든 핸들러를 flush+close 하기도 한다. 그러니 "앱이 깐 로깅이
마이그레이션을 통과하고도 살아 있는가"를 테스트가 붙들고 있어야 한다.

다른 하나는 **상관 ID** 다. Caddy 액세스 로그·uvicorn 액세스 로그·앱 로그·
``traces`` 행을 한 요청으로 묶을 키가 없었다.

⚠️ 이 파일이 ``app.router.lifespan_context`` 를 직접 쓰는 이유: 스위트의 나머지
전부가 ``ASGITransport(app=app)`` 만 쓰는데 그것은 lifespan 을 **아예 실행하지
않는다**. 기동 배관(로깅 설정, 마이그레이션)이 지금까지 통째로 테스트 밖에
있었다는 뜻이고, 위의 결함이 여섯 마일스톤 동안 CI 를 초록으로 통과한 이유가
그것이다.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from starlette.responses import StreamingResponse

from app.config import settings
from app.db import SessionLocal, engine, run_migrations
from app.logging import configure_logging, request_id
from app.main import app


def run_async(coro_fn):
    async def wrapper():
        try:
            return await coro_fn()
        finally:
            await engine.dispose()

    return asyncio.run(wrapper())


def _db_available() -> bool:
    async def check():
        try:
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.run(check())
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


@pytest.fixture
def pristine_logging():
    """루트 로거·레벨·레코드 팩토리는 프로세스 전역이다.

    ``configure_logging()`` 은 일부러 프로세스 하나에 한 번만 먹도록 되어
    있으므로, 되돌려 놓지 않으면 이 파일이 스위트의 나머지 테스트가 보는
    로깅 상태를 바꿔버린다. 되돌리는 김에 "지웠다가 다시 부르면 다시 깔린다"
    도 같이 보장된다 — 멱등성 판단이 모듈 플래그가 아니라 실제 핸들러 상태에
    걸려 있기 때문이다.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    factory = logging.getLogRecordFactory()
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.setLogRecordFactory(factory)


def _install() -> logging.Handler:
    """``configure_logging()`` 이 새로 매단 핸들러 하나를 돌려준다."""
    root = logging.getLogger()
    before = {id(handler) for handler in root.handlers}
    configure_logging()
    added = [handler for handler in root.handlers if id(handler) not in before]
    assert len(added) == 1, f"핸들러가 하나만 깔려야 한다: {added}"
    return added[0]


def test_a_log_line_is_stamped_in_utc_and_carries_the_request_id(pristine_logging):
    """포맷이 계약이다: 시각(UTC)·레벨·로거·상관 ID.

    시각이 UTC 인지를 tolerance 로 확인하는 이유는, 기본 converter 인
    localtime 으로 돌아가면 이 머신에서 정확히 9시간이 어긋나기 때문이다 —
    포맷 문자열만 봐서는 구분되지 않는 종류의 회귀다.
    """
    handler = _install()
    # 두 번 불러도 줄이 두 번 찍히면 안 된다(uvicorn --reload 는 이 경로를
    # 한 프로세스에서 여러 번 지난다).
    configure_logging()
    assert sum(h is handler for h in logging.getLogger().handlers) == 1

    logger = logging.getLogger("tests.format_probe")
    token = request_id.set("abc123")
    try:
        record = logger.makeRecord(
            logger.name, logging.ERROR, "f.py", 1, "hello", None, None
        )
    finally:
        request_id.reset(token)

    line = handler.format(record)
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.\d{3}Z "
        r"ERROR\s+tests\.format_probe \[abc123\] hello",
        line,
    )
    assert match, line

    stamped = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    assert abs((datetime.now(timezone.utc) - stamped).total_seconds()) < 60, line


def test_a_line_logged_outside_a_request_still_fills_the_id_column(pristine_logging):
    """기동·마이그레이션·백그라운드 줄도 같은 모양이어야 grep 이 성립한다."""
    handler = _install()
    logger = logging.getLogger("tests.format_probe")
    record = logger.makeRecord(
        logger.name, logging.ERROR, "f.py", 1, "booting", None, None
    )
    assert handler.format(record).endswith(" [-] booting")


@needs_db
def test_the_startup_migration_leaves_the_apps_logging_alone(
    pristine_logging, monkeypatch, tmp_path
):
    """마이그레이션을 **진짜로 태운 뒤에도** 앱이 깐 로깅이 그대로여야 한다.

    ``alembic/env.py`` 의 ``fileConfig`` 를 막지 않으면 전부 깨진다: 루트
    핸들러는 ini 의 console 핸들러로 통째로 교체되고, 레벨은 ini 의
    ``[logger_root] WARNING`` 으로 덮이며, 그 과정에서 프로세스의 모든 핸들러가
    flush+close 된다.

    레벨을 일부러 ERROR 로 바꿔 두는 이유는 기본값이 WARNING 이라 ini 값과
    같아서, 기본값 그대로면 "덮였다"와 "안 덮였다"가 구분되지 않기 때문이다.

    파일 핸들러를 하나 매다는 이유는 close 쪽 결함을 실제로 재현하기
    위해서다. 지금 이 결합이 멀쩡해 보이는 유일한 이유는 uvicorn 이
    ``StreamHandler`` 를 쓰고 그 ``close()`` 가 스트림을 닫지 않기 때문이다 —
    누가 로그를 파일로 빼는 순간 기동 직후 로그가 전멸한다.
    """
    monkeypatch.setattr(settings, "log_level", "ERROR")
    handler = _install()

    root = logging.getLogger()
    log_file = tmp_path / "probe.log"
    probe = logging.FileHandler(log_file, encoding="utf-8")
    root.addHandler(probe)

    handlers_before = root.handlers[:]
    format_before = handler.formatter._fmt

    run_async(run_migrations)

    assert root.handlers == handlers_before, "마이그레이션이 루트 핸들러를 갈아치웠다"
    assert root.level == logging.ERROR, "마이그레이션이 앱이 고른 레벨을 덮었다"
    assert handler.formatter._fmt == format_before

    logging.getLogger("tests.after_migration").error("여전히 흐른다")
    probe.flush()
    written = log_file.read_text(encoding="utf-8")
    assert "여전히 흐른다" in written

    # 그리고 ini 가 실제로 주던 것은 잃지 않았어야 한다: alembic 은 어떤
    # 리비전을 올렸는지를 INFO 로 알린다. 루트 기본값이 WARNING 이라
    # alembic 로거를 따로 INFO 로 두지 않으면, 로깅을 앱이 가져오면서
    # "배포가 무엇을 마이그레이션했는지"가 조용히 사라진다.
    assert "PostgresqlImpl" in written, "마이그레이션 로그가 사라졌다"

    root.removeHandler(probe)
    probe.close()


_PROBE_PATH = "/__request_id_probe__"
_probe_logger = logging.getLogger("tests.request_id_probe")


async def _probe_endpoint():
    async def body():
        # 스트림이 시작된 **뒤에** 찍는다. 상관 ID 가 정말 필요한 로그는
        # conversations.py 의 SSE 실패 로그이고 그건 응답 헤더가 이미 나간
        # 다음에 나온다 — 미들웨어가 그때까지 contextvar 를 쥐고 있지 않으면
        # 이 작업은 정작 필요한 자리에서 아무것도 못 한다.
        _probe_logger.error("probe")
        yield b"ok"

    return StreamingResponse(body(), media_type="text/plain")


@needs_db
def test_every_response_carries_a_request_id_and_so_do_its_log_lines(
    pristine_logging, caplog
):
    """헤더와 로그 양쪽에 같은 ID 가, 요청마다 다르게 나와야 한다.

    실제 ``app`` 에 임시 라우트를 달아서 돌린다. 별도의 작은 앱을 만들면
    미들웨어가 실제로 ``app/main.py`` 에 매여 있는지, 순서가 맞는지는 증명되지
    않는다. 끝나면 라우트 목록을 원래 길이로 잘라 되돌린다.
    """
    routes_before = len(app.router.routes)
    app.add_api_route(_PROBE_PATH, _probe_endpoint, methods=["GET"])

    async def scenario():
        # lifespan 을 실제로 돈다: 로깅 설정과 마이그레이션이 여기서만 일어난다.
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                return [
                    await client.get(_PROBE_PATH),
                    await client.get(
                        _PROBE_PATH, headers={"X-Request-Id": "caddy-7f3a.1"}
                    ),
                    await client.get(
                        _PROBE_PATH, headers={"X-Request-Id": "no spaces allowed"}
                    ),
                    await client.get(_PROBE_PATH, headers={"X-Request-Id": "a" * 65}),
                ]

    try:
        generated, echoed, misshapen, too_long = run_async(scenario)
    finally:
        del app.router.routes[routes_before:]

    minted = re.compile(r"[0-9a-f]{32}")
    assert minted.fullmatch(generated.headers["x-request-id"])
    # 프록시가 이미 붙여 보낸 ID 는 이어받는다 — 그러지 않으면 Caddy 의
    # 액세스 로그와 앱 로그를 묶을 방법이 다시 없어진다.
    assert echoed.headers["x-request-id"] == "caddy-7f3a.1"
    # 모양이 수상하거나 너무 긴 값은 이어받지 않고 새로 만든다. 이 값은 모든
    # 로그 줄과 응답 헤더에 그대로 박히므로, 남이 준 문자열을 검사 없이 싣는
    # 것은 로그 위조·헤더 주입을 그대로 허용하는 것과 같다.
    assert misshapen.headers["x-request-id"] != "no spaces allowed"
    assert minted.fullmatch(misshapen.headers["x-request-id"])
    assert minted.fullmatch(too_long.headers["x-request-id"])

    logged = [r.request_id for r in caplog.records if r.name == _probe_logger.name]
    assert logged == [
        response.headers["x-request-id"]
        for response in (generated, echoed, misshapen, too_long)
    ], "로그 줄에 실린 ID 가 응답이 돌려준 ID 와 요청별로 일치해야 한다"
    assert len(set(logged)) == 4, "요청마다 달라야 한다 — 안 그러면 묶을 수가 없다"


def _record_from(logger_name: str) -> tuple[logging.Handler, list[logging.LogRecord]]:
    """이름 붙은 로거에 매달아 두고 지나가는 레코드를 모으는 핸들러."""
    seen: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    handler = Collect(level=logging.DEBUG)
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return handler, seen


def test_a_healthy_health_check_leaves_no_access_line(pristine_logging):
    """컨테이너 헬스체크가 액세스 로그의 대부분이 되는 것을 막는다.

    30초마다 한 줄이면 아무도 안 쓰는 날에도 하루 2,880 줄이고, 로그
    드라이버가 보관하는 10MB(docker-compose.yml) 안에서 진짜 요청을 밀어낸다.
    단 **실패한** 헬스체크는 남겨야 한다 — 그 줄이 "컨테이너가 재시작되는
    중"과 "컨테이너는 멀쩡한데 그 앞이 이상하다"를 가른다.
    """
    _install()
    handler, seen = _record_from("uvicorn.access")
    try:
        def access(path: str, status: int) -> None:
            logging.getLogger("uvicorn.access").info(
                '%s - "%s %s HTTP/%s" %d', "127.0.0.1:52000", "GET", path, "1.1", status
            )

        access("/health", 200)
        access("/health", 503)
        access("/documents", 200)
        # 모양이 다른 레코드(포맷을 바꾸거나 다른 곳에서 온 것)는 건드리지
        # 않는다 — 이해하지 못한 레코드를 삼키는 필터가 나중에 진단을 잡아먹는다.
        logging.getLogger("uvicorn.access").info("startup complete")
    finally:
        logging.getLogger("uvicorn.access").removeHandler(handler)

    passed_through = [
        record.args[2] if isinstance(record.args, tuple) and len(record.args) == 5
        else record.msg
        for record in seen
    ]
    assert passed_through == ["/health", "/documents", "startup complete"], (
        f"성공한 헬스체크만 빠져야 한다: {passed_through}"
    )
    assert seen[0].args[4] == 503


@needs_db
def test_a_failed_statement_does_not_dump_its_parameters_into_the_log():
    """DB 오류 문자열이 바인딩 값을 통째로 쏟지 않는다.

    ``hide_parameters`` 가 없으면 StatementError 의 텍스트에
    ``[parameters: (...)]`` 로 **그 statement 의 모든 값**이 최대 300자까지
    붙는다. 이 앱에서 가장 실패하기 쉬운 INSERT 가 ``traces`` 와
    ``messages`` 이고, 거기 실리는 값이 질문 전문과 답변 전문이다. 그 예외는
    곧바로 ``logger.exception`` 이 받는다(``tracing.py``·``conversations.py``)
    — 삼킨 실패를 보이게 하려고 만든 그 경로가 동시에 계약서를 찍어내는
    경로였다는 뜻이다.

    ⚠️ 이것은 완전한 차단이 아니다. 드라이버가 **자기 메시지 안에** 문제가
    된 값을 직접 적는 경우가 있고(asyncpg 의 타입 오류가 ``$1: '...'`` 를
    적는다) 그건 SQLAlchemy 의 설정으로 못 막는다. 그래서 이 테스트는 막힌
    것을 정확히 고정한다: **오류와 무관한 나머지 파라미터**가 더 이상 딸려
    나오지 않는다는 것. 실제 유출량의 차이는 값 하나와 값 전부의 차이다.
    """
    answer = "답변-전문처럼-생긴-값"

    async def scenario():
        async with SessionLocal() as session:
            with pytest.raises(Exception) as caught:  # noqa: PT011 - 드라이버 예외
                await session.execute(
                    text("SELECT CAST(:bad AS int), :answer"),
                    {"bad": "숫자가-아님", "answer": answer},
                )
            return str(caught.value)

    message = run_async(scenario)
    assert answer not in message, "오류와 무관한 파라미터까지 예외 문자열에 실렸다"
    assert "hide_parameters" in message, "파라미터 덤프가 여전히 켜져 있다"
    # 진단에 필요한 것은 남는다 — 어떤 statement 였는지.
    assert "SELECT CAST" in message
