# Reservation settlement + sweep — report

Branch `main`, one commit on top of `f81716e`:

- `2d5b8df0fa7c1362870d8cc5b787edf6ebd2ca7f` — `settled_at` marker + sweep

## Suite numbers (measured, not quoted)

| When | Command | Result |
|---|---|---|
| Baseline, before any change (clean tree at `f81716e`) | `.venv/bin/python -m pytest tests/ -q` | **174 passed, 0 failed** (13.48s) |
| After the fix, full suite | same | **178 passed, 0 failed** (14.34s) |
| Source-only stash (`app/config.py app/models.py app/routers/documents.py app/services/usage.py`; migration + new test file left in place) — new test file only | `.venv/bin/python -m pytest tests/test_reservation_sweep.py -v` | **3 failed, 1 passed** (see below) |
| `git stash pop`, new test file only | same | **4 passed** (0.54s) |
| Restored, full suite (final) | `.venv/bin/python -m pytest tests/ -q` | **178 passed, 0 failed** (14.01s) |

Environment note: Postgres was already up (`docs_rag-db-1`). TEI embed was already answering on 8080. Rerank on 8081 was down at the start of this session — not the "unrelated app" 401 collision the task warned about, just not running — so I started this project's own `scripts.local_model_server --port 8081` (the same thing `scripts/stack.sh up` would have started) so the baseline and every subsequent run were clean end-to-end rather than skewed by a gap unrelated to this change. I didn't touch anything else about the environment.

## The gap, and the fix

`f81716e` made quota check-then-act atomic with `pg_advisory_xact_lock` + a reservation row in `usage_events`, released as soon as the row is committed so the slow work (embed/rerank/LLM) runs unlocked. If the process dies between that commit and `commit_reservation`/`release_reservation` (SIGKILL, OOM, a deploy), the row stays forever, and nothing distinguished it from a real completed event (both have `tokens_in=0, tokens_out=0, cached=False`). For a verified account this is a rounding error that a day-window quota ages out on its own. For the unverified taster allowance — deliberately cumulative over the account's lifetime, to stop re-signup abuse — an orphan is permanent: it eats one of 5 queries / 1 document / 50 pages forever, with no recovery path for the user or the operator.

Fix, in four pieces:

1. **Marker.** `usage_events.settled_at` (nullable timestamp, migration `0009`), same shape as `email_verification_tokens.consumed_at`. NULL = still a reservation; a value = this row's contents are final. All 20 pre-existing rows backfilled to `settled_at = created_at`.
2. **Every real-usage write sets it.** `record()` sets it immediately (this is what `ingest.py`'s post-index-success write goes through). `commit_reservation()` sets it when the work behind a reservation finishes. `reserve()` defaults to leaving it NULL (the query path, which really does finish later via `commit_reservation`/`release_reservation`), but takes a new `settled: bool = False` — `documents.py`'s upload path now passes `settled=True`, because for uploads every value (`pages`) is already known before the lock and nothing calls `commit_reservation` afterward; without this, upload rows would have been the single worst case, since they back the lifetime document/page quota directly.
3. **Aggregates unchanged.** `queries_today`, `queries_total`, `documents_total`, `pages_uploaded_total`, `pages_this_month` still count NULL (pending) rows — that's what makes two concurrent requests see each other. Added a comment block warning against ever adding a `settled_at` filter there.
4. **Sweep.** `acquire_quota_lock()` now calls a new `_sweep_stale_reservations()` right after taking the per-`(kind, user_id)` advisory lock, before the caller re-reads the aggregate. It deletes this user's own rows for this `kind` where `settled_at IS NULL AND created_at < now() - reservation_ttl_seconds`. No background job or startup hook — it runs on the next request of the affected kind from the affected user, which is exactly when it's needed.

## Timeout: `reservation_ttl_seconds`, default 600s

`embed()` and `rerank()` each hard-cap their HTTP call at 120s (`httpx.Timeout(120.0)`, hardcoded in `embeddings.py`/`rerank.py`), and one hybrid query's critical path can hit both — 240s. The LLM call is measured at 34-40s on top of that (existing comment in `config.py` on `gemini-3.6-flash`). Worst-case legitimate, non-crashing request: ~300s. 600s leaves close to double that margin. This number is a judgment call built from the two data points the task supplied, not a fresh measurement of this environment's actual worst-case latency — flagged again under Concerns.

Setting added to both `app/config.py` and `.env.example` (`RESERVATION_TTL_SECONDS`), with the same arithmetic in the comment at both places.

## Tests — `tests/test_reservation_sweep.py` (4 new)

1. **`test_orphaned_reservation_past_the_ttl_does_not_lock_the_account_forever`** — reserves + commits with no finalize (the crash), ages the row past the ttl, then drives a *real* subsequent request through `QueryRunner.enforce_limits` and asserts it succeeds — not just that a boolean flips, but that exactly one row remains afterward (the orphan swept, this request's new reservation committed).
2. **`test_a_fresh_reservation_still_blocks_the_next_request`** — the one the task calls out as mattering most. An age-0 reservation is immediately followed by a second `enforce_limits` call against the same now-exhausted quota; asserts it's still blocked (429) and exactly one row exists. I deliberately did **not** build this on the blocker-lock concurrency technique from `test_verification.py`/`test_phase2.py` (a third connection holds the lock, two tasks race, `asyncio.wait(timeout=0.3)` proves real contention) — that pattern proves mutual exclusion, which predates this change and is already exercised, unmodified, by `test_phase2.py::test_concurrent_queries_from_a_verified_account_accept_only_one_over_quota` and `test_verification_api.py::test_concurrent_uploads_from_an_unverified_account_accept_only_one` (both still pass, confirmed in every full-suite run above). What's new here is the ttl's age boundary, and a lock handoff that takes low milliseconds can't exercise that boundary regardless of how badly the ttl is misconfigured. Explained this reasoning in the test's own docstring rather than silently duplicating the existing race tests.
3. **`test_record_created_events_are_never_swept_at_any_age`** — a `record()`-created row aged 10 years survives the sweep; also re-confirms `pages_this_month` still computes correctly afterward.
4. **`test_upload_reservations_are_born_settled_and_are_never_swept`** — not individually named in the task, added because it's the direct validation of the `documents.py` change: reproduces `reserve(..., settled=True)`, ages the row 10 years, confirms `documents_total`/`pages_uploaded_total` are unaffected by the sweep.

## Before/after (stash evidence)

Stashed exactly `app/config.py app/models.py app/routers/documents.py app/services/usage.py` (explicit paths). Left `alembic/versions/0009_usage_event_settled_at.py` and `tests/test_reservation_sweep.py` in place — the migration had already been applied to the dev DB (`alembic upgrade head` run before any of this), so `settled_at` existed as a physical column regardless of what the stashed `models.py` declared; git stash doesn't "undo" a migration already run against a live database.

Running `tests/test_reservation_sweep.py` against that stashed (pre-fix) state:

- `test_orphaned_reservation_past_the_ttl_does_not_lock_the_account_forever` — **FAILED**, `AttributeError: 'Settings' object has no attribute 'reservation_ttl_seconds'`.
- `test_a_fresh_reservation_still_blocks_the_next_request` — **PASSED**. Expected, and explained above (item 2): with no sweep at all, nothing can over-evict, so this is vacuously true pre-fix. It's a regression guard for the fix's own correctness, not a demonstration of the original bug.
- `test_record_created_events_are_never_swept_at_any_age` — **FAILED**, `assert settled is not None` → `None is not None` (old `record()` never sets the column).
- `test_upload_reservations_are_born_settled_and_are_never_swept` — **FAILED**, `TypeError: reserve() got an unexpected keyword argument 'settled'`.

3 of 4 failed, 1 passed by design. `git stash pop` restored cleanly; all 4 pass, and the full suite is 178 passed / 0 failed.

## Migration

`alembic/versions/0009_usage_event_settled_at.py`. `upgrade`: add nullable `settled_at`; backfill `UPDATE usage_events SET settled_at = created_at` (20 rows in this dev DB, confirmed 0 NULL afterward). `downgrade`: drop the column. Verified the full round-trip by hand: `upgrade head` → confirmed column + backfill → `downgrade -1` → confirmed column gone (`information_schema.columns` listing) → `upgrade head` again → confirmed column back, nullable, and re-backfilled with 0 NULLs.

## Concerns

- **The 600s default is a judgment call**, built from the two figures the task supplied (120s × 2 external calls, 34-40s LLM), not a fresh measurement of this environment's real worst-case end-to-end latency. If a future change adds retries, batching, or another sequential model call to the query path, this number should be revisited — the arithmetic is in the `app/config.py` comment specifically so that's auditable.
- **`_sweep_stale_reservations` runs on every `acquire_quota_lock` call** (every query, every upload) as an extra `DELETE`. It's scoped to one user and reuses the existing `(user_id, created_at)` index; given quota-bounded row counts per user this should be a non-issue, but I did not add a dedicated index (e.g. on `(user_id, kind, settled_at)`) — worth a look if a single account's `usage_events` ever grows very large.
- **Sweep-then-rollback is harmless but not literally a deletion.** When the sweep's `DELETE` runs inside a transaction that later rolls back (the account is still over quota even after the phantom is removed), the physical row survives — but every future check re-sweeps (uncommitted) before counting, so the phantom is never counted again either way. I reasoned through this carefully but did not write a dedicated test for "row physically persists but is functionally inert under repeated rollback" specifically, judging the four tests above to cover the behavior that actually matters.
- **`conversations.py`'s streaming path catches `HTTPException`/`Exception`, not `BaseException`** — a client disconnect mid-stream can raise `CancelledError`/`GeneratorExit`, neither of which triggers `release_reservation()`. This predates my change and is a second, non-crash way a reservation can be orphaned; the sweep now recovers from it too (indistinguishable, timing-wise, from a process crash), so I left the exception handling itself alone as out of scope rather than folding an unrelated fix into this one.

No disagreements with the task as given.
