# 이메일 인증 — 설계

M3에서 만든 인증(argon2id + 서버측 세션 쿠키)에 이메일 소유 확인을 얹는다.
이 문서는 결정과 그 근거만 적는다. 기존 인증 구조는 `app/services/auth.py`가
원본이고 여기서 다시 설명하지 않는다.

## 무엇을 풀려는 것인가

**쿼터 어뷰즈 차단.** M3 Phase 2가 세운 쿼터(200질의/일 · 1000쪽/월)는
계정 단위인데, 지금은 아무 문자열이나 이메일 자리에 넣고 가입이 된다.
즉 쿼터가 **재가입 한 번으로 초기화되는 장식**이다. 도달 가능한 주소를
가진 계정만 정상 쿼터를 받게 하는 것이 목적이다.

부차적으로 포트폴리오 완성도 — 프로덕션급 인증 플로우의 마지막 조각.

**범위 밖:** 비밀번호 재설정. 인증 토큰과 재설정 토큰은 수명·권한·실패
모드가 달라서 같은 테이블에 넣으면 둘 다 어정쩡해진다. 필요해지면 별도
설계로 다룬다.

## 확정된 결정

| 항목 | 결정 | 근거 |
|---|---|---|
| 토큰 저장 | DB 테이블 (`email_verification_tokens`) | 아래 "왜 stateless 토큰이 아닌가" |
| 토큰 해시 | sha256 | 아래 "왜 argon2가 아닌가" |
| 인증 플래그 | `users.email_verified_at TIMESTAMPTZ NULL` | boolean은 "언제"를 버린다 |
| 미인증 정책 | 맛보기 쿼터 (질의 5 · 문서 1, **누적**) | 아래 "왜 누적인가" |
| 기존 계정 | 마이그레이션에서 전부 verified 처리 | 아래 "기존 계정" |
| 발송 | Resend HTTP API + 콘솔 폴백 | SMTP 포트가 막힌 환경에서도 동작, 키 없이 clone 가능 |
| 발송 시점 | `BackgroundTasks` (응답 밖) | 제공자 지연이 가입 응답을 막으면 안 된다 |
| 검증 요청 | `POST /auth/verify` (GET 링크 아님) | 아래 "왜 POST인가" |
| 재발송 입력 | 세션 (이메일 주소 아님) | 주소를 받으면 계정 열거 창구가 된다 |
| 토큰 수명 | 24시간 | |

### 왜 stateless 토큰이 아닌가

HMAC 서명 토큰(`itsdangerous`)이면 테이블이 필요 없다. 기각한 이유는
이 프로젝트가 세션에서 이미 같은 선택을 했기 때문이다 — `services/auth.py`
독스트링이 "쿠키는 행 id만 나르므로 로그아웃 = 삭제 = 즉시 무효화"라고
적고 JWT를 기각했다. 서명 토큰은 발급한 링크를 취소할 수 없고, 시크릿을
돌리면 미처리 링크가 전부 죽는다. 같은 논리를 한 곳에서만 지키면 그건
논리가 아니라 우연이다.

### 왜 argon2가 아닌가

토큰은 `secrets.token_urlsafe(32)` = 256비트 난수다. 사용자가 고른
비밀번호와 달리 사전 공격 대상이 아니므로 느린 해시가 방어하는 것이 없다.
반대로 argon2는 솔트가 매번 달라 **인덱스 조회가 불가능**해진다 — 검증
때마다 미소비 토큰 전체를 훑어 하나씩 verify 해야 한다. "DB가 유출돼도
링크를 만들 수 없다"는 목적은 sha256으로 이미 달성된다.

### 왜 누적인가

기존 쿼터는 전부 "오늘 / 이번 달" 창이다(`services/usage.py`의
`_start_of_day` / `_start_of_month`). 맛보기 한도를 같은 방식으로 "하루
5회"로 두면 **미인증 계정이 매일 5회씩 영원히 쓴다** — 막으려던 어뷰즈가
그대로 통과한다.

그래서 미인증 한도만 계정 수명 전체 누적으로 센다. 문서도 "현재 소유
개수"가 아니라 누적 업로드 수다. 올리고 지우면 초기화되는 카운터는
카운터가 아니다.

### 기존 계정

마이그레이션 `0008`이 기존 행을 전부 `email_verified_at = now()`로 채운다.
M3의 마이그레이션 `0003`이 만든 시드 유저는 **로그인이 불가능한 계정**이라
(`UNUSABLE_PASSWORD_HASH`) 인증 자체를 할 방법이 없고, eval 코퍼스가 그
계정에 묶여 있다. 미인증으로 두면 되살릴 경로 없이 잠긴다. 게이트는 새
가입부터 적용된다.

### 왜 POST인가

메일 클라이언트와 보안 스캐너가 본문 링크를 미리 GET으로 밟는다. 링크가
곧 검증 엔드포인트면 사용자가 클릭하기 전에 1회용 토큰이 소진되고, 사용자
화면에는 "이미 사용된 링크"가 뜬다. 메일 링크는 프론트 `/verify?token=…`
페이지를 가리키고, 그 페이지가 POST를 친다.

## 데이터 모델 (마이그레이션 `0008`)

```
users
  + email_verified_at  TIMESTAMPTZ NULL      -- NULL = 미인증

email_verification_tokens
  id           UUID PK
  user_id      UUID FK users(id) ON DELETE CASCADE
  token_hash   TEXT UNIQUE                   -- sha256(원문)
  expires_at   TIMESTAMPTZ NOT NULL
  consumed_at  TIMESTAMPTZ NULL
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
  INDEX (user_id)
```

토큰의 생애는 셋으로 갈린다. **소비된** 토큰은 `consumed_at`을 찍고 행을
남긴다 — 남겨야 두 번째 클릭에 "만료됐다"가 아니라 "이미 인증하셨다"고
말할 수 있다. **미소비** 토큰은 재발송 때 지운다. 살아 있는 링크가 둘이면
어느 쪽이 유효한지 사용자가 알 수 없다. **만료된** 토큰은 그대로 두고
검증 시점에 `expires_at`으로 거른다.

그래서 `POST /auth/verify`의 분기는 이렇게 읽힌다:

| 토큰 상태 | 응답 |
|---|---|
| 미소비 + 유효 | 200, `email_verified_at` 기록 |
| `consumed_at` 있음 | 409 `email already verified` |
| 만료 / 해시 불일치 / 없음 | 400 `invalid or expired token` |

"해시 불일치"와 "만료"를 같은 응답으로 묶는 것은 의도된 것이다. 둘을
나누면 임의의 문자열을 던져 "그런 토큰은 없다"와 "있었는데 만료됐다"를
구분할 수 있게 되고, 그건 토큰 존재 여부를 묻는 창구가 된다.

## 발송 계층 — `app/services/mailer.py`

프로토콜 하나에 어댑터 둘.

- `ResendMailer` — `POST https://api.resend.com/emails` (httpx)
- `ConsoleMailer` — 링크를 로그로. 테스트와 오프라인 개발용

`RESEND_API_KEY`가 비면 자동으로 콘솔 폴백. 키 없이 clone 해도 앱이 뜨고
테스트가 돌아야 한다.

발송은 `BackgroundTasks`로 응답 밖에서 한다. **대가:** 사용자는 발송
실패를 알 수 없다. 재발송 엔드포인트가 그 유일한 복구 경로이므로 배너에서
항상 닿을 수 있어야 한다.

### 도메인 제약 (배포 시점)

도메인 없이 Resend를 쓰면 발신은 `onboarding@resend.dev`로 고정되고
**본인 주소로만 전달된다.** 제3자가 가입해서 인증을 마치려면 도메인이
필요하다. 다만 이건 코드가 아니라 `MAIL_FROM` 한 줄이다 —
`onboarding@resend.dev`(개발) → `no-reply@<도메인>`(공개). 구현을 막지
않으므로 지금 만들고, 공개 시점에 DNS(SPF/DKIM)를 붙인다.

### 설정

```
MAIL_PROVIDER=resend|console      # 기본 console
RESEND_API_KEY=
MAIL_FROM=onboarding@resend.dev
APP_BASE_URL=http://localhost:3000   # 링크 조립
VERIFY_TOKEN_TTL_HOURS=24
UNVERIFIED_QUOTA_QUERIES=5
UNVERIFIED_QUOTA_DOCUMENTS=1
RATE_LIMIT_VERIFY_RESEND_PER_MIN=1
```

## API

| 엔드포인트 | 동작 |
|---|---|
| `POST /auth/signup` | 기존 그대로 + 토큰 발급 + 백그라운드 발송 |
| `POST /auth/verify` `{token}` | 200 `UserOut`. 실패 400 `invalid or expired token`, 409 `email already verified` |
| `POST /auth/resend-verification` | 세션 필요. 분당 1통. 409 / 429 |
| `GET /auth/me` | `UserOut`에 `email_verified: bool` 추가 |

## 게이트

적용 지점 두 곳:

- `services/pipeline.py` `enforce_limits()` — 기존 쿼터 검사 옆
- `routers/documents.py:107` — 업로드 쿼터 검사 옆

새 헬퍼는 `services/usage.py`에 둔다: `queries_total()`, `documents_total()`
— 기존 집계에서 시간 창(`created_at >= …`)만 뺀 형태다.

셀 자체는 `usage_events`를 그대로 쓴다. 새 카운터가 필요 없는 이유는
그 테이블이 이미 원하는 성질을 갖고 있기 때문이다: `UsageEvent`의 외래키는
`documents`가 아니라 `users`를 향하므로 **문서를 지워도 ingest 행은 남는다.**
올리고 지워서 한도를 되돌리는 우회가 구조적으로 막혀 있다. `documents`를
세었다면 그 우회가 열린다.

응답은 **403 `email verification required`.** 429가 아닌 이유는 기다려서
풀리지 않기 때문이다 — 사용자가 행동해야 한다. 429로 두면 프론트가
"잠시 뒤에 다시"라는 틀린 조언을 한다.

`GET /usage`는 미인증이면 맛보기 한도를 내려보낸다. 안 그러면 설정 화면의
사용량 바가 200을 분모로 그려서 거짓말을 한다.

## 프론트엔드

### `web/app/verify/page.tsx`

`(app)` 그룹 **밖**에 둔다. 다른 브라우저나 로그아웃 상태에서 메일 링크를
여는 것이 정상 경로인데, `(app)` 안이면 로그인 리다이렉트에 걸려 토큰이
유실된다. 상태 셋: 확인 중 / 성공(→ 앱) / 실패(재발송 버튼, 세션이 없으면
로그인 안내).

### 미인증 배너

`AppShell` 상단. `user.email_verified === false`일 때 "이메일을 확인해
주세요 · 다시 보내기". 사이드바가 화면마다 다르지 않다는 기존 원칙대로
셸이 직접 그린다.

### 에러 카피 — `web/lib/api/errors.ts`

`tests/test_error_details.py`가 백엔드 detail 문자열과 이 표를 대조한다.
누락은 **조용히 실패한다** — 에러가 아니라 사용자가 틀린 조언을 받는다.
detail을 추가하면 그 테스트의 `DETAIL_MAPPED`에도 넣는다.

| detail | 상태 | 제목 / 안내 |
|---|---|---|
| `email verification required` | 403 | 이메일 확인이 필요합니다 / 가입할 때 보낸 메일의 링크를 눌러 주세요. 안 왔다면 다시 보낼 수 있습니다. |
| `invalid or expired token` | 400 | 링크가 만료됐습니다 / 24시간이 지나면 링크가 닫힙니다. 새 링크를 받아 주세요. |
| `email already verified` | 409 | 이미 확인된 이메일입니다 / 그대로 사용하시면 됩니다. |
| `verification email rate limit exceeded` | 429 | 메일을 방금 보냈습니다 / 1분 뒤에 다시 요청해 주세요. 스팸함도 확인해 보세요. |

### 가입 직후

지금처럼 세션을 발급하고 바로 앱으로 들여보낸다. 맛보기 쿼터를 고른 이유가
가입 직후 이탈을 막는 것이므로, "메일을 확인하세요" 화면에 가두면 그 선택이
무의미해진다.

## 테스트

pytest:

- 토큰 원문이 DB에 없고 해시만 있다
- 만료 토큰 거부 / 소비된 토큰 재사용 거부 / 재발송이 이전 토큰을 무효화
- 미인증: 6번째 질의 403, 2번째 업로드 403
- **올렸다 지워도 카운트가 줄지 않는다** (누적이 실제로 누적인지)
- 인증 후 정상 쿼터로 복귀
- 마이그레이션 후 기존 계정이 verified
- `ConsoleMailer`로 돌며 Resend를 부르지 않는다
- `test_error_details.py`가 새 detail 4개를 통과

vitest:

- `describeError`가 새 detail 넷을 각각 다른 카피로 옮긴다
