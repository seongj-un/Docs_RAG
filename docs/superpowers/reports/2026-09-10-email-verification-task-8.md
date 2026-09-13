# Task 8 report — verify 페이지 · 배너 · 사용량 바

Status: **완료**. 커밋 2개, `feat/email-verification` 브랜치.

- `030ebdd` — feat(web): 인증 착지 페이지와 배너 (브리프 Step 1~5, 9)
- `d57155c` — fix(web): 미인증 사용량 안내 문구가 쿼터 값을 하드코딩하던 문제 (brief-audit.md I8-1 반영, 코디네이터 지시로 완료 후 추가)

---

## 파일별 변경

### `web/lib/session.tsx` (수정)

브리프 Step 1 그대로: `SessionValue`에 `refresh: () => Promise<void>` 필드 추가(주석 포함), `SessionProvider` 안에 `refresh` `useCallback`을 `signOut` 바로 위에 추가(`auth.me()`를 다시 불러 `setUser`, 실패 시 `setUser(null)`), `Provider`의 `value`에 `refresh` 추가. `useCallback`은 이미 import돼 있어 import 변경 없음.

### `web/components/VerifyBanner.tsx` (신규) / `web/components/VerifyBanner.module.css` (신규)

브리프 Step 2의 로직·문구·상태 머신(`idle | sending | sent`)을 100% 그대로 옮겼다 — 조건 분기, 텍스트, 함수 이름 모두 변경 없음. 브리프 코드에는 스타일이 없었는데(`<div role="status">` 맨몸), 이 프로젝트는 컴포넌트마다 `*.module.css`를 갖는 것이 관례라(`AppShell`, `UsageBar`, `Alert` 참조) `VerifyBanner.module.css`를 새로 만들어 `className`만 추가했다. `styles/tokens.css`의 "색상 없음" 원칙을 따라 에러 텍스트는 색 대신 `var(--text)` + `font-weight:600`으로만 강조했다(`UsageBar`의 `data-near` 패턴과 동일).

### `web/app/verify/page.tsx` (신규) / `web/app/verify/verify.module.css` (신규)

`(app)` 라우트 그룹 밖, `web/app/verify/`에 새로 만들었다. `web/app/layout.tsx`(루트 — `ThemeProvider` → `SessionProvider`만 있음)와 `web/app/(app)/layout.tsx`(로그인 안 됐으면 `/login`으로 리다이렉트하는 게이트가 있음)를 직접 읽고 배치를 결정했다: `verify/page.tsx`는 `(app)` 형제 경로라 **루트 레이아웃만** 거치고 `(app)/layout.tsx`의 인증 게이트는 거치지 않는다 — 로그인 안 한 방문자도 `SessionProvider`(재발송 버튼용 세션 컨텍스트)는 받지만 `/login`으로 튕기지 않는다. brief-audit.md도 이 지점을 독립적으로 확인했다("Confirmed correct" 절 참조).

로직은 브리프 Step 4와 동일하되 한 가지 구조를 바꿨다 — 아래 "브리프와의 차이" 참조. 스타일은 `login.module.css`와 같은 골격(`screen`/`card`/`heading`/`subtitle`)으로 `verify.module.css`를 새로 만들었고, 링크는 색 대신 밑줄+굵기로 강조했다(`.link`).

### `web/components/AppShell.tsx` (수정)

`import { VerifyBanner } from "@/components/VerifyBanner";` 추가(다른 `@/components/*` import와 알파벳 순서로 배치). `<VerifyBanner />`는 `.topbar`를 닫는 `</div>` 바로 다음, `#main`/`.content` div 바로 앞에 넣었다 — 브리프가 요구한 "crumb을 그리는 상단 바 바로 아래"와 정확히 일치. 사이드바가 아니라 본문 위에 두는 이유(좁은 화면에서 사이드바가 덮개로 바뀌어 가려짐)를 주석으로 남겼다.

### `web/app/(app)/settings/page.tsx` (수정, 2차 커밋에서 한 번 더 수정)

1차: 브리프 Step 5 그대로 — `usage.email_verified`로 전체 `<UsageBar>` 블록(라벨·숫자·refill 문구)을 통째로 스왑. 인증 계정은 기존 문구(`오늘 한 질문`/`내일 다시 채워집니다`), 미인증 계정은 새 문구(`지금까지 한 질문`/`이메일을 확인하면 하루 200개로 늘어납니다`)로 분기.

2차(`d57155c`): brief-audit.md의 I8-1 지적을 코디네이터 지시로 반영. 브리프의 미인증 분기 코드는 "하루 200개"·"한 달 1000쪽"을 리터럴 문자열로 박아뒀는데, 이 숫자들은 `QUOTA_QUERIES_PER_DAY`/`QUOTA_UPLOAD_PAGES_PER_MONTH` 환경변수 기본값이고 바로 위 인증 분기가 쓰는 `usage.queries_per_day`/`usage.pages_per_month`로 이미 같은 응답 안에 들어 있다. 리터럴을 템플릿 리터럴 보간으로 바꿨다:

```tsx
refill={`이메일을 확인하면 하루 ${usage.queries_per_day.toLocaleString()}개로 늘어납니다.`}
...
refill={`이메일을 확인하면 한 달 ${usage.pages_per_month.toLocaleString()}쪽으로 늘어납니다.`}
```

이게 이 태스크의 핵심 요구("사용량 바는 정직해야 한다")를 한 번 더 지키는 수정이라 브리프의 리터럴 그대로 두지 않고 고쳤다.

### `README.md` (수정)

`## 설정`의 환경변수 표에 8줄 추가(`MAIL_PROVIDER`, `RESEND_API_KEY`, `MAIL_FROM`, `APP_BASE_URL`, `VERIFY_TOKEN_TTL_HOURS`, `UNVERIFIED_QUOTA_QUERIES`, `UNVERIFIED_QUOTA_DOCUMENTS`, `RATE_LIMIT_VERIFY_RESEND_PER_MIN`) — 기본값은 `app/config.py`의 `Settings` 클래스에서 직접 확인해 채웠다(브리프에는 표 값이 없었다). `### 무료 티어에서 생성 모델 고르기`와 `## 테스트` 사이에 `### 이메일 인증` 절을 브리프 Step 9의 문단 그대로 삽입했다.

---

## 브리프와의 차이 (왜)

**1. `verify/page.tsx`의 토큰-없음 분기를 이펙트에서 렌더 시점 계산으로 옮겼다.**

브리프 코드 그대로 구현하면 `npm run lint`가 실패한다:

```
web/app/verify/page.tsx
  34:7  error  Error: Calling setState synchronously within an effect can trigger cascading renders
  react-hooks/set-state-in-effect
```

브리프의 `useEffect`는 토큰이 없거나 빈 문자열이면 그 안에서 곧바로 `setState({kind:"failed", ...})`를 호출한다. `auth.verify().then()/.catch()` 안의 `setState` 호출은 (비동기 콜백이라) 잡히지 않았고, 딱 이 동기 분기만 걸렸다. `AppShell.tsx`에 이미 같은 문제의 전례가 있다 — `matchMedia`를 마운트 시 동기적으로 읽어 `setCollapsed`하는 부분에 `// eslint-disable-next-line react-hooks/set-state-in-effect`가 달려 있다. 억제 대신 근본적으로 고쳤다: 토큰의 존재 여부는 `useSearchParams()`로 렌더링 시점에 이미 알 수 있으므로, `useState`의 lazy initializer로 초기 상태를 계산하고 이펙트는 오직 실제 비동기 작업(`auth.verify` 호출)만 맡는다.

```tsx
function initialState(token: string | null): State {
  if (token === null || token === "") {
    return { kind: "failed", copy: { title: "...", hint: "..." } };
  }
  return { kind: "checking" };
}
// ...
const [state, setState] = useState<State>(() => initialState(token));
// ...
useEffect(() => {
  if (started.current || token === null || token === "") return;
  started.current = true;
  auth.verify(token).then(...).catch(...);
}, [token, refresh]);
```

사용자에게 보이는 동작·문구·타이밍은 브리프와 동일하다(빈/누락 토큰이면 즉시 "링크가 올바르지 않습니다" 화면). `started.current`가 유효하지 않은 토큰에서는 계속 `false`로 남지만, 그 경로는 부작용이 없는 no-op이라 무해하다.

**2. CSS 모듈을 브리프에 없던 파일로 추가했다** (`VerifyBanner.module.css`, `web/app/verify/verify.module.css`). 브리프의 JSX 스니펫은 클래스 없는 맨 마크업이었지만, 태스크의 전역 제약("이 프로젝트는 컴포넌트 라이브러리를 쓰지 않는다 — 기존 컴포넌트 구조와 CSS 모듈 관례를 따를 것")과 실제 코드베이스 전체(모든 컴포넌트·페이지가 짝을 이루는 `*.module.css`를 가짐)를 따랐다. 로직·문구·구조(어떤 조건에서 무엇이 렌더되는지)는 건드리지 않고 `className`만 추가했다.

**3. I8-1 반영으로 `settings/page.tsx`의 미인증 refill 문구를 브리프의 리터럴에서 보간식으로 바꿨다** — 위 "파일별 변경" 절 참조. 코디네이터가 brief-audit.md의 지적을 명시적으로 지시했다.

**4. Step 10(`pytest tests/ -v && npm test` 통합 실행)을 실행하지 않았다.** 태스크 지시(오케스트레이터 프롬프트)가 "web/에서 실행: `npx tsc --noEmit` / `npm run lint` / `npm test`"로 검증 범위를 명시적으로 좁혔고, 이 브랜치에서 Python 파일은 다른 에이전트가 동시에 커밋 중이었다. `pytest`는 내 파일 목록 밖이고 실행할 필요도 없었다 — 실제로 코디네이터가 나중에 "`tests/test_error_details.py`는 이제 초록(커밋 `900ff81`, 159/159 통과)"이라고 확인해줬다.

---

## 실행한 검증 명령과 결과 (전부 `web/`에서)

### `npx tsc --noEmit`

두 번 실행(1차 커밋 전, I8-1 수정 후) 모두 출력 없음 — 에러 없음.

### `npm run lint`

1차 실행에서 위에 적은 `react-hooks/set-state-in-effect` 에러 1건 발견 → 수정 → 재실행:

```
> web@0.1.0 lint
> eslint

```
(출력 없음, 종료 코드 0)

I8-1 수정 후 다시 실행해도 동일하게 깨끗함.

### `npm test`

```
> web@0.1.0 test
> vitest run

 RUN  v5.0.0 /Users/seongjun/Desktop/project/Docs_RAG/web

 Test Files  3 passed (3)
      Tests  39 passed (39)
```

기존 3개 파일(`lib/api/errors.test.ts`, `lib/footnotes.test.ts`, `lib/sse.test.ts`)만 존재 — `vitest.config.mts`가 `include: ["lib/**/*.test.ts"]`, `environment: "node"`로 고정돼 있어 `.tsx` 컴포넌트나 `app/`, `components/` 아래는 애초에 수집 대상이 아니고 jsdom/RTL도 설치돼 있지 않다. 그래서 `VerifyBanner`·`verify/page.tsx`·`session.tsx`의 `refresh`에 대한 렌더링 테스트를 새로 추가하지 않았다 — 추가해도 이 설정에서는 실행되지 않고, 프로젝트 전체에 페이지/컴포넌트 렌더링 테스트가 하나도 없다(`login/page.tsx`도 없음)는 기존 관례와도 맞다. brief-audit.md의 M8-2도 같은 결론이었다.

### 추가로 실행: `npm run build` (필수 항목은 아니었으나 직접 확인)

Next 16 문서(`node_modules/next/dist/docs/01-app/03-api-reference/04-functions/use-search-params.md`)에 "프로덕션 빌드에서 `useSearchParams`를 쓰는 정적 페이지는 `Suspense`로 감싸지 않으면 빌드가 실패한다"는 경고가 있어, `dev`/`vitest`가 못 잡는 바로 그 실패 모드를 직접 확인하려고 돌렸다.

```
✓ Compiled successfully in 1039ms
✓ Generating static pages using 9 workers (8/8) in 73ms

Route (app)
├ ○ /
├ ○ /_not-found
├ ○ /design
├ ○ /documents
├ ƒ /documents/[id]
├ ○ /login
├ ○ /settings
└ ○ /verify
```

`/verify`가 `○`(정적 프리렌더)로 나와 `Suspense` 경계가 제대로 동작함을 확인했다.

---

## 손으로 못 돌려본 것 — 브리프 Step 8

Step 8의 수동 e2e 절차(가입 → 서버 로그의 `[mail]` 링크 복사 → 로그인 안 한 시크릿 창에서 열기 → 배너가 사라지는지 → 같은 링크 재사용 시 409 → 설정 화면 사용량 바 문구 확인)는 실행하지 않았다. 이유: `GET /usage`가 아직 5개 신규 필드(`email_verified`, `queries_total`, `documents_total`, `unverified_query_limit`, `unverified_document_limit`)를 내려주지 않는 상태로 작업을 시작했고(Task 6이 병렬로 그 작업 중이었음 — 완료 후 커밋 로그에 `7e25c39 feat(usage): 미인증 계정의 한도를 사용량 응답에 싣는다`로 확인됨), 오케스트레이터 지시가 이 수동 워크스루를 명시적으로 보류하라고 했다.

Task 6이 이제 랜딩됐으니, 사람이 한 번 돌려봐야 할 순서는 브리프 Step 8 그대로다:

1. 터미널 3개: `docker compose up -d db`, `.venv/bin/python -m uvicorn app.main:app --reload`, `cd web && npm run dev:webpack` (이 맥에서 Turbopack이 포트를 안 잡는다는 브리프의 경고에 따라 `dev:webpack` 사용 — `next build`는 Turbopack으로 성공했지만 `next dev --turbopack`은 별개로, 검증하지 않았다).
2. `http://localhost:3000/login`에서 새 계정 가입 → 앱 진입 후 상단에 `VerifyBanner`가 보이는지, "질문과 업로드가 몇 번으로 제한됩니다" 문구인지.
3. 백엔드 로그의 `[mail]` 줄에서 링크 복사(`MAIL_PROVIDER=console` 기본값).
4. **로그인하지 않은 별도 브라우저(시크릿 창)**에 붙여넣기 → "이메일 확인이 끝났습니다" 화면이 뜨는지 — 이게 이 태스크의 핵심 요구사항이라 특히 중요하다.
5. 원래 창 새로고침 → 배너가 사라지는지(`refresh()`가 `/auth/me`를 다시 불러 `email_verified: true`를 받아오는지).
6. 같은 링크 재방문 → "이미 확인된 이메일입니다" 문구(409 `email already verified`).
7. 설정 화면: 인증 전에는 "지금까지 한 질문 N / 5개 · 이메일을 확인하면 하루 200개로 늘어납니다"(또는 실제 `QUOTA_QUERIES_PER_DAY` 값), 인증 후에는 "오늘 한 질문 0 / 200개 · 내일 다시 채워집니다"로 바뀌는지 — 특히 숫자가 하드코딩이 아니라 실제 서버 값과 일치하는지(이번에 고친 부분).
8. 만료·위조 토큰 케이스(400, 사유 구분 불가)와 미인증 5회/1개 한도 초과(403) 케이스는 이 태스크 파일 밖(백엔드) 이지만, `/verify` 페이지가 그 두 에러의 `describeError` 카피(`링크가 만료됐습니다`, `이메일 확인이 필요합니다` 등은 각각 다른 화면 — verify 페이지는 토큰 오류만 봄)를 제대로 보여주는지는 여기서 같이 확인하면 좋다.

---

## brief-audit.md 대응 정리

코디네이터가 감사 결과를 보내오기 전에 `.superpowers/sdd/brief-audit.md`가 이미 디스크에 있는 것을 발견해 Task 8 관련 부분을 먼저 읽었고, 뒤이어 코디네이터의 메시지로 확인·지시받았다.

- **C8-1** (`test_error_details.py`의 `DETAIL_MAPPED` 4줄 누락) — Task 8 파일 밖의 문제(브리프의 Files 목록에 그 파일이 없다)라 손대지 않았다. 코디네이터 확인: 다른 에이전트가 커밋 `900ff81`로 이미 고쳤고 `pytest` 159/159 통과.
- **I8-1** (미인증 refill 문구 하드코딩) — 위에서 수정, 커밋 `d57155c`.
- **M8-1** (Interfaces 절이 Task 6을 명시적 의존성으로 안 적음) — 브리프 메타데이터 표기 문제일 뿐 코드에 영향 없음. `Usage` 타입은 이미 5개 신규 필드를 갖고 있어(Task 7이 landing) 그대로 빌드했다. 코디네이터도 "Build against the Usage type as planned — it already has them"으로 확인.
- **M8-2** (신규 로직에 대한 자동 테스트 없음) — 위 "실행한 검증 명령" 절에서 설명한 대로 `vitest.config.mts`의 범위·기존 관례와 일치해 추가하지 않았다. 감사 결과와 내 판단이 독립적으로 일치했다.

---

## 그 밖에 특이했던 점

- `web/CLAUDE.md` → `web/AGENTS.md`가 "이 Next.js는 학습 데이터와 다를 수 있다, 코드 작성 전에 `node_modules/next/dist/docs/`를 읽어라"고 지시해서 실제로 `use-search-params.md`와 `upgrading/version-16.md`를 읽었다. 둘 다 이번 구현과 충돌하는 내용은 없었다(`useSearchParams`+`Suspense` 패턴은 문서 예제와 정확히 같은 모양이고, v16 breaking change 목록 중 이 파일들에 해당하는 항목은 없음 — 우리는 `searchParams` prop이 아니라 `useSearchParams()` 훅을 쓰는 클라이언트 컴포넌트라 async params 관련 변경과 무관). 다만 이 점검이 없었다면 놓쳤을 수도 있는 부분이라 기록해 둔다.
- 브리프가 완료 기준 #9로 요구하는 "`pytest`와 `npm test`가 전부 통과한다"는 착수 시점에는 거짓이었다(brief-audit.md C5-3/C8-1). 태스크 경계를 넘는 문제라 내가 고칠 파일이 아니었고, 실제로 다른 에이전트가 고쳤다 — 병렬 작업 특유의 위험(브리프의 완료 기준이 다른 태스크의 완료에 암묵적으로 의존)이 실제로 발생한 사례였다.
- `git status`를 여러 번 찍어보며 관찰한 바, 같은 세션 동안 Python 쪽 에이전트가 최소 2세트의 커밋을 만들었다(`59b07f3`, `900ff81`, `7e25c39`). 매번 `git add`는 내가 만진 경로만 명시적으로 지정했고, 커밋 직전·직후 `git status --porcelain`/`git diff --cached --stat`로 대상 파일 집합을 재확인했다 — 겹친 파일은 한 번도 없었다.

---

## 수정: 리뷰 지적 4건

Status: **완료**. 커밋 1개, `feat/email-verification` 브랜치, 파일 1개(`web/app/verify/page.tsx`)만 수정.

- `6213121` — fix(web): 인증 착지 페이지의 리뷰 지적 4건 — 재발송 이중 클릭, 막다른 화면, 접근성, 거짓 문구

4건 모두 `web/app/verify/page.tsx` 안에 있었고, 기준은 두 파일: `web/components/VerifyBanner.tsx`(이 문제들을 이미 올바르게 푼 배너)와 `web/app/login/page.tsx`(같은 submitting-guard 관례). 새 추상화는 만들지 않고 두 파일의 기존 패턴을 그대로 옮겼다.

### Finding 1 (Important) — 재발송 버튼 이중 제출

`ResendOrSignIn`의 `sent: boolean`을 `VerifyBanner.tsx`와 같은 `idle | sending | sent` 3상태 머신으로 바꿨다. `onResend`가 시작하자마자 `setState("sending")`, 성공하면 `"sent"`, 실패하면 에러를 채우고 `"idle"`로 되돌린다. 버튼은 `disabled={state === "sending"}`, 라벨은 `state === "sending" ? "보내는 중…" : "새 링크 받기"` — `VerifyBanner.tsx`의 조건·문구를 그대로 재사용했다.

### Finding 2 (Minor) — 재발송 성공 후 막다른 화면

`state === "sent"` 분기가 안내 문장 하나로 끝나던 것을, 같은 파일의 다른 종료 분기(인증 성공·미로그인·이미인증)와 맞춰 `<Link href="/">돌아가기</Link>`를 추가했다(이미 로그인된 사용자이므로 `/login`이 아니라 `/`).

### Finding 3 (Minor) — 실패 문구가 스크린 리더에 전달 안 됨

`VerifyBanner.tsx`는 컨테이너 `div` 하나에 `role="status"`를 붙여 안의 메시지·에러 텍스트 변화를 알린다. 이번엔 컨테이너 전체가 아니라 텍스트를 담은 두 엘리먼트에 직접 붙였다 — `<h1>`까지 같이 감싸면 heading의 암묵적 role이 status로 덮여 헤딩 내비게이션에서 사라지는 부작용이 생기기 때문이다:

- `VerifyInner`의 실패 힌트(`state.copy.hint`) — 토큰 검증이 끝난 뒤 비동기로 나타나는 텍스트라 `role="status"` 없이는 "확인하고 있습니다…"에서 뭐가 바뀌었는지 전달되지 않는다.
- `ResendOrSignIn`의 재발송 인라인 에러(`error`) — 리뷰가 정확히 지목한 경로.

### Finding 4 (Minor, but it matters here) — 성공 문구의 거짓 주장

"이제 한도 없이 질문하고 문서를 올릴 수 있습니다."를 "이제 맛보기 쿼터가 풀려 평소 한도로 질문하고 문서를 올릴 수 있습니다."로 바꿨다. 인증 후에도 하루 질문·월간 업로드 쪽수 한도(`QUOTA_QUERIES_PER_DAY`/`QUOTA_UPLOAD_PAGES_PER_MONTH`, 기본 200/1000)는 그대로 적용된다 — 없어지는 건 인증 전 계정에 걸린 "맛보기 쿼터"(`lib/api/types.ts:10`의 표현을 그대로 재사용)뿐이다. 지시대로 숫자는 API에서 새로 읽어오지 않고, 숫자 없이 참인 문장으로만 고쳤다 — 이 브랜치에서 settings 화면 사용량 바가 하드코딩된 200/1000을 걷어내고 API 값을 읽도록 고쳤던 것(위 "파일별 변경 > settings/page.tsx" 절)과 같은 이유다.

---

## 실행한 검증 명령과 결과 (전부 `web/`에서, 이번 수정분)

### `npx tsc --noEmit`

출력 없음, 종료 코드 0.

### `npm run lint`

```
> web@0.1.0 lint
> eslint

```
(출력 없음, 종료 코드 0)

### `npm test`

```
> web@0.1.0 test
> vitest run

 Test Files  3 passed (3)
      Tests  39 passed (39)
```

`vitest.config.mts`가 `include: ["lib/**/*.test.ts"]`로 고정돼 있어 `app/verify/page.tsx`는 수집 대상이 아니다 — 이 39개는 기존 `lib/` 테스트 그대로이고, 이번 4건 수정 중 어느 것도 검증하지 않는다. **이 통과는 이번 변경에 대해 아무것도 증명하지 않는다.** 그래서 아래에 사람이 직접 확인할 절차를 적는다.

### `npm run build`

성공. `/verify`가 여전히 `○`(정적 프리렌더)로 나와, `Suspense` 경계와 once-only 가드(`useRef`)를 건드리지 않았다는 것도 같이 확인됐다.

---

## 사람이 직접 확인할 절차 (자동 테스트가 못 보는 부분)

사전 준비는 기존 Step 8 절차와 같다: `docker compose up -d db`, 백엔드(`uvicorn app.main:app --reload`), `cd web && npm run dev:webpack`. 계정 하나를 가입해 로그인된 상태로 시작한다(재발송 버튼은 세션이 있어야 나타난다 — `user === null`이면 로그인 링크만 보인다).

**Finding 1 — 이중 제출 가드.** `/verify?token=아무값`처럼 유효하지 않은 토큰으로 열어 실패 화면(재발송 버튼 포함)을 띄운다. DevTools Network 탭에서 "Preserve log"를 켠 뒤:
1. "새 링크 받기"를 클릭 → 라벨이 즉시 "보내는 중…"으로 바뀌고 버튼이 disabled 되는지 확인.
2. 응답 전에 같은 버튼을 다시 클릭(또는 Enter 연타)해도 Network 탭에 `POST /auth/resend-verification`이 **한 번만** 찍히는지 확인 — disabled 버튼은 클릭 이벤트를 아예 발생시키지 않으므로 두 번째 요청이 없어야 한다(수정 전에는 두 번 찍혔다). Elements 탭에서 버튼에 `disabled` 속성이 잠깐 붙는 것도 같이 보인다.
3. 응답 후 라벨이 원래대로 돌아오거나(실패 시) 성공 화면으로 바뀌는지(성공 시) 확인.

**Finding 2 — 재발송 성공 후 링크.** 재발송이 성공하면 "링크를 다시 보냈습니다..." 아래 "돌아가기" 링크가 보이고, 클릭 시 `/`로 이동하는지 확인.

**Finding 3 — 스크린 리더 announce.** Chrome DevTools의 Elements → Accessibility 탭(또는 macOS VoiceOver, Cmd+F5)으로 확인:
1. 실패 화면의 힌트 `<p>`에 `role="status"`가 잡히는지.
2. 재발송을 실패시켰을 때(예: 분당 제한에 이미 걸린 상태에서 재클릭) 인라인 에러 `<p>`가 나타나는 순간 VoiceOver가 자동으로 읽는지 — role 없이는 화면 변화가 조용히 지나간다.

**Finding 4 — 성공 문구.** 유효한 인증 링크(백엔드 로그의 `[mail]` 줄, `MAIL_PROVIDER=console` 기본값)를 로그인하지 않은 창에 열어 "이메일 확인이 끝났습니다" 화면 문구가 "이제 맛보기 쿼터가 풀려 평소 한도로 질문하고 문서를 올릴 수 있습니다."로 보이고 "한도 없이"·"무제한" 표현이 없는지 확인. `/settings`로 이동해 사용량 바가 인증 후 문구로 바뀌어 이 성공 문구와 모순되지 않는지도 같이 보면 좋다.

---

## 그 밖에 특이했던 점

- `web/AGENTS.md`의 "이 Next.js는 학습 데이터와 다를 수 있다" 지시는 이번에도 그대로 있었다. 이번 변경은 React 상태·JSX·CSS 클래스만 건드리고 Next.js API를 새로 쓰지 않아 `node_modules/next/dist/docs/`를 다시 참조할 필요가 없었다 — 지난 태스크에서 이미 관련 문서를 확인해 충돌 없음을 기록해 뒀다(위 "그 밖에 특이했던 점" 첫 항목).
- 작업 내내 `git status --porcelain`으로 다른(Python) 에이전트의 동시 커밋 여부를 확인했다 — 이번 세션에서는 겹치는 변경이 없었고, `git add`도 `web/app/verify/page.tsx` 한 파일만 경로를 명시해 스테이징했다.
