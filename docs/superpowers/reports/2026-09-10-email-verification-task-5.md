# Task 5 report — 게이트: 미인증 맛보기 쿼터

## Status: DONE

Commit: `59b07f324da80da7fb6e9f335a15cd3fd6b35ff5` (short `59b07f3`), on branch
`feat/email-verification`, on top of `b2f58de` (a concurrent `web/`-only
commit from another agent working the same branch).

## Files changed

### `app/services/usage.py`

Added four functions verbatim from the brief's Step 3 block, inserted
between `pages_this_month` and `query_quota_exceeded` as instructed:

- `queries_total(session, user_id) -> int` — lifetime count of `kind="query"`
  rows, no time window.
- `documents_total(session, user_id) -> int` — lifetime count of
  `kind="upload"` rows (not `ingest`, not a join against `documents`).
- `unverified_query_exceeded` / `unverified_upload_exceeded` — `>=` compare
  against `settings.unverified_quota_queries` / `settings.unverified_quota_documents`,
  each returning `False` outright when its setting is `<= 0`. Neither checks
  `email_verified` itself — per the brief's own comment, "호출자가 인증
  여부를 먼저 확인한다."

`settings.unverified_quota_queries`/`unverified_quota_documents` and
`User.email_verified` already existed from Task 1, exactly as the brief's
"Consumes" line claims — nothing to add in `app/config.py` or `app/models.py`.

### `app/deps.py`

Added `VERIFICATION_REQUIRED = HTTPException(status_code=403, detail="email
verification required")` right after `_UNAUTHENTICATED`, with the brief's
403-not-429 comment verbatim.

### `app/services/pipeline.py`

Added `from app.deps import VERIFICATION_REQUIRED` (verified this does not
create an import cycle: `app.deps` only reaches `app.config`, `app.db`,
`app.models`, `app.services.auth`, none of which import `pipeline`; confirmed
with a direct `import app.services.pipeline` after the edit). Changed
`QueryRunner.enforce_limits` exactly as the brief's Step 5 shows: after the
existing rate-limit check, unverified accounts go through
`usage.unverified_query_exceeded` and raise `VERIFICATION_REQUIRED`; verified
accounts keep the existing `query_quota_exceeded` → `too_many(...)` path
unchanged.

Because both `POST /query` and `POST /conversations/{id}/query` construct a
`QueryRunner` and call `enforce_limits` (confirmed
`app/routers/conversations.py:181`), this one change gates both query
surfaces — exactly what the pipeline module's own docstring promises
("두 엔드포인트가... 드리프트하지 않도록").

### `app/routers/documents.py`

Extended the existing `from app.deps import get_current_user` to also import
`VERIFICATION_REQUIRED` (no second import line, per the task's context note).
Added the upload gate immediately after the rate-limit block and before
`data = await file.read()`, and the `"upload"` acceptance event immediately
after `await session.refresh(doc)` and before the file is written to disk —
both at the exact line positions and with the exact comments the brief's
Step 6 specifies.

### `scripts/cost_report.py`

Added `UsageEvent.kind != "upload"` to the first `select(...).where(...)` in
`collect()`, per Step 7, so upload-acceptance rows (always 0 tokens) don't
inflate the "요청 N건" count or skew the cache-hit ratio. Did not touch the
second (`by_user`) query — the brief only names "첫 select", and upload rows
contribute 0 to every summed column there regardless, so leaving it alone is
behaviorally identical, just consistent with what was actually asked.

### `tests/test_unverified_gate.py` (new)

Same test names, assertions, and Korean docstrings as the brief's Step 1
block, verbatim. Per the brief's own warning block and the task's global
constraints: `pytestmark = pytest.mark.asyncio` → `pytest.mark.skipif(not
_db_available(), reason="Postgres not reachable")`; `run_async`/`_db_available`
copied verbatim from `tests/test_verification.py`; every `async def test_*`
became a synchronous `def test_*` wrapping a local `async def scenario()`
run via `run_async(scenario)`. The one exception is `test_zero_disables_the_gate`,
which mutates `settings.unverified_quota_queries` around the `run_async`
call inside its own `try/finally` (matching the brief's structure, just with
the coroutine boundary moved).

### `tests/test_verification_api.py`

Appended the brief's Step 10 tests (converted to the same sync `run_async`
convention as the rest of the file), after the existing 8 tests — did not
touch anything already there. Added two things below the existing imports,
per the brief's "파일 상단 import 아래에 픽스처를 둔다": `from app.services.ratelimit
import auth_limiter, upload_limiter, verify_resend_limiter` and `BROKEN_PDF
= b"%PDF-1.4 not actually a pdf"`. Also added `from app.config import
settings` to the top-level import block — required because the new tests
reference `settings.unverified_quota_queries` at module-call time, and the
brief's own snippet assumes it's in scope. `auth_limiter` and
`verify_resend_limiter` end up imported but unused (only `upload_limiter.reset()`
is called) — kept as the brief specifies since this repo has no lint step in
CI (`.github/workflows/ci.yml` runs only `pytest` for the backend job) and
the task said to keep the brief's fixtures.

### `tests/test_phase2.py` (not in the brief's file list — see Deviations)

Added an explicit verify step (`verification.issue_token` +
`verification.consume_token`) to `test_quota_exceeded_returns_429`, before
it records the two "at the daily allowance" query events. See Deviation 3.

## Commands run, verbatim output

### `.venv/bin/python -m pytest tests/test_unverified_gate.py -v`

```
============================= test session starts ==============================
platform darwin -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0 -- .../Docs_RAG/.venv/bin/python
cachedir: .pytest_cache
rootdir: /Users/seongjun/Desktop/project/Docs_RAG
plugins: anyio-4.15.0, langsmith-0.12.2
collecting ... collected 6 items

tests/test_unverified_gate.py::test_query_gate_opens_and_then_closes PASSED [ 16%]
tests/test_unverified_gate.py::test_the_query_window_is_lifetime_not_today PASSED [ 33%]
tests/test_unverified_gate.py::test_upload_gate_counts_accepted_uploads_not_surviving_documents PASSED [ 50%]
tests/test_unverified_gate.py::test_indexing_events_do_not_count_toward_the_upload_gate PASSED [ 66%]
tests/test_unverified_gate.py::test_a_verified_account_is_not_gated PASSED [ 83%]
tests/test_unverified_gate.py::test_zero_disables_the_gate PASSED        [100%]

============================== 6 passed in 0.63s ===============================
```

**6 passed, not the brief's stated "Expected: 5 passed" (Step 9).** This is a
discrepancy in the brief itself, not a bug: its own Step 1 code block defines
six `async def test_*` functions (`test_query_gate_opens_and_then_closes`,
`test_the_query_window_is_lifetime_not_today`,
`test_upload_gate_counts_accepted_uploads_not_surviving_documents`,
`test_indexing_events_do_not_count_toward_the_upload_gate`,
`test_a_verified_account_is_not_gated`, `test_zero_disables_the_gate`) —
Step 9's count is simply off by one against the brief's own code. 6 passing
is correct and matches every test the brief actually wrote.

### `.venv/bin/python -m pytest tests/test_verification_api.py -v`

```
============================= test session starts ==============================
platform darwin -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0 -- .../Docs_RAG/.venv/bin/python
cachedir: .pytest_cache
rootdir: /Users/seongjun/Desktop/project/Docs_RAG
plugins: anyio-4.15.0, langsmith-0.12.2
collecting ... collected 11 items

tests/test_verification_api.py::test_signup_starts_unverified PASSED     [  9%]
tests/test_verification_api.py::test_verify_marks_the_account_and_me_reflects_it PASSED [ 18%]
tests/test_verification_api.py::test_a_second_click_says_already_verified PASSED [ 27%]
tests/test_verification_api.py::test_a_bad_token_is_a_400 PASSED         [ 36%]
tests/test_verification_api.py::test_verify_needs_no_session PASSED      [ 45%]
tests/test_verification_api.py::test_resend_requires_a_session PASSED    [ 54%]
tests/test_verification_api.py::test_resend_is_throttled PASSED          [ 63%]
tests/test_verification_api.py::test_resend_to_a_verified_account_is_a_409 PASSED [ 72%]
tests/test_verification_api.py::test_the_sixth_question_is_refused_until_verified PASSED [ 81%]
tests/test_verification_api.py::test_verifying_restores_the_normal_quota PASSED [ 90%]
tests/test_verification_api.py::test_the_second_upload_is_refused_and_deleting_does_not_reopen_it PASSED [100%]

======================== 11 passed, 6 warnings in 1.14s ========================
```

Matches the brief's Step 10 "Expected: 11 passed" exactly (8 pre-existing +
3 new). The 6 warnings are the pre-existing google-genai/SWIG deprecation
noise present in every run in this repo, unrelated to this change.

### Baseline vs. after — `.venv/bin/python -m pytest tests/ -q --ignore=tests/test_error_details.py`

I measured the baseline myself rather than trusting a number, per the task
instructions. Since I had already made Task 5's edits by the time I ran this,
I used `git stash push -u` to put the working tree back to the exact
pre-Task-5 commit (`b2f58de`), ran the suite, then `git stash pop` to restore
my changes (confirmed identical working tree afterward via `git status`).

**Baseline, at `b2f58de`, before any Task 5 change:**
```
144 passed, 6 warnings in 12.47s
```
0 failed, 0 skipped (Postgres reachable).

**After Task 5's changes (including the `tests/test_phase2.py` fix):**
```
........................................................................ [ 47%]
........................................................................ [ 94%]
.........                                                                [100%]
153 passed, 6 warnings in 14.39s
```
0 failed, 0 skipped. `153 = 144 + 9` — exactly the 6 new tests in
`test_unverified_gate.py` plus the 3 new tests appended to
`test_verification_api.py`, nothing else shifted. This baseline also happens
to match Task 4's report, which recorded its own post-change count as 144 —
consistent, no drift this time.

**Before landing on 153, one regression surfaced and was fixed** (see
Deviation 3): immediately after making the pipeline/documents changes and
before touching `test_phase2.py`, the same full-suite command reported `1
failed, 152 passed`, failing
`tests/test_phase2.py::test_quota_exceeded_returns_429` with `assert 503 ==
429`.

### Informational only (not part of the required verification, not committed to)

`.venv/bin/python -m pytest tests/test_cost_report.py -v` → `6 passed in
0.11s`, confirming the Step 7 regression check the brief calls out (this
test builds its aggregate dict by hand and never touches the query, so my
`.where()` change can't be seen by it — a pass here is expected either way).

`.venv/bin/python -m pytest tests/test_error_details.py -q` (run only to
confirm I did not newly break something already broken, never staged or
edited): `2 failed, 3 passed`. Both failures list `'email verification
required'` alongside three detail strings from Task 4
(`'email already verified'`, `'invalid or expired token'`,
`'verification email rate limit exceeded'`) as either "새 detail 문자열이
생겼습니다" or already present in the frontend's map but not yet in this
file's `EXPECTED`/`DETAIL_MAPPED`. This confirms the task's own framing —
the file "is currently red on purpose and a later task closes it" — and that
my one new literal detail string lands in the same, already-broken bucket
rather than creating a new failure mode.

## Deviations from the brief, and why

1. **Test execution mechanics** (`async def` + `pytest.mark.asyncio` → sync
   `def` wrapping `run_async(scenario)`, plus `_db_available`/`skipif`).
   Required by the brief's own opening warning block and the task's global
   constraints; confirmed `pytest-asyncio` is genuinely absent (only
   `pytest`, `anyio`, and — per the task instructions — `nest-asyncio` are
   installed).

2. **Brief's Step 9 expected count (5) doesn't match its own Step 1 code
   (6 test functions).** Reported 6 passed, matching the code as written —
   see the test-output section above for the itemized list.

3. **Fixed `tests/test_phase2.py::test_quota_exceeded_returns_429`, a file
   outside the brief's "Files" list.** The brief's Step 11 says to expect
   "기존 테스트 전부 통과. 기존 계정은 전부 verified 이므로 게이트에 걸리지
   않는다" — but that "existing accounts are all verified" claim is only true
   for accounts that existed *before* the Task 1 migration (backfilled, per
   that migration's own test in `tests/test_verification.py`). It does not
   hold for an account this test signs up fresh, mid-run, via `/auth/signup`
   — that account starts unverified, same as every other signup since Task
   3. Before this task, that didn't matter: `enforce_limits` never looked at
   `email_verified`. After this task's Step 5 change, a fresh unverified
   signup with only 2 recorded `"query"` events (well under the default
   `unverified_quota_queries=5`) sails past the new gate, then fails for an
   unrelated reason (no embedding server in the test environment → 503)
   instead of ever reaching the daily-quota check the test means to exercise
   → the test asserted `429` and got `503`. I fixed it at the source: verify
   the account (`verification.issue_token` + `verification.consume_token`,
   the same pattern `tests/test_verification.py` uses to verify without a
   real mailbox) before recording the quota-exhausting events, so the test
   goes on to exercise exactly what it always meant to — the verified
   account's daily quota. This is a two-line, narrowly-scoped fix; I staged
   it explicitly by path alongside the brief's files rather than silently
   folding it in, and I'm flagging it here and in my summary since it's
   outside the brief's stated scope. I did not find any other test with the
   same fresh-signup-then-assume-normal-quota shape — the full-suite run
   surfaced exactly one failure both before and after isolating the cause.

4. **Did not modify `tests/test_error_details.py`**, despite it appearing in
   the brief's top-level "Files: Modify" list. The task's own instructions
   explicitly override that file list ("Do not edit `tests/test_error_details.py`.
   It is currently red on purpose and a later task closes it"), and I
   followed the override, excluding the file from every suite run except one
   informational, uncommitted check (see above).

5. **Added `from app.config import settings` to `tests/test_verification_api.py`'s
   top-level imports**, not shown in the brief's Step 10 snippet in isolation
   but required for `settings.unverified_quota_queries` to resolve — the
   brief's own test bodies reference it unqualified.

Everything else — function bodies, comments, docstrings, exact line
placement, the commit message's substance — is verbatim or a direct
translation of the brief's own code blocks.

## What surprised me

- The baseline arithmetic came out clean on the first try (144 measured now
  matches 144 recorded as Task 4's own post-change total) — no drift this
  time, unlike the pattern flagged in Tasks 3 and 4's reports.
- The `test_phase2.py` interaction was the only real surprise: it's a direct,
  mechanical consequence of Step 5 exactly as specified, not a mistake in my
  implementation, but the brief's Step 11 "expected" note doesn't anticipate
  it because it only reasons about pre-existing accounts, not accounts the
  test suite itself signs up fresh.
- Everything else wired up exactly as the brief predicted: `settings.unverified_quota_queries`/
  `unverified_quota_documents` and `User.email_verified` were already in
  place from Task 1, `app/routers/documents.py` already reached usage
  helpers as `ingest.usage.<fn>`, and importing `app.deps` from
  `app.services.pipeline` introduced no import cycle.

## Commit

```
git add app/services/usage.py app/deps.py app/services/pipeline.py app/routers/documents.py scripts/cost_report.py tests/test_unverified_gate.py tests/test_verification_api.py tests/test_phase2.py
git commit  # 59b07f324da80da7fb6e9f335a15cd3fd6b35ff5
```

(adds `tests/test_phase2.py` beyond the brief's literal Step 12 command —
see Deviation 3. No other files were staged; `git status` was clean and
showed only these eight paths — seven modified, one new — before staging,
and confirmed clean again immediately after the commit.)
