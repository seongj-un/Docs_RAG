# CAND_K default: 50 -> 20

## Status

Done. Committed on `main`, not pushed.

## Commit

`6b617d2` — "tune(retrieval): CAND_K 50 -> 20, measured not guessed"

Files changed (staged individually, no `git add -A`):
- `app/config.py` — `cand_k` default 50 -> 20, terse comment replaced with the
  measurement + reasoning (matches the `rerank_top`/`rerank_min_score` house
  style: command run, corpus, numbers, why this value over the alternatives).
- `.env.example` — added `CAND_K=20` to the Retrieval section (was previously
  absent from this file entirely), with a short comment pointing to
  `app/config.py` for the full reasoning rather than duplicating it.
- `README.md` — **contested file, staged only these two hunks** (verified via
  `git diff` before staging that no other changes existed in the file): the
  config table row (`CAND_K | 50` -> `20`), and the prose in the "후보 수
  (CAND_K)" eval section that explained why `wide` is the corpus built for
  this — see "Note on eval/corpora/wide.py" below for why this wasn't a
  plain find-replace.
- `eval/corpora/wide.py` — docstring only (not `CLAUSES`/`QUERIES`, not
  touched). Same reason as the README hunk above.

`eval/candk_sweep.py`'s own `CAND_KS = (100, 50, 30, 20, 10, 5)` sweep tuple
was left untouched, as instructed — it deliberately sweeps a range.

## Tests

- Before my change: `1 failed, 168 passed` (169 total), 14.42s.
- After my change: `1 failed, 168 passed` (169 total), 13.06s.
- Identical pass/fail outcome before and after.

The one failure, `tests/test_verification_api.py::test_verifying_restores_the_normal_quota`,
is pre-existing and unrelated to this change — it's about email-verification
quota restoration (`usage.record`, `/auth/verify`), nothing in the
retrieval/rerank path. I did not investigate or fix it; out of scope for this
task.

No test in `tests/` asserts on `cand_k`/`CAND_K` or on a candidate count tied
to it — confirmed by grep (zero hits) and by checking the tests that do touch
retrieval/tracing (`test_query_pipeline.py`, `test_tracing.py`,
`test_isolation.py`, `test_conversations.py`): the count assertions they do
have (e.g. `len(snap["stages"][STAGE_RRF]) == 3`) are about the size of a
small mocked fixture, not the real `cand_k` default, so they don't hold or
break either way here.

## Re-validation not run

Per instructions, I did not run `python -m eval.candk_sweep` or any other
`eval/` entry point — they need the local model server (BGE-M3 + reranker on
:8081), which isn't running, and starting it was explicitly out of scope. The
1226ms/135ms/9.1x/1.7x/6.7x numbers in the new `app/config.py` comment and in
the commit message are the measurement given for this task, not something I
re-derived or independently verified. To re-validate:

    python -m eval.candk_sweep --corpus wide

(requires Postgres and the model server reachable at `RERANK_URL`).

## Concerns

1. **`eval/corpora/wide.py`'s docstring needed more than a number swap.** It
   argued "every other corpus here is smaller than the default 50 — 12, 36,
   40 chunks — so [CAND_K] does nothing [to them]." I checked the actual
   sizes: `longchunk`=12, `simple`=15, `hard`=40 (measured by importing each
   corpus module and counting `CLAUSES`). At the new default of 20, `hard`'s
   40 chunks are **no longer** under the cutoff — the cutoff now binds on
   that corpus too, at least somewhat, which is a substantive change, not
   just a documentation one: `python -m eval.run --corpus hard` (an existing,
   presumably previously-recorded eval path) will now retrieve a narrower
   candidate set than it did under the old default. I did not re-run it (out
   of scope; needs the model server). I reworded the docstring and the
   parallel README paragraph to state this honestly rather than leave a
   self-contradicting "smaller than 20 — 40 chunks" sentence. Worth someone
   re-running `eval.run --corpus hard` once the model server is up, to see if
   any previously-recorded numbers for that corpus moved.
2. **Unrelated pre-existing oddity I did not touch**: that same docstring's
   "12, 36, 40 chunks" list doesn't match what I measured (12, 15, 40) — the
   36 doesn't correspond to any corpus's chunk count I could find; the
   golden-set eval is "3문서·36문항" (36 *questions*, not chunks), so this
   looks like a pre-existing mix-up unrelated to the CAND_K value itself. Not
   caused by this change and not something the task asked me to audit, so I
   left it as-is.
3. **`scripts/bench_cpu_serving.py` also hardcodes 50** (`--cand-k` default,
   plus a comment that says "CAND_K를 20으로 줄이면... 예상" — literally
   projecting the value this task ships). I left it untouched: the task
   scoped the staleness check to "README.md, docs/, eval/", not `scripts/`,
   and that directory otherwise belongs to the agent working on deployment
   files. Flagging it here rather than silently leaving it stale.
4. `docker-compose.yml` also documents "CAND_K 10 -> 29s / CAND_K 50 -> 184s"
   — explicitly the other agent's file; not touched.
