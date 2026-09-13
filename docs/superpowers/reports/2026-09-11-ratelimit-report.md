# Rate-limit / concurrency fixes — report

Branch `main`, two commits:

- `03785c2` — Part 1: `POST /auth/verify` rate limit
- `f81716e` — Part 2: atomic quota check-then-act (query + upload)

## Suite numbers (measured, not quoted)

| When | Command | Result |
|---|---|---|
| True baseline (`git stash` of all my changes, clean HEAD) | `.venv/bin/python -m pytest tests/ -q` | **169 passed, 0 failed** |
| After Part 1 only | same | **171 passed** (169 + 2 new) |
| After Part 1 + Part 2, final state | same | **174 passed, 0 failed** |
| Repeated 3x for stability | same | 174 passed each time |

I did not observe the port-8081/`test_verifying_restores_the_normal_quota` failure the coordinator flagged as expected baseline noise — my baseline run (stashed, clean tree) came back `169 passed, 0 failed` with nothing skipped. Whatever was answering on 8081 wasn't there during my session, or it stopped. Either way it's not something my changes touch, and I didn't chase it.

No changes were needed to `tests/conftest.py` for either part — nothing here to merge with the coordinator's planned `tei_url`/`rerank_url` pin.

## Part 1 — `POST /auth/verify` rate limit

**File: `app/routers/auth.py`.** Added `request: Request` to `verify()` and one line, `_guard_attempts(request)`, as the first statement in the handler, before `verification.consume_token(...)`. No email argument — verify has no email at the point the guard must fire (that's the whole point: the token maps to an email only after the DB lookup this guard exists to bound), so it consumes only the IP bucket, exactly the fallback `_guard_attempts` already supports.

Checked and confirmed no other file needs to change:
- `tests/test_error_details.py`'s AST scan finds literal `detail=` strings passed to `HTTPException(...)`. `_guard_attempts` already contains the `"auth rate limit exceeded"` literal (used by signup/login); calling that same function from `verify` adds no new call site for the scanner to find, so `EXPECTED`/`DETAIL_MAPPED` need nothing added.
- `web/lib/api/errors.ts` already maps `"auth rate limit exceeded"` (grepped and confirmed).
- `web/app/verify/page.tsx` calls `auth.verify()` exactly once per page load (guarded by a `useRef` against React 18 double-invoke) and has no retry loop, so a 429 here can't cause a hammering loop client-side — it just renders the existing failure copy via `describeError`.

**File: `tests/test_auth_ratelimit.py`.** Added two tests following the file's existing pattern (`_attempts(payloads, path=...)`, `@needs_db`):
- `test_verify_is_throttled_too` — `limit + 2` bogus-token POSTs to `/auth/verify`; asserts the first is a `400` (not yet throttled, proving the guard doesn't block *before* it should) and a `429` shows up and is the last code.
- `test_verify_throttle_is_per_address_not_shared_with_login` — hammer `/auth/verify` from one address up to the limit, then confirm the *next* `/auth/login` from that same address is also `429` — proving the IP bucket is genuinely shared across auth routes (by the existing `auth_limiter` design), not just present on `/auth/verify` in isolation.

## Part 2 — atomic quota check-then-act

### Design chosen

Postgres `pg_advisory_xact_lock`, keyed on `(kind, user_id)` via `hashtext('quota:<kind>:<user_id>')`, held only across the **check + reserve + commit** — never across embedding/retrieval/reranking/generation. The lock is transaction-scoped so the commit that ends the locked section is also what releases it; a hash collision between two different `(kind, user_id)` pairs is harmless (it only serializes two unrelated requests briefly — the actual decision still comes from the `*_exceeded` query re-read after the lock is held, not from the lock's uniqueness).

The reservation itself is the row: `usage.reserve()` inserts a `UsageEvent` (with a client-generated id, see bug below) *before* the real work runs. On success, `usage.commit_reservation()` fills in the real `tokens_in`/`tokens_out`/`cached` and commits — no second row. On failure, `usage.release_reservation()` rolls back, deletes that one row by id, and commits. `usage_events` stays the only counter; nothing new is kept in memory or in a second table, so there's nothing to drift from it.

**Query path** (`app/services/pipeline.py`, `app/routers/query.py`, `app/routers/conversations.py`): `QueryRunner.enforce_limits` now does lock → re-check → `reserve` → commit (wrapped in `try/except BaseException: rollback; raise` so a rejected request never leaves the lock/transaction hanging). `QueryRunner` tracks `self._reservation_id`, cleared by `record_cache_hit`/`finalize` once they convert it to a real record. A new `QueryRunner.release_reservation()` is a no-op once that's happened, which is what lets both callers invoke it unconditionally on any exception:
- `query.py`: wrapped everything from `resolve_scope()` through `finalize()`/return in `try/except BaseException: await runner.release_reservation(); raise`.
- `conversations.py`: `_stream_turn` already had one big `try` with two `except` blocks that clean up the orphaned question message (`_discard_unanswered`). Added `if runner is not None: await runner.release_reservation()` to both, next to that existing cleanup — same failure surface, same shape, no restructuring needed since the try already spanned the whole turn.

**Upload path** (`app/routers/documents.py`): simpler, because for uploads the only quantity the quota needs (`pages`) is already known (parsed) *before* the locked section even starts, and there's no fallible network/LLM call between "decide to accept" and "record the accept." So there's no separate reserve → (later) finalize-or-release; the lock section is check → `Document` row → `reserve` → **one commit**, wrapped in `try/except BaseException: rollback; raise`. The absolute per-file page cap (`max_upload_pages`) moved outside/before the lock since it depends on nothing but the file itself — no cross-request race is possible there, so no reason to hold the lock for it.

### What I rejected, and why

1. **Lock the whole check-through-record span in one transaction.** This is exactly what constraint 2 rules out: a real LLM call is tens of seconds, and holding a lock (or just an open transaction pinning a connection) across it serializes one user's requests end-to-end and ties up a connection from the pool for the duration. Rejected outright.
2. **A single user-level transaction wrapping the whole request.** Same problem as (1) restated — plus it doesn't fit the existing session lifecycle (`query.py`'s session is request-scoped via `Depends(get_session)`; `conversations.py` opens its own `SessionLocal()` for the life of the SSE stream). Neither wants to hold one open transaction from the first check to the last write.
3. **A second, in-memory or DB counter dedicated to "reserved but not yet finalized."** Rejected because the task is explicit that `usage_events` is the deliberate single source of truth, and a second counter is one more place for reality and the count to disagree — especially across restarts/workers, which is the exact property `usage_events` exists to have and an in-memory structure would not.
4. **Row-level lock on the aggregate instead of an advisory lock.** There's no row to lock before the first `usage_events` row for a (user, kind) exists, and even after one exists, "lock the count" isn't a single row — advisory locks are the standard tool for exactly this shape (see `pg_advisory_xact_lock` docs' own "protecting an aggregate" example), and it's what let me key on `(kind, user_id)` directly rather than on data that may not exist yet.
5. **Skip the upload-side reserve/release split and only fix the read.** Considered simply re-running the `*_exceeded` checks under the lock without also moving the insert into the same commit — rejected because that still leaves a window (however tiny) between "lock says OK" and "row exists," and since folding the `Document` insert into the same commit costs nothing extra (it was already being committed right after in the original code), there was no reason to leave that gap open.
6. **Compensate for a disk-write failure after the upload's commit.** The upload commit (Document + reservation) already matches this codebase's existing definition of "accepted" — `documents_total`/`pages_uploaded_total`'s doc comments are explicit that acceptance, not indexing success, is what the quota counts, precisely so a slow/failed background index doesn't retroactively change what already counted. A disk write failing between that commit and `open(path, "wb")` is a pre-existing, unprotected edge case (today's code already commits `usage.record` before writing the file) that has nothing to do with the concurrency bug I was asked to fix. Extending the fix to compensate for it would be new behavior nobody asked for, on a failure path this task's constraints don't mention. I left it as-is and am flagging it here rather than silently deciding either way.

### A bug I found by testing, and fixed

`usage.reserve()` originally did:
```python
event = UsageEvent(user_id=user_id, kind=kind, pages=pages)
session.add(event)
return event.id
```
`UsageEvent.id` has only a **client-side** SQLAlchemy default (`default=uuid.uuid4`), which is populated at flush time, not at object-construction time. `event.id` right after `session.add()` is `None`. This silently broke the query path specifically: `enforce_limits` would store `self._reservation_id = None`, and `commit_reservation`/`release_reservation` would then run `UPDATE ... WHERE id = NULL` / `DELETE ... WHERE id = NULL`, matching zero rows — the reservation row would sit in the table forever, uncharged-for-real-work but never cleaned up either. The upload path never hit this because it discards `reserve()`'s return value entirely (its "commit" and its "reservation" are the same step, so nothing downstream needs the id back).

My own new test (`test_a_failed_query_does_not_spend_the_quota`, described below) caught this on first run — it failed with `assert 1 == 0` instead of erroring, which is exactly the "test doesn't lie" case the task asked me to take seriously. Fixed by generating the id in Python before constructing the row:
```python
event_id = uuid.uuid4()
event = UsageEvent(id=event_id, user_id=user_id, kind=kind, pages=pages)
session.add(event)
return event_id
```
All tests pass after the fix; I'm calling this out explicitly rather than folding it silently into "the fix" because it's the kind of thing that's easy to miss if reviewed by reading rather than running.

### Before/after race evidence

**Primary, permanent tests** (same technique as `tests/test_verification.py`'s two race tests: a third connection holds the exact lock the code under test also acquires, two racing tasks are proven genuinely blocked via `asyncio.wait(timeout=0.3)` + `assert not done`, then the blocker releases and Postgres's own lock queue — not test timing — decides the winner):

- `tests/test_verification_api.py::test_concurrent_uploads_from_an_unverified_account_accept_only_one` — two concurrent uploads from a fresh unverified account (allowance: 1 document). Asserts exactly one `202` + one `403`, and separately that `documents_total() == 1` in the database (not just that the HTTP responses looked right).
- `tests/test_phase2.py::test_concurrent_queries_from_a_verified_account_accept_only_one_over_quota` — same technique, directly on `QueryRunner.enforce_limits` (quota patched to 1/day), because this is the pre-existing verified-account rule the task insisted must get the identical guarantee, not just the new unverified gate.

Stashing only the Part 2 source files (`app/services/usage.py`, `app/services/pipeline.py`, `app/routers/{documents,query,conversations}.py`) and re-running with the new tests still in place:

```
FAILED tests/test_phase2.py::test_concurrent_queries_from_a_verified_account_accept_only_one_over_quota
FAILED tests/test_verification_api.py::test_concurrent_uploads_from_an_unverified_account_accept_only_one
2 failed, 172 passed
```
(both fail with `AttributeError: module 'app.services.usage' has no attribute 'acquire_quota_lock'` — the old code has no synchronization primitive at all for this, which is an accurate, if blunt, way for it to fail.) Restoring the stash: `174 passed, 0 failed`, and both tests pass deterministically across 5 repeated runs each.

**Extra, non-committed evidence** (a throwaway script, not part of the suite, run to see the actual double-accept rather than just an `AttributeError`): monkeypatched `usage.unverified_upload_exceeded` to `await asyncio.sleep(0.2)` after computing its real answer, artificially widening the check-then-act window regardless of whether anything in between now serializes on it, then fired two concurrent `/documents` uploads for one fresh unverified account (allowance 1):

- Against the pre-fix (stashed) source: `status_codes=202,202 accepted_rows=2 (quota=1)` — the exact bug described in the task, reproduced directly rather than inferred.
- Against the fixed source, same artificial delay still in place: `status_codes=202,403 accepted_rows=1 (quota=1)` — the fix holds even under an adversarial delay injected at the exact check call, not only under the specific blocker-lock test technique.

**Constraint 1 (don't charge for work that didn't happen), separately verified:**
- `tests/test_phase2.py::test_a_failed_query_does_not_spend_the_quota` — embedding forced to raise (503), asserts `usage.queries_total() == 0` afterward. (This one passes both before and after my change — record-only-on-success already held for the simple non-concurrent case; what my change adds is that it *still* holds now that a reservation exists to leak. Noting this so it isn't mistaken for a second race-condition proof.)
- `tests/test_conversations.py::test_failed_turn_leaves_no_unanswered_question` — existing test (embed forced to raise mid-stream), extended with the same `usage.queries_total() == 0` assertion to cover the streaming path's separate except-block cleanup.

## Things I disagree with or couldn't do

Nothing I disagree with. One thing I could not do because it's out of my authority per the constraints: nothing here needed a new setting, so there's nothing to hand to you for `app/config.py`.

One judgment call worth your attention even though I stand by it: the disk-write-failure gap in the upload path (item 6 under "rejected," above) predates this fix, isn't part of the check-then-act race this task describes, and I left it alone rather than silently expanding scope to fix it.
