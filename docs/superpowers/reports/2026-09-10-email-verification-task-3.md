# Task 3 report — 메일러

## Status: DONE

Commit: `482acead4a299f7a96a2222b63aee4fcf53dce04` (short `482acea`), on branch
`feat/email-verification`.

## Files changed

### `app/services/mailer.py` (new)

Implements the transport layer exactly as specified in the brief's Step 3:

- Module docstring stating the design decisions: two adapters so dev and prod
  don't block each other; HTTP API over SMTP because many hosts block ports
  25/465/587 and SMTP then fails as an unexplained timeout.
- `Mailer` — a `typing.Protocol` with `async def send(self, *, to, subject,
  html, text) -> None`, keyword-only as required (Task 4 calls it with
  keywords).
- `ConsoleMailer` — logs `to`, `subject`, and the plain-text body at INFO via
  `logger.info(...)`.
- `ResendMailer(api_key, sender)` — POSTs to `https://api.resend.com/emails`
  with `async with httpx.AsyncClient(timeout=_TIMEOUT) as client: ... await
  client.post(...); response.raise_for_status()`. `httpx` is imported at
  module level (`import httpx`), not `from httpx import AsyncClient`, per the
  constraint that tests monkeypatch `mailer.httpx.AsyncClient`.
- `get_mailer()` — returns `ResendMailer` only when `mail_provider == "resend"`
  **and** `resend_api_key` is non-empty; falls back to `ConsoleMailer`
  otherwise (logging a warning if the provider was set to `resend` but the key
  is missing). Not cached — reads `settings` fresh on every call, per the
  "Ambiguity resolved for you" instruction that settings get mutated by tests.
- No retry logic, per instruction.

### `tests/test_mailer.py` (new)

Four tests, same names/assertions/docstrings as the brief's Step 1 block,
adapted only in execution mechanics per the brief's own test-harness warning
and the task's global constraints:

- `test_missing_key_falls_back_to_console` and
  `test_resend_is_used_when_configured` — unchanged, already synchronous.
- `test_console_mailer_logs_the_link` and
  `test_resend_posts_the_expected_payload` — the brief's code used `async def
  test_*` with `@pytest.mark.asyncio`. Per the instruction "this task's tests
  need no database, so `asyncio.run(...)` inside a synchronous `def test_*` is
  sufficient — do not add the `run_async`/`_db_available` harness here, and do
  not add a `skipif`," I kept these as sync `def test_*`, built a local `async
  def scenario(): ...` closure containing the exact body from the brief, and
  ran it with `asyncio.run(scenario())`. No `pytestmark`, no `skipif`, no
  `run_async`/`_db_available` — this module needs no database and must run
  even when Postgres is down.
- Dropped `import pytest`: once `@pytest.mark.asyncio` is gone, nothing in the
  file references `pytest.*` directly — `caplog` and `monkeypatch` are
  injected by pytest's own fixture mechanism without needing the import. Test
  names, assertions, and docstrings are otherwise verbatim from the brief.

## Commands run, verbatim output

### `.venv/bin/python -m pytest tests/test_mailer.py -v`

```
============================= test session starts ==============================
platform darwin -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0 -- /Users/seongjun/Desktop/project/Docs_RAG/.venv/bin/python
cachedir: .pytest_cache
rootdir: /Users/seongjun/Desktop/project/Docs_RAG
plugins: anyio-4.15.0, langsmith-0.12.2
collecting ... collected 4 items

tests/test_mailer.py::test_missing_key_falls_back_to_console PASSED      [ 25%]
tests/test_mailer.py::test_resend_is_used_when_configured PASSED         [ 50%]
tests/test_mailer.py::test_console_mailer_logs_the_link PASSED           [ 75%]
tests/test_mailer.py::test_resend_posts_the_expected_payload PASSED      [100%]

============================== 4 passed in 0.13s ===============================
```

### `.venv/bin/python -m pytest tests/ -q`

```
.....................................................................    [100%]
=============================== warnings summary ===============================
.venv/lib/python3.14/site-packages/google/genai/types.py:42
  .../google/genai/types.py:42: DeprecationWarning: '_UnionGenericAlias' is deprecated and slated for removal in Python 3.17
    VersionedUnionType = Union[builtin_types.UnionType, _UnionGenericAlias]
<frozen importlib._bootstrap>:491 (x2): DeprecationWarning: builtin type SwigPyPacked has no __module__ attribute
<frozen importlib._bootstrap>:491 (x2): DeprecationWarning: builtin type SwigPyObject has no __module__ attribute
<frozen importlib._bootstrap>:491: DeprecationWarning: builtin type swigvarlink has no __module__ attribute
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
141 passed, 6 warnings in 10.64s
```

All 6 warnings are pre-existing (google-genai / SWIG deprecation noise from
unrelated dependencies) and appear identically with or without
`tests/test_mailer.py` in the run — this change adds none.

## Baseline discrepancy (read before trusting "139")

The task stated the regression baseline as **139 passed, 0 failed**. I could
not reproduce that number on the branch as I received it. Before writing any
code I measured the suite with `tests/test_mailer.py` absent and Postgres
reachable (confirmed live via a direct `SELECT 1` through `SessionLocal`, and
independently by zero skips in the summary line — the suite's own
`skipif(not _db_available())` guards would have skipped, not silently
dropped, anything if the DB were down):

```
.venv/bin/python -m pytest tests/ -q --ignore=tests/test_mailer.py
...
137 passed, 6 warnings in 10.24s
```

**137, not 139.** `git status --short` before I wrote anything showed a clean
tree, so this isn't something I disturbed. I traced it as far as commit
`bb4d44e` ("fix(verification): consume_token 이 이중 소비되거나 재발급과
부딪혀 죽던 문제"), which is on this branch and landed *after* Task 2 per the
brief's framing ("Tasks 1–2 landed the schema and the token service") — it
added two new race-condition tests to `tests/test_verification.py` (11 → 13
`def test_` lines) as pure additions (116 insertions, 0 deletions). That
commit alone doesn't fully account for a 139→137 gap (it's +2, in the wrong
direction to explain a shortfall), so I don't have a confirmed root cause for
the exact discrepancy — only confirmation that it predates my work and isn't
something my change introduced.

What I can state with certainty, verified directly rather than assumed:
- Before my change: **137 passed, 0 failed, 0 skipped**.
- After my change: **141 passed, 0 failed, 0 skipped** — exactly 137 + 4, the
  four tests I added, with nothing else shifting in either direction.

So the regression check is clean regardless of which baseline number is
"correct": my change is purely additive and broke nothing.

## Deviations from the brief, and why

1. **Test execution mechanics** (`async def` + `@pytest.mark.asyncio` →
   sync `def` wrapping `asyncio.run(scenario())`). Required by the brief's
   own opening warning: "이 저장소에는 `pytest-asyncio` 가 없다 ... 아래에
   보이는 `pytestmark = pytest.mark.asyncio` 와 `async def test_*` 를 그대로
   쓰면 ... 로 죽는다" ("this repo has no pytest-asyncio ... using the
   `async def test_*` shown below as-is dies with that error"), and by the
   task's explicit instruction not to add the `run_async`/`_db_available`/
   `skipif` harness since this module needs no database. Verified: `pip list`
   shows `pytest 9.1.1`, `anyio 4.15.0`, `nest-asyncio 1.6.0` — no
   `pytest-asyncio` — confirming the brief's premise.
2. **Dropped `import pytest`** from the test file, since removing the
   `@pytest.mark.asyncio` decorators left no remaining reference to `pytest.*`
   in the module. Fixtures (`caplog`, `monkeypatch`) don't require the import.
3. Everything else — file contents, class/function signatures, the
   `get_mailer()` fallback logic, the Resend payload shape, the module
   docstring, test names/assertions/docstrings — matches the brief's Step 1
   and Step 3 code blocks verbatim.

## Established-pattern check (per the task's context note)

I read `app/services/upstream.py` and `app/services/embeddings.py` before
writing `ResendMailer`, as instructed, specifically to check whether the
brief's HTTP client code differed in style from this repo's established
outbound-HTTP pattern. It didn't, on the points that matter here: both
`embeddings.py` and the brief's `mailer.py` do `import httpx` at module level
and call `async with httpx.AsyncClient(timeout=_TIMEOUT) as client: resp =
await client.post(...); resp.raise_for_status()`. I kept the brief's code as
given.

One thing I noticed but deliberately did **not** carry over: `upstream.py`
also defines `UpstreamUnavailable` and a `calling(service)` context manager
that translates `httpx.HTTPStatusError`/`RequestError` into a domain
exception, and `embeddings.py` wraps its client calls in `with
calling("embedding"):`. I considered wrapping `ResendMailer.send` in
`calling("resend")` for consistency, but held off:
- No test in the brief exercises a Resend failure path, so nothing forces a
  particular exception type.
- `UpstreamUnavailable`'s own docstring frames it as "a model server we
  depend on did not answer usefully," which reads as scoped to the
  embedding/rerank servers, not a third-party transactional-email API.
- The task's own instruction says "Delivery failure is handled by the resend
  endpoint in Task 4" — I can't see Task 4's brief, and swapping the raised
  exception type from a raw `httpx` error to `UpstreamUnavailable` is exactly
  the kind of decision that could silently mismatch whatever Task 4 expects
  to catch.

Flagging this for whoever writes/reviews Task 4: right now, a Resend failure
in `ResendMailer.send` propagates as a raw `httpx.HTTPStatusError` (4xx/5xx)
or `httpx.RequestError` (connection/timeout) — not `UpstreamUnavailable`. If
Task 4's resend-endpoint wants to catch one exception type and turn it into a
clean HTTP response, it should catch those `httpx` exceptions directly, or
Task 4 should explicitly add its own translation layer.

## What surprised me

- The baseline count mismatch (139 vs. the 137 I measured) — see above. Not
  alarming once isolated (my change is cleanly additive either way), but
  worth surfacing rather than quietly reporting "139 → 143" when that's not
  what actually happened.
- Nothing else was surprising — `app/config.py` already had all three
  settings the brief expects (`mail_provider`, `resend_api_key`,
  `mail_from`), `app/services/verification.py` (Task 2) already has
  `build_link`/`build_email` ready for Task 4 to pair with this mailer, and
  the brief's code needed no adaptation beyond the test-execution mechanics
  called out by its own warning banner.
