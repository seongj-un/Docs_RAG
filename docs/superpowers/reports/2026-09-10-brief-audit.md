# Brief audit — Tasks 5, 6, 8 (feat/email-verification)

Audited against the working tree as read during this session. Note: Tasks 5, 7
and 8 all had live, uncommitted or freshly-committed implementation activity
happening in the same working directory *while this audit ran* (Task 7 landed
as commit `b2f58de` mid-audit; Task 5's files and Task 8's `AppShell.tsx`/
`settings/page.tsx` picked up uncommitted edits mid-audit). Where that mattered
it's called out explicitly; the analysis below is otherwise based on the brief
text itself, which does not change.

---

## Task 5 — unverified-account gate

### Critical

**C5-1. The brief's own code blocks reproduce already-fixed defect #1 (`pytest.mark.asyncio` / bare `async def test_*`).**

The brief's top banner (lines 1-19) explicitly warns: *"이 저장소에는
`pytest-asyncio` 가 없다... 아래에 보이는 `pytestmark = pytest.mark.asyncio`
와 `async def test_*` 를 그대로 쓰면 죽는다... 실행 방식만 바꾼다."* Despite
that, the actual code shown is written exactly in the banned style:

- Step 1 (`tests/test_unverified_gate.py`, line 74): `pytestmark = pytest.mark.asyncio`, followed by seven `async def test_*(): ...` functions (lines 101-183) with no `run_async`/sync wrapper.
- Step 10 (appended to `tests/test_verification_api.py`, lines 344-424): `async def test_the_sixth_question_is_refused_until_verified():` and `async def test_verifying_restores_the_normal_quota():`, again bare, and `async def test_the_second_upload_is_refused_and_deleting_does_not_reopen_it():` (line 396) — none wrapped.

This is not hypothetical: `tests/test_verification_api.py`'s *existing* 8 tests
(read from the real file) all use the sync `def test_*(): async def
scenario(): ...; run_async(scenario)` idiom, and `tests/test_verification.py`
/ `tests/test_isolation.py` (the files the banner names as canonical) do the
same. The banner tells the implementer to translate but the sample code was
never updated to match, so a literal copy — or a careless skim past the
banner — reproduces defect #1 exactly (pytest either errors with "async def
functions are not natively supported" or silently skips/warns without running
the body, depending on pytest version).

**Fix:** rewrite both code blocks in the sync + `run_async`/`_db_available`
idiom already used by every other DB-touching test file in this suite. Test
names, assertions and docstrings can stay verbatim per the banner's own
instruction — only the wrapper changes.

**C5-2. Step 10's new tests use `settings` without importing it — `NameError` at test run time, independent of C5-1.**

`tests/test_verification_api.py`'s current imports are:
```python
import asyncio
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from app.db import SessionLocal, engine
from app.main import app
from app.services import verification
```
No `from app.config import settings`. Step 10's two new tests both reference
`settings.unverified_quota_queries` (brief lines ~353 and ~372):
```python
        async with SessionLocal() as session:
            for _ in range(settings.unverified_quota_queries):
                await usage.record(session, user_id, "query")
```
and the "add below the imports" fixture block (brief lines 389-393) only adds
`from app.services.ratelimit import auth_limiter, upload_limiter,
verify_resend_limiter` and `BROKEN_PDF = b"..."` — not `settings`. As written
this raises `NameError: name 'settings' is not defined` the first time either
test body runs, even after C5-1 is fixed.

**Fix:** add `from app.config import settings` to the Step 10 import block.

**C5-3. `tests/test_error_details.py` is listed as a file this task modifies, but no step touches it, and Step 11 explicitly excludes it — so the new detail string this task adds is never registered, and the suite is left permanently red.**

Files section (line 28): `Modify: tests/test_error_details.py`. Step 4 adds
`VERIFICATION_REQUIRED` to `app/deps.py` with `detail="email verification
required"` — a brand-new literal `detail=` string that `test_error_details.py`'s
AST scanner (`_literal_details()`) will pick up from any `.py` file under
`app/`, whether or not the containing call is inside a function. But no Step
in this brief edits `DETAIL_MAPPED`, and Step 11's regression command is:

`.venv/bin/python -m pytest tests/ -v --ignore=tests/test_error_details.py`

i.e. the one file that would catch the gap is excluded from this task's own
verification.

I confirmed this is real, not theoretical, by running the test directly (it's
infra-free per its own docstring — no DB, safe to run):

```
tests/test_error_details.py::test_error_details_match_the_frontend_map FAILED
tests/test_error_details.py::test_frontend_maps_every_detail_that_needs_its_own_copy FAILED
2 failed, 3 passed
AssertionError: 새 detail 문자열이 생겼습니다: ['email already verified',
'email verification required', 'invalid or expired token',
'verification email rate limit exceeded']
```

Root cause, traced through git history: plan commit `75e6046` ("에러 카피
대조 테스트의 태스크 경계 정리") deliberately moved the `DETAIL_MAPPED` update
out of Task 5 and into Task 7, reasoning that the bidirectional check can only
go green when backend and frontend land together. Task 7 (`task-7-brief.md`
Step 7) does contain the instruction to add all 4 lines to `DETAIL_MAPPED`.
But Task 7 has already been executed (commit `b2f58de`) and its own report
records an explicit deviation: *"tests/test_error_details.py 는 건드리지
않았다 — 백엔드 detail 네 개 중 하나가 아직 다른 작업에서 진행 중이라 지금
넣으면 양방향 대조가 잘못된 이유로 깨진다."* — i.e. Task 7 punted back to
whichever task lands the 4th string (Task 5), because Task 5 and Task 7 were
dispatched concurrently rather than strictly sequentially. Task 5's brief
still carries the stale `Modify: tests/test_error_details.py` line from
*before* the 75e6046 reorg but was never given the steps back. Task 6 doesn't
touch the file either, and neither does Task 8.

Net effect: as the four briefs (5, 6, 7-done, 8) are currently written, nobody
ever adds the 4 entries to `DETAIL_MAPPED`. This directly breaks Task 8's own
Step 10 (`pytest tests/ -v && npm test`, no `--ignore`, "Expected: 양쪽 전부
통과") and Task 8's completion criterion #9 ("`pytest`와 `npm test`가 전부
통과한다") — see Task 8 finding C8-1.

**Fix:** add a step to Task 5 (matching Task 7's already-written Step 7
content) that adds all 4 literals to `DETAIL_MAPPED` in
`tests/test_error_details.py`, and drop `--ignore=tests/test_error_details.py`
from Step 11 once that's in. (Alternatively, explicitly hand this to Task 8
and update Task 8's Files list — either way, some brief needs to own it.)

### Important

**I5-1. Files header cites `app/services/pipeline.py:105-112`; the real `enforce_limits` method is at lines 106-112.** Off by one (105 is a blank line before the method). Harmless in practice since the step's prose anchors on the method body, not the number — flagged because the audit asked to note stale line numbers.

**I5-2. Files header cites `app/routers/documents.py:100-113`; that range is the pre-existing page-count/monthly-quota block, not where either of this task's edits land.** The verification gate (Step 6, first snippet) belongs between the rate-limit 429 block and `data = await file.read()` — around line 90-93 in the current file. The "upload" usage-record call (Step 6, second snippet) belongs between `await session.refresh(doc)` and `path = _storage_path(...)` — around line 122-124. The prose instructions ("바로 뒤" / "바로 앞") are precise and correct; the header range is stale/misleading if anyone trusts it over the prose.

**I5-3. The import instruction for `app/routers/documents.py` reads as "add a new import line" but the name it adds is already partly imported.** Brief: *"app/routers/documents.py의 import에 추가한다: `from app.deps import VERIFICATION_REQUIRED, get_current_user`"*. The file already has `from app.deps import get_current_user` (line 18). Taken literally as "add," this produces two `from app.deps import ...` statements both naming `get_current_user` in the same file — not a runtime error, but a redefinition a linter (ruff/flake8 F811) would flag. Should read "change the existing import to."

### Minor

**M5-1. Step numbering skips from Step 7 to Step 9 (no Step 8).** Cosmetic, but corroborates C5-3 — consistent with a "Step 8: DETAIL_MAPPED 갱신" having been deleted from this task during the 75e6046 reorg without renumbering the rest.

**M5-2. Step 10's added fixture imports `auth_limiter` and `verify_resend_limiter` but neither is used** by the three new tests in that step (only `upload_limiter.reset()` is called) — unused imports. Also, `tests/conftest.py`'s autouse `fresh_rate_limits` fixture already resets all four limiters before every test, so the explicit `upload_limiter.reset()` is redundant (harmless, just noise).

### Confirmed correct (the two things this audit was asked to weigh most)

- **Lifetime-cumulative, not resettable by deletion — holds.** `queries_total`/`documents_total` (Step 3) filter only on `user_id` + `kind`, with no `created_at` window at all. `UsageEvent.user_id` foreign-keys to `users` (`ondelete="CASCADE"`), not to `documents` — there is no FK from `usage_events` to `documents`, so deleting a `Document` row cannot cascade-delete or otherwise affect any `UsageEvent` row. `enforce_limits` (the query gate) is shared by both `POST /query` and `POST /conversations/{id}/query` (`app/routers/conversations.py:181` also calls `runner.enforce_limits(...)`), so neither endpoint nor document deletion can leak past the gate.
- The brief's own raw-SQL test snippet correctly avoids defect #2, binding `uid=user.id` (a UUID object) via `.bindparams(...)` rather than `str(user.id)`.
- `UsageEvent.kind` is a plain `Text` column with no CHECK constraint or enum in migration `0004_usage_and_cache.py` — adding the new `"upload"` kind needs no migration.
- Cross-task check on `UsageEvent.kind`/`usage_events` consumers requested by the audit: `app/services/usage.py` (all queries kind-filtered, unaffected by a new kind), `scripts/cost_report.py` (Step 7 correctly excludes `kind != "upload"` from the request-count aggregate that would otherwise inflate "요청 N건"; the by-user token-sum query is unaffected since upload events carry 0 tokens either way), `eval/` (no references to `UsageEvent`/`usage_events` at all — grepped, empty), `app/routers/admin.py` (one docstring mention, no actual query). No hidden breakage from the new kind.

---

## Task 6 — `GET /usage` unverified limits

### Critical

**C6-1. Same defect-#1 recurrence as Task 5.** Step 1's `tests/test_usage_unverified.py` (lines 34-81) uses `pytestmark = pytest.mark.asyncio` (line 48) and a bare `async def test_unverified_usage_reports_the_taster_limits():` (line 51), under the identical top-of-file warning banner as Task 5 telling the implementer not to do this. Same failure mode, same fix: rewrite as sync `def test_*(): async def scenario(): ...; run_async(scenario)` with `pytestmark = pytest.mark.skipif(not _db_available(), ...)`.

### Important

None beyond what's already covered under Task 5/8's cross-task findings.

### Minor

**M6-1. `auth_limiter.reset()` at the top of the one test is redundant** — `tests/conftest.py`'s autouse `fresh_rate_limits` fixture already resets it (and 3 other limiters) before every test. Harmless.

### Confirmed correct

- `UsageOut`'s 5 new fields (`email_verified`, `queries_total`, `documents_total`, `unverified_query_limit`, `unverified_document_limit`) and the Step 4 router wiring call `usage_service.queries_total`/`documents_total` with exactly the signatures Task 5 produces (`(session, user_id) -> int`).
- Cross-task check requested by the audit: Task 6's `UsageOut` matches Task 8's consumption field-for-field. `web/lib/api/types.ts`'s `Usage` type (already landed for real via Task 7, commit `b2f58de`) has exactly the same 9 fields with the same names (`queries_today, queries_per_day, pages_this_month, pages_per_month, email_verified, queries_total, documents_total, unverified_query_limit, unverified_document_limit`), and Task 8's settings-page edit reads exactly those names. No drift.
- `UsageOut(` is constructed in exactly one place in the whole repo (`app/routers/usage.py`, the file this task also edits) — grepped `app/`, `tests/`, `scripts/`, `eval/`. Widening it with 5 new required fields breaks no other caller.

---

## Task 8 — frontend: verify page, banner, session refresh, usage bars

### Critical

**C8-1. Step 10's full-suite command and completion-criterion #9 will not pass, because of the gap identified in C5-3.** Step 10: `.venv/bin/python -m pytest tests/ -v && cd web && npm test` — "Expected: 양쪽 전부 통과", and 완료 기준 #9: *"`RESEND_API_KEY` 없이 `pytest`와 `npm test`가 전부 통과한다."* Neither Task 5, 6, 7 (already landed) nor Task 8 itself ever adds the 4 new detail strings to `tests/test_error_details.py`'s `DETAIL_MAPPED` (see C5-3), and I confirmed by direct run that this file currently fails 2 of 5 tests. Task 8 is the last task and the one whose acceptance criteria actually exercise the full, un-ignored suite, so this is where the gap becomes visible as a broken promise even though the root cause is upstream. Not a bug in Task 8's own code — but the brief's own success condition is false as written, unless the DETAIL_MAPPED fix lands somewhere before Step 10 runs.

### Important

**I8-1. The unverified-state usage bar hardcodes the post-verification quota numbers as copy text instead of reading them off the same `usage` object.** Step 5's replacement (in `web/app/(app)/settings/page.tsx`):
```tsx
<UsageBar
  label="지금까지 한 질문"
  used={usage.queries_total}
  limit={usage.unverified_query_limit}
  unit="개"
  refill="이메일을 확인하면 하루 200개로 늘어납니다."
/>
...
refill="이메일을 확인하면 한 달 1000쪽으로 늘어납니다."
```
"200" and "1000" are `settings.quota_queries_per_day` / `settings.quota_upload_pages_per_month` — both already present on the very same `usage` object two lines above (as `usage.queries_per_day` / `usage.pages_per_month`, used in the verified branch immediately preceding this one). If either setting is ever configured away from its default (both are ordinary env-configurable settings in `app/config.py`), this banner silently shows the wrong number — no type error, no test catches it — which is exactly the "silently do the wrong thing" failure mode the audit asked to watch for, and it directly contradicts `UsageOut`'s own stated design rationale ("caller can phrase the remainder itself... instead of receiving a number it cannot explain"). **Fix:** interpolate `usage.queries_per_day` / `usage.pages_per_month` into the refill strings instead of the literals.

### Minor

**M8-1. Task 8's Interfaces section doesn't name Task 6 as a dependency** ("Consumes: Task 7의 auth.verify, auth.resendVerification, User.email_verified, Usage의 새 필드" — no explicit "Task 6"), unlike Task 6's own brief which does name its Task-5 dependency explicitly. Task 8's settings-page code and its Step 8 manual-QA walkthrough both silently require Task 6's backend fields to exist; worth naming for clarity, not a functional defect since Task 6 precedes Task 8 in the dispatch order regardless.

**M8-2. No automated test exercises any of this task's new logic.** `web/package.json`'s `test` script (`vitest run`) only picks up the 3 pre-existing files (`errors.test.ts`, `footnotes.test.ts`, `sse.test.ts`); none reference `VerifyBanner.tsx`, `verify/page.tsx`, or `session.tsx`'s new `refresh`. Step 7's "Expected: 전부 통과" is therefore true regardless of whether the new code works — it only re-confirms pre-existing behavior. This matches the rest of the codebase's convention (no page/component anywhere in `web/` has a rendering test, e.g. `login/page.tsx` has none either), so it isn't a regression Task 8 introduces, but it's worth naming since Step 7 reads as if it provides coverage it doesn't.

### Confirmed correct (the routing/provider question the audit asked to check specifically)

- Read `web/app/layout.tsx` and `web/app/(app)/layout.tsx` directly. `web/app/verify/page.tsx` is a sibling of the `(app)` route group, not nested inside it, so Next's route-group nesting means it is wrapped only by the **root** layout (`<ThemeProvider><SessionProvider>{children}</SessionProvider></ThemeProvider>`) and **not** by `(app)/layout.tsx`'s `useEffect(() => { if (!loading && user === null) router.replace("/login"); })` auth gate. A logged-out visitor in a different browser hitting `/verify?token=...` gets `SessionProvider` (so `useSession()` works) but no redirect to `/login` — matching the brief's own stated reasoning verbatim.
- `SessionValue`'s new `refresh` field: `useCallback` is already imported in `web/lib/session.tsx` (no missing import); both insertion anchors ("`signOut` 위", the `Provider` value object) exist verbatim; no existing consumer of `useSession()` (`AppShell.tsx`, `settings/page.tsx`, `login/page.tsx`, `(app)/layout.tsx`) destructures in a way an added field could break.
- `auth.verify(token)` / `auth.resendVerification()` (Task 7, already landed for real as of this audit, commit `b2f58de`) match Task 8's calls exactly; `token` narrows from `string | null` to `string` correctly after the `null`/`""` guard before being passed to `auth.verify`.
- `Button` (`ButtonHTMLAttributes<HTMLButtonElement> & { variant? }`) and `UsageBar` (`{label, used, limit, unit, refill}`) prop usage in `VerifyBanner.tsx`, `verify/page.tsx`, and the settings-page edit all match the real component signatures.
- Hook usage in `VerifyBanner`, `VerifyInner`, and `ResendOrSignIn` follows the rules of hooks (hooks called unconditionally before any early return in all three). The `useRef` double-invoke guard in `VerifyInner` is a valid pattern, and this Next version does need the `<Suspense>` wrapper around a `useSearchParams()` consumer — confirmed independently by `(app)/layout.tsx`'s own comment about `AppShell` needing the same treatment.
- `dev:webpack`, `lint`, `test` scripts referenced in Steps 6-8 all exist in `web/package.json` exactly as named.

---

## Summary counts

- **Task 5:** 3 Critical, 3 Important, 2 Minor
- **Task 6:** 1 Critical, 0 Important, 1 Minor
- **Task 8:** 1 Critical, 1 Important, 2 Minor
