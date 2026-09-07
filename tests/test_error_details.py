"""The frontend maps error text by ``detail``, so the strings are an interface.

``web/lib/api/errors.ts`` turns a response into what the user reads, and it
keys on ``detail`` before status because one status can mean two different
things — 429 is both "you are asking too fast" (wait) and "you used up today's
questions" (waiting will not help). A detail the frontend does not know falls
back to a generic message, and the generic message gives the wrong advice.

That failure is silent: nothing breaks, the user is just told to do the wrong
thing. So this test pins the strings. Adding one here is cheap; the point is
that it fails when someone changes a message without touching the frontend.

Infrastructure-free: it reads the source, it does not call anything.
"""

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# Every literal detail the API can return, and how the frontend covers it.
# Keep in step with BY_DETAIL / BY_STATUS in web/lib/api/errors.ts.
EXPECTED = {
    # Mapped by detail — status alone cannot tell these apart.
    "query rate limit exceeded",
    "daily query quota exceeded",
    "upload rate limit exceeded",
    "monthly upload page quota exceeded",
    "email already registered",
    "invalid email or password",
    "search unavailable",
    "model quota exceeded",
    "model unavailable",
    # Mapped by status — the status is unambiguous on its own.
    "authentication required",  # 401
    "not found",  # 404
    "Only PDF uploads are supported in M1",  # 415
}

# Functions that build an HTTPException out of a detail handed to them, and
# the position that detail arrives in. The literal scan cannot see through
# these, so every one of them has to be listed — the guarantee this file makes
# is only as wide as this mapping, and
# ``test_every_detail_wrapper_is_registered`` is what keeps it honest.
DETAIL_HELPERS = {"too_many": 0, "_too_many": 0, "_retryable": 2}


def _literal_details() -> set[str]:
    """Every plain-string detail the app can raise.

    f-strings are skipped on purpose: their text varies with configuration, so
    the frontend cannot key on them and maps those responses by status.
    """
    found: set[str] = set()

    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            for keyword in node.keywords:
                if keyword.arg == "detail" and isinstance(
                    keyword.value, ast.Constant
                ):
                    if isinstance(keyword.value.value, str):
                        found.add(keyword.value.value)

            name = _called_name(node)
            index = DETAIL_HELPERS.get(name)
            if index is not None and index < len(node.args):
                arg = node.args[index]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(arg.value)

    return found


def _called_name(node: ast.Call) -> str | None:
    return getattr(node.func, "id", None) or getattr(node.func, "attr", None)


def _detail_wrappers() -> set[str]:
    """Functions that pass a detail they were *handed* into an HTTPException.

    Exactly the blind spots of the literal scan: from the outside such a call
    looks like any other, so its detail string never gets seen. Each one has
    to be registered in DETAIL_HELPERS with the position of its detail
    argument.

    An f-string detail is not one of these. It is unreadable to the scan for
    a different reason — its text depends on configuration — and that case is
    already handled by design: the frontend cannot key on it, so those
    responses are mapped by status instead. Counting them here would demand a
    registration that could never help.
    """
    wrappers: set[str] = set()

    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                if _called_name(node) != "HTTPException":
                    continue
                if any(
                    keyword.arg == "detail"
                    and not isinstance(keyword.value, (ast.Constant, ast.JoinedStr))
                    for keyword in node.keywords
                ):
                    wrappers.add(func.name)

    return wrappers


def test_every_detail_wrapper_is_registered():
    """The scan must not be allowed to shrink in silence.

    This is how the two Gemini details went missing: ``_retryable`` wraps
    HTTPException and takes its detail third, so the string literals at its
    call sites were invisible here — and the test still passed, reporting
    full coverage of a set it could no longer see all of.

    A test that quietly stops checking things is worse than no test, because
    it is trusted. So an unregistered wrapper fails loudly instead.
    """
    unregistered = _detail_wrappers() - set(DETAIL_HELPERS)
    assert not unregistered, (
        f"detail 을 넘겨받아 HTTPException 을 만드는 함수가 등록되지 않았습니다: "
        f"{sorted(unregistered)}\n"
        "DETAIL_HELPERS 에 {함수명: detail 인자 위치} 로 추가해 주세요. "
        "그러지 않으면 그 함수를 거치는 detail 은 이 파일의 검사에서 조용히 "
        "빠지고, 프론트에 매핑이 없어도 아무도 모릅니다."
    )


def test_error_details_match_the_frontend_map():
    found = _literal_details()

    unmapped = found - EXPECTED
    assert not unmapped, (
        "새 detail 문자열이 생겼습니다: "
        f"{sorted(unmapped)}\n"
        "web/lib/api/errors.ts 에 문구를 추가하고 (또는 상태 코드로 충분하면 "
        "그렇게 결정하고) 이 파일의 EXPECTED 에도 넣어 주세요. "
        "그러지 않으면 사용자는 일반 폴백 문구를 보게 되고, "
        "그 문구는 이 상황에 맞지 않는 행동을 안내합니다."
    )

    gone = EXPECTED - found
    assert not gone, (
        f"더 이상 쓰이지 않는 detail 이 남아 있습니다: {sorted(gone)}\n"
        "백엔드에서 지웠다면 EXPECTED 와 프론트 매핑에서도 지워 주세요."
    )


def test_details_are_not_shown_to_users_verbatim():
    """These strings are identifiers, not copy — they are all English.

    The frontend translates them; this test records why they are allowed to
    stay untranslated on the backend.
    """
    for detail in _literal_details():
        assert detail.isascii(), (
            f"{detail!r} 은 한국어입니다. detail 은 프론트가 문구를 고르는 "
            "식별자이지 사용자에게 그대로 보여줄 문장이 아닙니다."
        )
