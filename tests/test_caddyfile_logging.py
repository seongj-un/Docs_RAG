"""프록시 접근 로그가 이메일 인증 토큰을 평문으로 남기지 않는지 본다.

설정 파일을 읽어서 검사하는 테스트다. 프록시를 실제로 띄워 요청을 넣는
편이 더 정확하지만 그러려면 docker 가 있어야 하고, 이 저장소의 테스트는
인프라 없이 도는 것이 전제다(tests/test_mailer.py 첫 줄). 그래서 여기서는
"필터가 붙어 있는가"만 지키고, 그 필터가 **실제로 토큰을 지우는가**는
caddy:2-alpine 컨테이너에 요청을 직접 넣어 손으로 확인했다:

    docker run --rm -e SITE_ADDRESS=":80" -p 8080:80 \\
      -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2-alpine

막으려는 것은 조용한 퇴행이다. 이 블록을 인자 없는 ``log`` 로 되돌리면
설정은 여전히 유효하고 프록시도 잘 뜬다 — 달라지는 것은 로그 줄 안에
토큰 원문이 다시 들어간다는 것뿐이라, 아무도 눈치채지 못한다.
"""

import ast
import re
import uuid
from pathlib import Path

CADDYFILE = Path(__file__).resolve().parents[1] / "Caddyfile"


def _directives() -> list[str]:
    """주석을 걷어낸 지시자 줄들. 주석 안의 단어에 속지 않기 위해서다."""
    lines = []
    for line in CADDYFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


def test_no_bare_log_directive_survives():
    """인자 없는 log 는 요청 URI 를 그대로 적는다 — 레거시 링크가 그 URI 에 온다."""
    assert "log" not in _directives(), (
        "인자 없는 log 가 돌아왔다. 새 인증 링크는 /verify#token=<원문> 이라 "
        "토큰이 URI 에 실리지 않지만, TTL 안에 살아 있는 레거시 "
        "/verify?token=<원문> 링크는 그대로 기록된다"
    )


def test_the_token_is_redacted_from_both_the_uri_and_the_referer():
    """URI 만 막으면 모자란다.

    프론트와 API 가 한 오리진이라 /verify 페이지가 보내는 모든 동일 출처
    요청에 Referer 로 전체 URL(=토큰 포함)이 따라붙는다. 브라우저 기본
    referrer policy 가 동일 출처에는 쿼리스트링까지 붙여 보내기 때문이다.
    """
    body = "\n".join(_directives())
    assert re.search(r"request>uri\s+query\s*\{\s*replace\s+token\b", body)
    assert re.search(r"request>headers>Referer\s+query\s*\{\s*replace\s+token\b", body)


def test_the_filter_is_applied_to_the_error_logger_too():
    """업스트림이 죽어 502 가 나는 동안에도 토큰이 남으면 안 된다.

    http.log.error 는 사이트 블록의 log 설정을 타지 않고 기본 로거로 나간다.
    그래서 같은 필터를 global 블록에도 한 번 더 건다 — import 가 두 번
    나오는 것이 그 뜻이다.
    """
    imports = [line for line in _directives() if line == "import redact_verify_token"]
    assert len(imports) == 2, "접근 로그와 기본(오류) 로거 양쪽에 걸려 있어야 한다"


# ── 요청 상관 ID ────────────────────────────────────────────────────────
# 아래는 토큰 리댁션과 다른 관심사다: 프록시 홉이 앱 로그와 이어져 있는가.
#
# 여기서도 설정 파일만 읽는다(위와 같은 이유). 실제 동작은 caddy:2-alpine
# v2.11.4 에 일회용 컨테이너로 요청을 넣어 확인했다 — 업스트림은 받은
# 헤더를 되비추는 작은 서버로 두고, 프록시 액세스 로그와 대조했다:
#
#   docker run --rm -e SITE_ADDRESS=":80" -p 18090:80 \
#     -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2-alpine
#
#   헤더 없음      -> 업스트림이 받은 값 == 로그의 request_id == 로그의 uuid
#   정상 헤더      -> 그 값이 그대로 간다(이어받기)
#   65자·공백·빈값 -> 버리고 우리가 만든 값으로 간다
#   중복 헤더      -> 값 하나로 합쳐져 우리가 만든 값으로 간다
#   ESC·NUL        -> Caddy 에 닿기 전에 Go 의 HTTP 서버가 400 으로 끊는다
#   업스트림 down  -> 502 줄에도 request_id 가 남는다
#
# 막으려는 것은 여기서도 조용한 퇴행이다. 아래 지시자 중 하나가 사라져도
# 설정은 유효하고 프록시도 잘 뜬다. 달라지는 것은 사고가 났을 때 두 로그를
# 이을 수 없다는 것뿐이고, 그건 사고가 난 뒤에야 알게 된다.

APP_LOGGING = Path(__file__).resolve().parents[1] / "app" / "logging.py"


def _app_acceptable_id_pattern() -> str:
    """``app/logging.py`` 의 ``_ACCEPTABLE_ID`` 정규식 원문.

    import 하지 않고 소스를 파싱한다. 이 파일은 인프라 없이 도는 것이
    전제인데(맨 위 참조) ``app.logging`` 은 starlette 와 설정을 끌고 온다.
    문자열 검색이 아니라 ast 인 것은 ``_directives()`` 가 주석을 걷어내는
    것과 같은 이유다 — 주석 안에 적힌 같은 모양의 정규식에 속지 않는다.
    """
    tree = ast.parse(APP_LOGGING.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "_ACCEPTABLE_ID" for t in node.targets
        ):
            continue
        call = node.value
        assert isinstance(call, ast.Call), "_ACCEPTABLE_ID 가 re.compile(...) 이 아니다"
        literal = call.args[0]
        assert isinstance(literal, ast.Constant), "정규식이 리터럴이 아니다"
        return literal.value
    raise AssertionError("app/logging.py 에서 _ACCEPTABLE_ID 를 찾지 못했다")


def test_the_id_is_both_forwarded_upstream_and_written_to_our_own_log():
    """한쪽만으로는 아무것도 묶이지 않는다.

    넘기기만 하면 앱 로그에는 ID 가 있고 프록시 로그에는 없다. 남기기만
    하면 앱이 자기 ID 를 따로 만들어 두 값이 서로 남남이 된다.
    """
    body = "\n".join(_directives())
    assert re.search(r"^request_header\s+@\S+\s+X-Request-Id\s+\S+$", body, re.M), (
        "업스트림으로 넘기는 쪽이 사라졌다. 앱이 매번 자기 ID 를 새로 만든다"
    )
    assert re.search(
        r"^log_append\s+request_id\s+\{http\.request\.header\.X-Request-Id\}$",
        body,
        re.M,
    ), "액세스 로그에 남기는 쪽이 사라졌다. 프록시 줄에 상관 ID 가 없다"


def test_the_proxy_and_the_app_agree_on_what_an_id_may_look_like():
    """알파벳이 갈라지면 정확히 이 설정이 막으려던 자리에서 사슬이 끊긴다.

    프록시가 통과시킨 값을 앱이 버리고 자기 것을 새로 만들면, 두 로그의
    ID 가 달라진다. 그런데 설정도 앱도 멀쩡히 돌기 때문에 아무도 모른다.
    앱은 ``fullmatch`` 로 보므로 프록시 쪽은 같은 알파벳에 ^$ 만 붙는다.
    """
    app_pattern = _app_acceptable_id_pattern()
    match = re.search(r'\.matches\("([^"]+)"\)', "\n".join(_directives()))
    assert match, "프록시 쪽 검사식이 사라졌다"
    assert match.group(1) == f"^{app_pattern}$", (
        f"프록시는 {match.group(1)!r}, 앱은 {app_pattern!r} 로 본다. "
        "한쪽을 고쳤으면 양쪽을 고쳐라"
    )


def test_the_id_caddy_mints_would_survive_the_app_s_own_check():
    """우리가 만든 값을 앱이 버리면 넘기는 의미가 없다.

    앱은 ``uuid4().hex``(하이픈 없음)를 쓰지만 Caddy 의
    ``{http.request.uuid}`` 는 하이픈이 붙은 표준 표기다. 알파벳에
    하이픈이 들어 있어서 통과하는 것이지 우연이 아니다 — 알파벳에서
    하이픈을 빼면 프록시가 넣어준 모든 ID 를 앱이 버리게 된다.
    """
    app_pattern = _app_acceptable_id_pattern()
    minted = str(uuid.uuid4())
    assert "-" in minted  # 전제가 바뀌면 이 테스트가 먼저 말해줘야 한다
    assert re.fullmatch(app_pattern, minted), (
        f"앱이 {minted!r} 를 버린다. 프록시가 넣어주는 값이 바로 이 모양이다"
    )


def test_the_uuid_is_minted_unconditionally():
    """``{http.request.uuid}`` 는 **게으르게** 만들어진다.

    한 번도 전개하지 않은 요청에는 UUID 가 아예 생기지 않고 액세스 로그의
    ``uuid`` 칸이 통째로 빠진다. 실측으로 겪은 일이다: 처음 쓴 설정은
    ``request_header`` 안에서만 전개했는데, 그러면 클라이언트가 멀쩡한 ID 를
    보낸 요청 — 매처가 떨어져 ``request_header`` 가 안 도는 요청 — 에만
    ``uuid`` 가 사라졌다. 하필 클라이언트가 고를 수 없는 서버 쪽 키가 가장
    필요한 경우다. 그래서 매처 바깥의 ``vars`` 로 먼저 붙들어 둔다.
    """
    directives = _directives()
    minted = [
        line
        for line in directives
        if line.startswith("vars ") and "{http.request.uuid}" in line
    ]
    assert len(minted) == 1, (
        "매처 바깥에서 {http.request.uuid} 를 전개하는 vars 가 없다. "
        "클라이언트가 ID 를 보낸 요청의 로그에서 uuid 칸이 사라진다"
    )
    assert not re.search(
        r"^request_header\s+@\S+\s+X-Request-Id\s+\{http\.request\.uuid\}$",
        "\n".join(directives),
        re.M,
    ), "request_header 가 vars 를 거치지 않고 직접 전개한다 — 위의 함정 그대로다"
