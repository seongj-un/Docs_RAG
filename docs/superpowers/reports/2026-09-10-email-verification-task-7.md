# Task 7 report — frontend half (types · auth API · error copy)

Branch: `feat/email-verification`
Commit: `b2f58defd70a4dc67d673031383b90509182e46c`

## Scope actually implemented

Exactly the four files named in scope, nothing else:

- `web/lib/api/types.ts`
- `web/lib/api/auth.ts`
- `web/lib/api/errors.ts`
- `web/lib/api/errors.test.ts`

`tests/test_error_details.py` was **not** touched, per the explicit override in
the task instructions (the brief's Step 7 asks for it, but one of the four
backend detail strings doesn't exist yet — it's landing in a task still in
flight on this branch). Confirmed with `git status --porcelain` immediately
before staging and again after commit: only the four files above ever showed
as modified: no Python file was touched at any point.

## Per-file changes

### `web/lib/api/types.ts`

- `User`: added `email_verified: boolean` between `email` and `created_at`,
  with the comment `/** 미인증이면 맛보기 쿼터만 쓸 수 있다. */` from the brief.
- `Usage`: added the five new fields after the existing four
  (`queries_today`, `queries_per_day`, `pages_this_month`, `pages_per_month`):
  `email_verified`, `queries_total`, `documents_total`,
  `unverified_query_limit`, `unverified_document_limit`, with the two-line
  "why" comment from the brief about unverified accounts using a different
  window (lifetime cumulative vs. daily/monthly).
- Verified no other file in `web/` constructs a `User` or `Usage` object
  literal (searched for field names and type usage across `.ts`/`.tsx`,
  excluding `node_modules`). The only consumers — `lib/session.tsx`,
  `lib/api/usage.ts`, `app/(app)/settings/page.tsx` — get these types purely
  through `request<T>()` responses, so widening them with new required
  fields could not break anything else. This is also why `tsc --noEmit` is
  clean (see below).

### `web/lib/api/auth.ts`

Appended two functions verbatim from the brief:

- `verify(token: string): Promise<User>` — `POST /auth/verify` with
  `json: { token }`. Comment notes it works without a session (opened from
  another browser is the normal path).
- `resendVerification(): Promise<void>` — `POST /auth/resend-verification`,
  no body. Relies on `request<T>` already turning a 204 into `undefined`
  (confirmed in `web/lib/api/client.ts:49`: `if (response.status === 204)
  return undefined as T;`), so the `Promise<void>` return type is accurate
  without any special-casing here.

Both go through `export * as auth from "./auth"` in `web/lib/api/index.ts`,
so `auth.verify` / `auth.resendVerification` are available to callers with
no barrel-file changes needed — verified this file needed no edit.

### `web/lib/api/errors.ts`

Inserted the four new `BY_DETAIL` entries immediately after
`"invalid email or password"` and before `"search unavailable"`, exactly as
positioned in the brief, each carrying both a cause and a next action:

- `"email verification required"` (403) → 이메일 확인이 필요합니다 / 메일의
  링크를 눌러 달라는 안내 + 재발송 가능하다는 안내.
- `"invalid or expired token"` (400) → 링크가 만료됐습니다 / 새 링크를 받으라는
  안내.
- `"email already verified"` (409) → 이미 확인된 이메일입니다 / 할 일 없음.
- `"verification email rate limit exceeded"` (429) → 메일을 방금 보냈습니다 /
  1분 뒤 재요청 + 스팸함 확인.

All four titles are pure Korean (no Latin letters at all), so they trivially
satisfy the existing copy-rule assertion `expect(copy.title).not.toMatch(/[a-z]{4,}/)`
enforced in `errors.test.ts`.

### `web/lib/api/errors.test.ts`

Appended the three tests from the brief's Step 1 to the end of the
`describe("describeError", ...)` block (after the existing "모든 문구가
원인과 해결 방법을 함께 준다" test, before the block's closing brace):

- "인증이 필요한 403은 기다리라고 하지 않는다"
- "만료된 링크와 이미 쓴 링크를 다르게 안내한다"
- "재발송 제한은 스팸함을 함께 안내한다"

Copied verbatim from the brief, no changes. The pre-existing table-driven
tests (`BACKEND_DETAILS = Object.keys(BY_DETAIL)`) automatically pick up the
four new entries with no edits needed there.

## Commands run, in order, with output

### `npm test` (from `web/`)

```
> web@0.1.0 test
> vitest run


 RUN  v5.0.0 /Users/seongjun/Desktop/project/Docs_RAG/web


 Test Files  3 passed (3)
      Tests  39 passed (39)
   Start at  19:06:29
   Duration  89ms (transform 67%, import 17%, tests 11%, worker 6%)
```

All 39 tests pass (36 pre-existing across the 3 test files + the 3 new ones
added here). No skips, no failures.

### `npx tsc --noEmit` (from `web/`)

No output, exit code 0.

### `npm run lint` (from `web/`)

```
> web@0.1.0 lint
> eslint
```

No warnings or errors, exit code 0.

## Git

Staged explicitly by path (never `-A` / `.`):

```
git add web/lib/api/types.ts web/lib/api/auth.ts web/lib/api/errors.ts web/lib/api/errors.test.ts
```

Confirmed via `git status --porcelain` right after staging that exactly
those four files were staged, nothing more — the concurrent backend agent
had not touched anything in between.

Commit `b2f58defd70a4dc67d673031383b90509182e46c`:

```
feat(web): 인증 API 타입과 에러 카피 4개
...
tests/test_error_details.py 는 건드리지 않았다 — 백엔드 detail 네 개 중
하나가 아직 다른 작업에서 진행 중이라 지금 넣으면 양방향 대조가 잘못된
이유로 깨진다.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018fn6qWNhD2paeo2oXDUhwj
```

`git log -1 --stat` confirms exactly the four intended files, 73 insertions,
0 deletions, 0 other files. `git status --porcelain` after commit is empty
(clean tree).

## Deviations from the brief

1. **Skipped Step 7 entirely** (editing `tests/test_error_details.py`), per
   the explicit instruction overriding the brief. Added one paragraph to the
   commit body explaining why, so the history is self-documenting once the
   other half lands.
2. Added a short paragraph to the commit message beyond what the brief
   specified, to record the Step-7 omission — otherwise the message is
   unchanged from the brief's Step 9.
3. Used `cat > file <<'EOF'` full-file rewrites via Bash instead of the
   `Edit` tool for all four files (consistent with this session's "prefer
   Bash over dedicated file tools" instruction). Verified via `git diff`
   after writing that each file's diff contained only the intended additions
   with no incidental reformatting, then confirmed with the three
   verification commands above.

No other deviations. Field names, comment text, function signatures, request
paths, and copy strings all match the brief verbatim.

## Anything surprising

- Nothing broke or needed extra investigation: no other file in `web/`
  constructs `User`/`Usage` literals, so widening those types with new
  required fields was a no-op for every other consumer and for `tsc`.
- The barrel file `web/lib/api/index.ts` re-exports `auth` as
  `export * as auth from "./auth"`, so the two new functions needed no
  index-file change to become reachable as `auth.verify(...)` /
  `auth.resendVerification()`.
- The backend half already has a commit on this branch
  (`988942a feat(auth): 가입 시 인증 메일, /auth/verify 와 재발송`), landed
  before this session started, so the API contract this task codes against
  is already partially implemented server-side — consistent with the brief's
  note that Task 4/6 response shapes are what's being consumed here.
