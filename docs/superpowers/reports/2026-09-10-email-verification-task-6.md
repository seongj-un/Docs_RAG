# Task 6 report — `/usage`: 미인증일 때 다른 한도

## Status: DONE

Commit: `7e25c39690879a1e95ea42cfba16202c9f5c9075` (short `7e25c39`), on branch
`feat/email-verification`, on top of `900ff81` (a concurrent test-only commit
from another agent working the same branch — see "Concurrent activity"
below). The branch has since moved further ahead of my commit as other
agents kept committing; I did not rebase, switch branches, or pull, per the
task's constraints.

## Files changed

### `app/schemas.py` — `UsageOut`

Added exactly the brief's Step 3 block: the docstring's new paragraph
("미인증 계정은 다른 한도를 **다른 창**으로 센다...") and five new fields
(`email_verified`, `queries_total`, `documents_total`,
`unverified_query_limit`, `unverified_document_limit`), appended after the
existing four (`queries_today`, `queries_per_day`, `pages_this_month`,
`pages_per_month`), which are untouched.

### `app/routers/usage.py` — `get_usage`

Added exactly the brief's Step 4 block: the four new `UsageOut` kwargs
(`email_verified=user.email_verified`,
`queries_total=await usage_service.queries_total(session, user.id)`,
`documents_total=await usage_service.documents_total(session, user.id)`,
`unverified_query_limit=settings.unverified_quota_queries`,
`unverified_document_limit=settings.unverified_quota_documents`). No new
imports needed — `User`, `usage_service`, and `settings` were already
imported. Confirms the brief's "Consumes: Task 5의 `usage.queries_total` /
`documents_total`" line: both functions already existed in
`app/services/usage.py`, nothing to add there.

### `tests/test_usage_unverified.py` (new)

Same test name, assertions, and Korean docstrings as the brief's Step 1
block, verbatim, including both Korean comments inside the test body
("게이트가 세는 것은 수락된 업로드다..." and "기존 필드는 그대로 남는다...").
Per the brief's own opening warning ("이 브리프의 코드 블록보다 우선한다")
and the task's global constraints, two things changed from the brief's
literal snippet:

1. **Execution mechanics.** `pytestmark = pytest.mark.asyncio` →
   `pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")`;
   the single `async def test_unverified_usage_reports_the_taster_limits()`
   became a synchronous `def test_unverified_usage_reports_the_taster_limits()`
   wrapping a local `async def scenario()` (which returns the response body)
   run via `run_async(scenario)`, with assertions after, outside the
   coroutine — the same shape `tests/test_verification_api.py` uses.
   `run_async` / `_db_available` copied verbatim from `tests/test_verification.py`.
2. **Dropped `auth_limiter.reset()` and its import.** The brief's Step 1
   snippet calls `auth_limiter.reset()` at the top of the test. The task's
   global constraints state this directly: "`tests/conftest.py` resets the
   rate limiters before each test... so `AsyncClient(base_url="http://test")`
   carries session cookies and you need no `.reset()` calls of your own" —
   confirmed by reading `tests/conftest.py`'s `fresh_rate_limits` fixture
   (`autouse=True`, resets `query_limiter`, `upload_limiter`, `auth_limiter`,
   `verify_resend_limiter` before every test). Kept the import list minimal
   accordingly (no `app.services.ratelimit` import in this file at all).

## Commands run, verbatim-equivalent output

### `.venv/bin/python -m pytest tests/test_usage_unverified.py -v`

```
tests/test_usage_unverified.py::test_unverified_usage_reports_the_taster_limits PASSED [100%]
======================== 1 passed, 6 warnings in 0.64s =========================
```

Matches the brief's Step 5 "Expected: 1 passed" exactly.

**Confirmed red before green**, since I'd already written the implementation
by the time I first ran this: I used `git stash push -- app/schemas.py
app/routers/usage.py` (deliberately leaving the new test file in place) to
put just the schema/router back to their pre-Task-6 state, reran the same
command, got `1 failed` (both files reverted together, so `UsageOut(...)`
construction inside the route still succeeds with only the old four fields —
the test then fails at `body["email_verified"]`, i.e. the brief's predicted
`KeyError: 'email_verified'` shape), then `git stash pop` to restore.

### Baseline vs. after — `.venv/bin/python -m pytest tests/ -q --ignore=tests/test_error_details.py`

I measured the baseline myself rather than trusting a quoted figure, per the
task instructions — and I want to flag honestly that I did this *after*
already editing the three files, not before starting as instructed. I
corrected by using `git stash push -u -- app/schemas.py app/routers/usage.py
tests/test_usage_unverified.py` to put the working tree back to the exact
pre-Task-6 state (commit `59b07f3`'s content for these paths — the branch tip
at the time was `900ff81`, but that commit only touches
`tests/test_error_details.py`, which is excluded from this command), ran the
suite, then `git stash pop` to restore (confirmed identical working tree
afterward via `git status`).

**Baseline, before any Task 6 change:**
```
153 passed, 6 warnings in 13.60s
```
0 failed, 0 skipped (Postgres reachable).

**Immediately after committing Task 6 (`7e25c39`), before any later
concurrent commit landed:**
```
154 passed, 6 warnings in 13.51s
```
0 failed, 0 skipped. `154 = 153 + 1` — exactly the one new test, nothing else
shifted.

**Re-ran once more at the very end of this task**, after other agents had
gone on to commit `d57155c` (a `web/`-only fix) and to leave uncommitted,
unstaged WIP in `app/routers/auth.py` / `tests/test_verification_api.py`
(confirmed by reading their diff stat only, not their content, and not
touching either file): the same command now reports `155 passed`. That extra
test is that other agent's in-progress work, not mine — the attributable,
verified-in-isolation number for this task is **153 → 154**.

## Concurrent activity observed on this shared branch/worktree

Per the task's warning, several other agents committed or edited files on
`feat/email-verification` while this task was in progress, all outside my
three files:

- `030ebdd feat(web): 인증 착지 페이지와 배너` — landed before I started editing.
- `900ff81 test: 이메일 인증 detail 4개를 백엔드-프론트 대조에 넣는다` — landed
  while I was reading files, before my first edit; this is what my "baseline
  before" commit is measured at.
- `d57155c fix(web): 미인증 사용량 안내 문구가 쿼터 값을 하드코딩하던 문제` —
  landed immediately after my commit; per its own message this is the
  frontend consumer reading the `queries_per_day` / `pages_per_month` fields
  my endpoint already returned, instead of hardcoding the quota numbers.
- `0816fdf docs: 게이트의 동시성 한계를 스펙에 적는다` — landed after that.
- Uncommitted, unstaged edits to `app/routers/auth.py` and
  `tests/test_verification_api.py` were present in the working tree at the
  very end (not committed by anyone as of this report) — untouched by me.

I staged only `app/schemas.py app/routers/usage.py tests/test_usage_unverified.py`
by explicit path for my commit (never `git add -A`/`git add .`); `git status`
was clean except these three immediately before staging and immediately
after the commit.

**One anomaly worth flagging even though it caused no harm:** during the
`git stash`/`git checkout HEAD~1 -- ...` sequence I used to measure the
baseline and to try to capture the pre-implementation failure text, an
untracked file `tests/test_usage_unverified 2.py` briefly appeared —
byte-identical to my own `tests/test_usage_unverified.py` (confirmed with
`diff`, zero output) but with different permission bits (`-rw-------` vs. the
real file's `-rw-r--r--@`), consistent with a filesystem/sandbox
reconciliation artifact rather than another agent independently authoring
the same 79 lines. It was never tracked, never staged, and had already
disappeared on its own by the time I went to remove it (`rm` reported "No
such file or directory" moments later). It briefly inflated one ad-hoc full-
suite count by one. No git history, commit, or file on disk was affected —
noting it here only for transparency.

## What surprised me

- I started editing before measuring the baseline, which the task explicitly
  warned against ("do not trust any figure I quote; I have gotten it wrong
  three times"). I corrected it with `git stash` rather than skipping it, but
  flagging the sequencing mistake here rather than glossing over it.
- The shared working tree is genuinely live — `git status` and `git log`
  changed under me between commands more than once (a fast-forwarded HEAD,
  a transient duplicate file, then real uncommitted WIP in unrelated files).
  Explicit-path staging and re-checking `git status` immediately before
  staging and immediately after committing were what actually kept this
  task's commit clean; I'd have caught a real conflict this way too.
- Everything in the brief itself matched the codebase exactly: `queries_total`/
  `documents_total` (Task 5), `User.email_verified`, and
  `settings.unverified_quota_queries`/`unverified_quota_documents` (Task 1)
  were all already in place, so Steps 3–4 were a direct, verbatim transcription
  with no adaptation needed beyond the test harness.

## Commit

```
git add app/schemas.py app/routers/usage.py tests/test_usage_unverified.py
git commit  # 7e25c39690879a1e95ea42cfba16202c9f5c9075
```

`git status` showed exactly these three paths (two modified, one new) before
staging, and was clean again immediately after the commit.
