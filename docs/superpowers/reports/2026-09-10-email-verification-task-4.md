# Task 4 report — 인증 API: signup 훅 · verify · resend

## Status: DONE

Commit: `988942acfc2e1ddc61c7511a813ff987b0371d6e` (short `988942a`), on branch
`feat/email-verification`.

## Files changed

### `app/schemas.py`

Added `VerifyRequest` (a single `token: str = Field(..., min_length=1, max_length=512)`,
with the brief's comment explaining why the token rides in the POST body and
not a GET query param: mail scanners pre-fetch links and would burn a
one-shot token before the user ever clicks). Added `email_verified: bool =
False` to `UserOut`, with the brief's comment noting it reads off the
`User.email_verified` property. Both inserted verbatim from the brief's Step
3 block, in the position specified (`VerifyRequest` after `LoginRequest`,
the new field inside `UserOut`).

No route needed to change to populate `email_verified` — `UserOut`'s
`model_config = ConfigDict(from_attributes=True)` was already in place, so
FastAPI's response serialization reads the property directly off the `User`
ORM instance returned by every route.

### `app/services/ratelimit.py`

Appended `verify_resend_limiter = TokenBucketLimiter(settings.rate_limit_verify_resend_per_min)`
after `auth_limiter`, with the brief's comment verbatim. `settings.rate_limit_verify_resend_per_min`
already existed in `app/config.py` (value `1`, added ahead of this task per
its own comment: "인증 메일 재발송... 하루 100통이라 태울 수 있다") — nothing
to add there.

### `app/routers/auth.py`

- Import block: added `import logging`; changed the `fastapi` import to
  include `BackgroundTasks`; added `VerifyRequest` to the `app.schemas`
  import; added `mailer, verification` to the `app.services` import; added
  `verify_resend_limiter` to the `app.services.ratelimit` import. Added
  `logger = logging.getLogger(__name__)` above `router = APIRouter(...)`,
  per the brief's "파일 상단(라우터 정의 위)에 로거를 둔다."
- Added `_deliver_verification(email, raw_token)` right after
  `_set_session_cookie`, exactly as given: builds the email via
  `verification.build_email(verification.build_link(raw_token))`, sends
  through `mailer.get_mailer().send(...)`, and wraps the send in a broad
  `except Exception` that logs via `logger.exception` and swallows the
  error. This is the containment boundary for the constraint noted in my
  task context — a Resend failure surfaces as a raw `httpx.HTTPStatusError`/
  `RequestError`, and this is the only place that catches it, deliberately
  broadly, since it runs inside a `BackgroundTask` where an uncaught
  exception would have nowhere to go.
- `signup`: added `background: BackgroundTasks` to the signature; after the
  `IntegrityError` guard, added `raw = await verification.issue_token(session, user.id)`
  followed by `background.add_task(_deliver_verification, user.email, raw)`,
  before the existing session-cookie logic. Token issuance stays inside the
  request (it needs the DB and commits); only the send is deferred.
- Added `POST /verify` and `POST /resend-verification`, inserted before
  `me` as instructed. `verify` takes no `Depends(get_current_user)` (a
  logged-out caller in a different browser must be able to use it) and does
  nothing to the session before calling `verification.consume_token` — no
  `session.add`/uncommitted work precedes it, which matters because
  `consume_token` rolls back the whole transaction on its non-OK paths (its
  own docstring: "OK 가 아닌 경로에서는 세션을 rollback 한다... 커밋하지
  않은 session.add() 를 들고 들어와서 ALREADY/INVALID 를 받으면 그 작업이
  조용히 사라진다"). `resend_verification` depends on `get_current_user`
  (needs a session) and checks `user.email_verified` and raises 409 **before**
  calling `verify_resend_limiter.allow(...)`, so a verified user retrying
  can't burn their own bucket.

Router order is now: signup, login, logout, verify, resend-verification, me
— matching the brief.

### `tests/conftest.py` (not in the brief's own file list — see Deviations)

Added `verify_resend_limiter` to the `app.services.ratelimit` import
(reformatted to multi-line for four names) and to the `fresh_rate_limits`
fixture's reset tuple: `(query_limiter, upload_limiter, auth_limiter, verify_resend_limiter)`.
This is the fix for the trap the brief's warning block calls out: the new
limiter's buckets live in-process and persist across tests, and at
`rate_limit_verify_resend_per_min = 1` the second test to call
`/auth/resend-verification` would otherwise see 429 immediately after
signup.

### `tests/test_verification_api.py` (new)

Same 8 test names, assertions, and Korean docstrings as the brief's Step 1
block, verbatim. Only the execution mechanics changed, per the brief's own
opening warning and the task's global constraints:

- `pytestmark = pytest.mark.asyncio` → `pytestmark = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")`.
- Added `run_async(coro_fn)` and `_db_available()`, copied verbatim from
  `tests/test_verification.py` (identical to the copy in `tests/test_isolation.py`).
- Every `async def test_*` became a synchronous `def test_*` that defines a
  local `async def scenario(): ...` closure and calls `run_async(scenario)`;
  values needed for post-hoc assertions (an `httpx.Response`, a response
  body dict, or a pair of responses) are returned from `scenario` and
  asserted on afterward — the same shape `test_verification.py` uses for
  ORM objects (e.g. `test_valid_token_marks_the_user_verified` asserts on
  `verified.email_verified` after `run_async` returns). Returning an
  `httpx.Response` after its `AsyncClient`/`ASGITransport` has closed is
  safe here because the body is already buffered — these are ordinary
  (non-streaming) `client.post`/`client.get` calls, so `.status_code` and
  `.json()` need no live connection.
- Per the brief's explicit instruction, removed every per-test
  `auth_limiter.reset()` (was inside the `_signup` helper) and
  `verify_resend_limiter.reset()` (was at the top of three tests) call,
  since `conftest.py`'s `fresh_rate_limits` now resets both before every
  test. Because those were the only uses of either limiter in the test
  file, I also dropped `auth_limiter`/`verify_resend_limiter` from the
  import line and dropped the now-unused `from app.config import settings`
  import (the brief's Step 1 snippet imports `settings` but never
  references it) — no lint config in this repo enforces this, but an
  import nobody uses is the same kind of leftover-confusion the brief itself
  argues against when telling me to delete the `.reset()` calls.
- The module's opening docstring — `"""인증 라우트. Postgres 가 필요하다
  (메일은 콘솔 어댑터로 나간다)."""` — is preserved verbatim.

## Commands run, verbatim output

### `.venv/bin/python -m pytest tests/test_verification_api.py -v`

```
============================= test session starts ==============================
platform darwin -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0 -- .../Docs_RAG/.venv/bin/python
cachedir: .pytest_cache
rootdir: /Users/seongjun/Desktop/project/Docs_RAG
plugins: anyio-4.15.0, langsmith-0.12.2
collecting ... collected 8 items

tests/test_verification_api.py::test_signup_starts_unverified PASSED     [ 12%]
tests/test_verification_api.py::test_verify_marks_the_account_and_me_reflects_it PASSED [ 25%]
tests/test_verification_api.py::test_a_second_click_says_already_verified PASSED [ 37%]
tests/test_verification_api.py::test_a_bad_token_is_a_400 PASSED         [ 50%]
tests/test_verification_api.py::test_verify_needs_no_session PASSED      [ 62%]
tests/test_verification_api.py::test_resend_requires_a_session PASSED    [ 75%]
tests/test_verification_api.py::test_resend_is_throttled PASSED          [ 87%]
tests/test_verification_api.py::test_resend_to_a_verified_account_is_a_409 PASSED [100%]

======================== 8 passed, 6 warnings in 1.37s =========================
```

(6 warnings are the pre-existing google-genai/SWIG deprecation noise seen in
every run in this repo; unrelated to this change.) Ran twice to rule out the
rate-limiter trap resurfacing under any ordering — stable both times (second
run: `8 passed, 6 warnings in 0.96s`).

### `.venv/bin/python -m pytest tests/ -q --ignore=tests/test_error_details.py`

```
144 passed, 6 warnings in 11.29s
```

Re-ran once more for stability: `144 passed, 6 warnings in 11.85s`.

**This is 144, not the stated baseline of 142 — see "Baseline discrepancy"
below.** The important fact either way: my change is purely additive. See
that section for the actual before/after measurement.

### `.venv/bin/python -m pytest tests/test_error_details.py -q`

```
.F...                                                                    [100%]
=================================== FAILURES ===================================
__________________ test_error_details_match_the_frontend_map ___________________

    def test_error_details_match_the_frontend_map():
        found = _literal_details()

        unmapped = found - EXPECTED
>       assert not unmapped, (
            "새 detail 문자열이 생겼습니다: "
            ...
        )
E       AssertionError: 새 detail 문자열이 생겼습니다: ['email already verified', 'invalid or expired token', 'verification email rate limit exceeded']
E         web/lib/api/errors.ts 에 문구를 추가하고 (또는 상태 코드로 충분하면 그렇게 결정하고) 이 파일의 EXPECTED 에도 넣어 주세요. ...

tests/test_error_details.py:161: AssertionError
=========================== short test summary info ============================
FAILED tests/test_error_details.py::test_error_details_match_the_frontend_map
1 failed, 4 passed in 0.15s
```

Exactly one failure, in `test_error_details_match_the_frontend_map`, and the
assertion names the exact three new detail strings this task introduces:
`email already verified`, `invalid or expired token`, `verification email
rate limit exceeded`. (A fourth new literal, `authentication required`, is
not in that list because it already existed — `resend_verification` reaches
it via the pre-existing `get_current_user` dependency, not new code.) **This
is what Task 7 needs to close**: add these three to `web/lib/api/errors.ts`'s
`BY_DETAIL` table and to this file's `EXPECTED`/`DETAIL_MAPPED` set. The
other four tests in the file (`test_every_detail_wrapper_is_registered`,
`test_details_are_not_shown_to_users_verbatim`,
`test_frontend_maps_every_detail_that_needs_its_own_copy`,
`test_frontend_covers_the_statuses_the_rest_rely_on`) pass — none of the new
code goes through an unregistered detail-wrapper function, and all three new
strings are plain ASCII.

## Baseline discrepancy (read before trusting "142")

The task's baseline claim was **142 passed, 0 failed** (excluding
`test_error_details.py`). Before writing any code, with a clean working tree
and Postgres confirmed reachable (direct `SELECT 1` via `SessionLocal`, and
independently by zero skips in every run's summary line), I measured:

```
.venv/bin/python -m pytest tests/ -q --ignore=tests/test_error_details.py
136 passed, 6 warnings in 11.76s
```

**136, not 142.** This continues the exact pattern Task 3's report already
flagged (brief said 139, measured 137 — a briefs-drift-from-reality issue
predating this task, not something either of us introduced). I traced this
one precisely, since the arithmetic lines up cleanly: Task 3's report
recorded **141 passed** as its own verified post-change total, running the
full suite with no `--ignore` (i.e., `test_error_details.py`'s 5 tests were
still included and green at that point, since no backend `detail` had gone
unmapped yet). `141 − 5 = 136` — exactly my pre-change, `--ignore`'d
measurement. So the 142-vs-136 gap isn't a new mystery; it's the same
already-known baseline drift, now additionally offset by the 5
`test_error_details.py` tests this task's `--ignore` flag removes from the
count that the brief's "142" didn't appear to account for.

What I can state with certainty, verified directly:
- Before my change: **136 passed, 0 failed, 0 skipped** (`--ignore
  tests/test_error_details.py`).
- After my change: **144 passed, 0 failed, 0 skipped** — exactly 136 + 8,
  the eight tests I added, with nothing else shifting either direction, and
  reproduced identically on a second run.

So regardless of which absolute baseline number is "correct," the regression
check is clean: purely additive, nothing broken.

## Deviations from the brief, and why

1. **Test execution mechanics** (`async def` + `pytest.mark.asyncio` → sync
   `def` wrapping `run_async(scenario)`, plus `_db_available`/`skipif`).
   Required by the brief's own opening warning block and the task's global
   constraints. Confirmed premise: `pytest-asyncio` is not installed in this
   venv (`ModuleNotFoundError` on direct import); `pytest 9.1.1`, `anyio
   4.15.0` are present instead.
2. **Dropped the per-test `auth_limiter.reset()` / `verify_resend_limiter.reset()`
   calls** and the now-unused `auth_limiter`, `verify_resend_limiter`,
   `settings` imports from the test file — explicitly instructed by the
   brief's warning block for the first two; the import cleanup follows the
   same reasoning (dead references invite the exact "wait, does this test
   need to reset something itself?" confusion the brief warns about).
3. **`tests/conftest.py` is not in the brief's "Files" list or its Step 8
   `git add` command, but I modified it and I'm including it in this
   commit anyway.** The brief's own warning block explicitly requires this
   change ("`tests/conftest.py` 의 `fresh_rate_limits` 튜플에
   `verify_resend_limiter` 를 추가하고 임포트도 함께 넣어라"), and the task's
   global constraints restate it as part of this task. Committing the
   router/limiter/schema changes and the new test file without this edit
   would land a test suite where `test_resend_is_throttled` and
   `test_resend_to_a_verified_account_is_a_409` pass only by luck of test
   order (first use of the fresh, never-reset `verify_resend_limiter`
   bucket in the whole run) and fail on any rerun or reorder — exactly the
   trap the warning block describes. I'm treating the Step 8 command as
   incomplete rather than authoritative here, since following it literally
   would silently drop a required, explicitly-instructed fix.
4. **Did not touch `tests/test_error_details.py`**, despite it appearing in
   the brief's top-level "Files: Modify" list. The task's global constraints
   explicitly override that: "New backend `detail` strings must NOT be added
   to `tests/test_error_details.py` in this task... exclude it... and do not
   edit it." This also matches what the brief's own Step 6 note says
   ("이 태스크에서 건드리지 않는다"), so the top-level file list and the
   brief's own body disagree with each other, and I followed the body plus
   the explicit task instruction over the file-list header.

Everything else — schema fields, the limiter line, the router bodies for
`signup`/`verify`/`resend_verification`, `_deliver_verification`, docstrings,
comments, test names/assertions/docstrings — is verbatim from the brief.

## What surprised me

- The baseline arithmetic actually resolved cleanly this time (136 = 141 − 5
  from Task 3's own recorded numbers), unlike Task 3's own unresolved
  139-vs-137 gap. Worth someone eventually reconciling why the briefs'
  stated baselines keep drifting from measured reality — it's happened on
  two tasks in a row now — but it hasn't cost either task correctness, only
  required not trusting the stated number blindly.
- Nothing in the actual wiring surprised me: `User.email_verified` was
  already a plain property reading `email_verified_at`, `UserOut` already
  had `from_attributes=True`, `get_current_user` already existed and 401s
  exactly as `resend_verification` needs, and `rate_limit_verify_resend_per_min`
  was already in `app/config.py` — Tasks 1–3 left nothing missing that this
  task's brief assumed was there.
- The `test_resend_to_a_verified_account_is_a_409` test, run alone with a
  fresh bucket, can't actually distinguish "409-checked-before-rate-limit"
  from "rate-limit-checked-first" — a single call passes the bucket (capacity
  1) either way and then hits the email-verified check. I implemented the
  order the brief specifies (and the task's "Ambiguity resolved for you"
  section requires) regardless, but flagging that this particular test
  wouldn't catch a regression in that ordering on its own.

## Commit

```
git add app/schemas.py app/services/ratelimit.py app/routers/auth.py tests/conftest.py tests/test_verification_api.py
```

(adds `tests/conftest.py` beyond the brief's literal Step 8 command — see
Deviation 3.)

## 수정: 리뷰 지적 2건

별도 세션에서, 같은 파일(`app/routers/auth.py`)에 대한 리뷰 지적 두 건을
고쳤다. 커밋 `cf7b9510c63cab4a5c692804027468953e4c3c79` (`cf7b951`), 브랜치
`feat/email-verification`. 변경 파일: `app/routers/auth.py`,
`tests/test_verification_api.py` 두 곳뿐 — 이 시점에 브랜치에서 동시에
작업 중이던 다른 에이전트들의 파일(`app/schemas.py`, `app/routers/usage.py`,
`web/**` 등)은 건드리지 않았다.

### 지적 1 (Important) — signup 이 500 을 내고 계정을 붕 띄울 수 있었다

**문제.** `signup` 은 `auth.create_user` → `verification.issue_token` →
`auth.create_session` 을 각각 독립 커밋으로 순서대로 실행했고, 셋 사이에
공유되는 실패 경계가 없었다. `create_session` 이 예외를 던지면(디비
커넥션 blip 이 현실적인 트리거 — 클라이언트 페이로드로는 유발할 수
없다) 그 예외는 FastAPI 가 백그라운드 태스크를 응답에 붙이기 전에
전파된다. 결과: user 행과 미소비 토큰은 이미 커밋돼 있고, 예약됐어야 할
인증 메일은 지연이 아니라 소멸하고, 클라이언트는 맨 500 을 받고, 같은
이메일로 재시도하면 409 "email already registered" 만 돌아온다 — 사용자는
우연히 로그인해서 재발송 버튼을 찾을 때까지 계정이 붕 뜬다.

**고침.** `create_user` 직후로 `create_session`(+`_set_session_cookie`)을
끌어올렸다. 세션이 없으면 응답에 실을 쿠키가 없고 쿠키 없는 201 은
거짓말이 되므로, 요청을 정당하게 실패시킬 수 있는 유일한 지점으로
`create_session` 을 남겨뒀다 — try/except 로 감싸지 않았다(그 상태에서도
`create_user` 는 이미 커밋돼 있어 계정 자체가 사라지진 않는다. 재시도가
409 를 보는 이 잔여 케이스까지 막으려면 재시도 루프나 두 커밋의 병합이
필요한데, 지적문이 전자를 명시적으로 금지했고 후자는 지적문이 제시한
"세션 실패만은 정당한 예외"라는 결론과 어긋난다).

그 뒤로 `verification.issue_token` + `background.add_task(_deliver_verification,
...)` 를 옮기고 `try/except Exception` 으로 감쌌다 — 실패해도 이미 만든
계정·세션·응답을 되돌리지 못한다. 실패는
`logger.exception("가입 직후 인증 토큰 발급 실패: user_id=%s email=%s", ...)`
로 남겨 진단 가능하게 했다.

이 `except` 블록에서 **`session.rollback()` 을 의도적으로 부르지
않았다.** `SessionLocal` 은 `expire_on_commit=False` 로 만들어지지만
(`app/db.py:24`), 그건 `commit()` 에만 적용되고 `rollback()` 에는 적용되지
않는다 — SQLAlchemy 소스를 직접 확인했다: 비-nested 트랜잭션 기준
`SessionTransaction._restore_snapshot` 은 `dirty_only=False` 로 불리고,
`sqlalchemy/orm/session.py:1126-1128` 이 세션 안의 **모든** 객체를 무조건
`_expire` 시킨다. `issue_token` 실패 뒤 `rollback()` 을 불렀다면, 이미
`create_user`/`create_session` 으로 커밋되어 응답으로 나갈 `user` 객체가
만료되고, `return user` 를 FastAPI/Pydantic 이 직렬화하는 순간 만료된
속성을 다시 읽으려는 지연 로딩이 `AsyncSession` 밖(그린렛 밖)에서
일어나 `MissingGreenlet` 으로 죽는다 — 500 을 없애야 할 경로에 새 500 을
심는 꼴이 된다. `issue_token` 실패 뒤에는 이 세션으로 더 할 일이 없으므로
(바로 `return user`), rollback 을 생략해도 문제없다: 다음 요청은 새
`AsyncSession` 을 받고, 이번 요청의 세션은 `get_session` 의
`async with SessionLocal() as session:` 이 응답 직렬화 **이후** 닫으면서
남은 트랜잭션을 정리한다.

성공 경로의 응답 모양·상태 코드·쿠키 동작은 바뀌지 않았다 — 최종 상태
(계정+토큰+세션+쿠키+201)는 순서를 바꾸기 전과 같고, 클라이언트가 관측할
수 있는 차이는 없다.

**수동 검증.** `verification.issue_token` 을 `unittest.mock.patch.object`
로 예외를 던지게 만들고, 실제 ASGI 앱에 `/auth/signup` 을 쳐서 확인했다
(스크래치패드에서 실행하고 버린 일회성 스크립트 — 저장소에는 없다):

```
$ PYTHONPATH=/Users/seongjun/Desktop/project/Docs_RAG .venv/bin/python verify_finding1.py
ERROR:app.routers.auth:가입 직후 인증 토큰 발급 실패: user_id=62522180-... email=finding1-...@example.com
Traceback (most recent call last):
  File ".../app/routers/auth.py", line 115, in signup
    raw = await verification.issue_token(session, user.id)
RuntimeError: simulated db blip

=== case 1: issue_token 이 예외를 던진다 ===
status: 201
body: {"id":"62522180-...","email":"finding1-...@example.com","email_verified":false,"created_at":"2026-09-10T10:27:59.782819Z"}
session cookie present: True
user row committed: 62522180-... email_verified: False
OK: create_user 성공 + issue_token 실패 => 201 + 쿠키 + 계정 행 존재

=== case 2: 같은 이메일로 재시도 => 계정이 이미 있으니 409 (스트랜딩 아님) ===
status: 409 body: {"detail":"email already registered"}

=== case 3: create_session 이 예외를 던진다 (허용된 유일한 실패) ===
propagated as exception (expected, no exception handler in test app): RuntimeError('simulated db blip')
```

`issue_token` 실패는 트레이스백까지 포함한 로그로 남고 `201` + 세션
쿠키 + 계정 행 커밋으로 끝난다(case 1). 같은 이메일 재시도는 409를 보지만
이번엔 첫 시도에서 이미 세션(로그인 상태)을 받았으므로 지적문이 말한
"스트랜딩"이 아니라 이미 성공한 가입의 중복 시도일 뿐이다(case 2).
`create_session` 실패는 지적문이 인정한 대로 그대로 전파된다(case 3).

**왜 이 케이스에 자동 회귀 테스트를 새로 추가하지 않았는지.** 지적문의
"테스트를 작성하라"는 지시는 지적 2(순서 고정)에만 명시돼 있었다.
`issue_token`/`create_session` 을 결정적으로 실패시키려면
`unittest.mock.patch.object` 로 서비스 함수를 가로채야 하는데, 이는
`tests/test_verification_api.py` 의 기존 헬퍼(`_client`, `_signup`,
`_token_for`)와는 다른 새 패턴이라 "기존 헬퍼를 재사용하고 새로 만들지
말라"는 제약과 결이 달랐다. 위 수동 검증으로 실제 동작은 직접 확인했다 —
필요하면 이 스크립트를 정식 회귀 테스트로 승격하는 건 어렵지 않다.

### 지적 2 (Important) — 순서를 지키는 테스트가 없었다

**문제.** `resend_verification` 은 `user.email_verified` 를 먼저 체크해
409 를 내고, 그 다음에 `verify_resend_limiter.allow(...)` 로 429 를
낸다. 기존 `test_resend_to_a_verified_account_is_a_409` 는 단 한 번만
호출한다 — `rate_limit_verify_resend_per_min = 1` 이라 빈 버킷은 순서와
무관하게 첫 호출에서 항상 `True` 를 반환하므로, 두 체크의 순서를 통째로
바꿔도 이 테스트는 똑같이 통과한다. 이 사실은 애초에 이 태스크의 원본
보고서(위 "What surprised me" 세 번째 항목)에서도 이미 지적한 바 있다 —
이번에 그 구멍을 실제로 메웠다.

**고침.** 프로덕션 코드(`resend_verification`)의 순서는 이미 올발랐다 —
`user.email_verified` 체크가 `verify_resend_limiter.allow(...)` 보다
앞에 있었다. 바뀐 건 테스트뿐이다. `tests/test_verification_api.py` 에
`test_resend_to_a_verified_account_never_burns_the_rate_limit` 을
추가했다: 계정을 검증한 뒤 `/auth/resend-verification` 을 연속 두 번
불러 **둘 다** 409 인지 확인한다. 리미터 용량이 분당 1 이므로, 순서가
뒤집혀 리미터가 먼저 소비됐다면 두 번째 호출은 429 로 샌다 — 그래서
"두 번째 호출도 여전히 409" 라는 사실 자체가 리미터를 전혀 축내지
않았다는 증거가 된다.

**역방향 순서로 실제로 실패하는지 확인(요청받은 대로).**
`app/routers/auth.py` 의 `resend_verification` 안에서
`if user.email_verified: raise 409` 블록과
`if not verify_resend_limiter.allow(...): raise 429` 블록을 잠깐
맞바꾸고, resend 관련 테스트만 돌렸다:

```
$ .venv/bin/python -m pytest tests/test_verification_api.py -v -k "resend"
tests/test_verification_api.py::test_resend_is_throttled PASSED          [ 50%]
tests/test_verification_api.py::test_resend_to_a_verified_account_is_a_409 PASSED [ 75%]
tests/test_verification_api.py::test_resend_to_a_verified_account_never_burns_the_rate_limit FAILED [100%]

...
>           assert response.status_code == 409, response.text
E           AssertionError: {"detail":"verification email rate limit exceeded"}
E           assert 429 == 409
E            +  where 429 = <Response [429 Too Many Requests]>.status_code

tests/test_verification_api.py:216: AssertionError
============ 1 failed, 3 passed, 8 deselected, 6 warnings in 0.80s =============
```

주목할 점: 기존 `test_resend_to_a_verified_account_is_a_409` 는 순서가
뒤집힌 상태에서도 **그대로 PASSED** 다 — 지적문이 예측한 그대로, 그
테스트는 이 회귀를 잡지 못한다. 새 테스트만 정확히 두 번째 호출에서
429 로 새는 것을 잡아 FAILED 됐다. 이걸로 새 테스트가 실제로 순서를
고정한다는 걸 확인한 뒤, 두 블록을 원래 순서로 되돌렸다. 복구는
`git diff` 로 원본과 완전히 같아졌음을 확인했고(그 구간에 diff 없음),
재실행하면 다시 12 개 전부 PASSED 다.

### 커맨드와 출력 (이번 수정 전/후, 내가 직접 잰 것)

변경 전(작업 시작 시점, working tree clean 확인 후):

```
$ .venv/bin/python -m pytest tests/test_verification_api.py -v
...
======================== 11 passed, 6 warnings in 1.29s ========================

$ .venv/bin/python -m pytest tests/ -q --ignore=tests/test_error_details.py
...
155 passed, 6 warnings in 14.28s
```

변경 후(커밋 직전 마지막 실행):

```
$ .venv/bin/python -m pytest tests/test_verification_api.py -v
...
tests/test_verification_api.py::test_resend_to_a_verified_account_is_a_409 PASSED [ 66%]
tests/test_verification_api.py::test_resend_to_a_verified_account_never_burns_the_rate_limit PASSED [ 75%]
...
======================== 12 passed, 6 warnings in 1.21s ========================

$ .venv/bin/python -m pytest tests/ -q --ignore=tests/test_error_details.py
...
155 passed, 6 warnings in 11.79s
```

전체 스위트 총량이 155 → 155 로 그대로인 건 우연한 착시다 — 이 작업을
진행하는 동안 같은 브랜치에 다른 에이전트들이 계속 커밋하고 있었다
(`git log` 타임스탬프 기준 이 세션 동안에도 `988942a`부터 `0816fdf`까지
다수 커밋이 새로 얹혔고, `web/`, `usage`, 새 `test_unverified_gate.py`/
`test_usage_unverified.py` 등을 건드렸다). 즉 "전/후 155" 두 숫자는 서로
다른 브랜치 스냅샷에서 잰 것이라 그 자체로는 아무것도 증명하지 않는다.
내 변경이 안전하다는 근거는 총량 일치가 아니라: (1) 커밋 직전
`git status`/`git diff` 로 스테이징된 두 파일 각각의 diff 가 정확히
위에서 설명한 내용뿐임을 확인했고, (2) 마지막 실행에서 실패 0 이었다는
것이다.

### 커밋

```
git add app/routers/auth.py tests/test_verification_api.py
git commit -m "fix(auth): signup 후반부 실패가 계정을 500으로 되돌리던 문제 두 가지" ...
```

SHA: `cf7b9510c63cab4a5c692804027468953e4c3c79` (`cf7b951`), 브랜치
`feat/email-verification`. 스테이징한 파일은 정확히 `app/routers/auth.py`
와 `tests/test_verification_api.py` 뿐 — `git add -A`/`git add .` 는 쓰지
않았다.
