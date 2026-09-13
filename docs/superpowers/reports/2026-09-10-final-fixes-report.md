# 최종 프리머지 리뷰 4건 — 수정 리포트

브랜치 `feat/email-verification`. `docs/superpowers/specs/2026-09-10-email-verification-design.md`
를 먼저 읽고 시작했다. 커밋 4개, 각 Finding당 하나.

| Finding | 심각도 | 커밋 | 상태 |
|---|---|---|---|
| 1. 미인증 업로드가 쪽수를 안 봄 | Critical | `a0a6c58` | 완료 |
| 2. 테스트가 실제 메일을 보낼 수 있음 | Important | `ce591f7` | 완료 |
| 3. 배포 문서에 필수 설정 2개 누락 | Important | `fd7e3d9` | 완료 |
| 4. 토큰 해시 테스트가 알고리즘을 안 고정 | Minor | `f5d83fb` | 완료 |

## 시작 전 측정 (사용자 주장 검증)

```
$ .venv/bin/python -m pytest tests/ -q
161 passed, 6 warnings in 11.92s

$ npm --prefix web test -- --run
Test Files  3 passed (3) / Tests  39 passed (39)

$ npm --prefix web run lint
(출력 없음 — 클린)

$ npm --prefix web run build   # next build, tsc 포함
✓ Compiled successfully / Running TypeScript ... Finished TypeScript in 581ms
```

161 / 39 / clean / clean — 주장 그대로. `.env` 에 `MAIL_PROVIDER` 줄 자체가
없고 `RESEND_API_KEY` 도 비어 있음을 먼저 확인했다(Finding 2 작업 전 안전
확인 — 값 자체는 출력하지 않았다).

---

## Finding 1 (Critical) — 미인증 업로드가 문서 개수만 세고 쪽수를 안 봄

### 문제

`unverified_upload_exceeded` 는 이력(과거에 수락된 업로드 행 수)만 본다.
미인증 계정의 **첫** 업로드는 이력이 0이라 무조건 통과하고, 그 뒤
`max_upload_pages=500` 절대 상한과 `quota_upload_pages_per_month=1000`
월간 한도만 체크됐다 — 둘 다 미인증 여부와 무관하게 넉넉했다. 업로드
레이트리밋(분당 5)의 check-then-act 경쟁까지 더하면 스크립트 하나로
계정 하나에서 최대 2,500쪽(인증 계정 월간 전체 한도의 2.5배)을 공짜로
임베딩할 수 있었다.

### 변경 파일

- **`app/config.py`** — `unverified_quota_pages: int = 50` 추가.
  `unverified_quota_documents` 바로 아래, 같은 관례(왜 필요한지 한국어
  주석, `0`이면 게이트를 끈다).
- **`.env.example`** — `UNVERIFIED_QUOTA_PAGES=50` 추가, 같은 자리.
- **`app/services/usage.py`** — 두 헬퍼 추가.
  - `pages_uploaded_total(session, user_id)`: `documents_total` 바로
    아래. `kind == "upload"` 행의 `pages` 합 (계정 수명 전체, `ingest`
    가 아니라 `upload` — 이유는 `documents_total` 과 동일: 색인 성공
    전에는 안 세고, 실패한 업로드도 빠지면 안 된다).
  - `unverified_pages_exceeded(session, user_id, incoming_pages=0)`:
    `unverified_upload_exceeded` 바로 아래. `upload_quota_exceeded` 와
    같은 모양(이력 + 들어오는 쪽수 합이 한도를 넘는지) — 문서 카운터와
    달리 첫 업로드 자체만으로 한도를 넘을 수 있어야 하기 때문.
- **`app/routers/documents.py`** —
  - `pages` 계산 직후, 절대 상한(`max_upload_pages`) 체크와 월간 쿼터
    체크 사이에 `unverified_pages_exceeded` 체크를 끼워 넣었다(미인증
    계정만). 파일을 다시 파싱하지 않고 이미 계산된 `pages` 변수를
    재사용한다. 거절되는 업로드는 여기까지 온 비용(파일 읽기 + PDF
    파싱) 이상을 추가로 쓰지 않는다.
  - `ingest.usage.record(session, user.id, "upload")` 호출에
    `pages=pages or 0` 을 추가했다. 이게 없으면 `unverified_pages_exceeded`
    가 볼 이력이 영원히 0으로 남는다 — 새 게이트가 실제로는 아무것도
    못 막는 상태로 조용히 죽는다.
- **`app/models.py`** — `UsageEvent.pages` 컬럼 주석을 "ingest 이벤트만"
  에서 "ingest 또는 upload 이벤트, 각자 자기 kind 로 필터해서 합산"으로
  갱신(내 변경이 그 주석을 부정확하게 만들었으므로).
- **`tests/test_unverified_gate.py`** — 서비스 레벨 테스트 4개:
  첫 업로드 초과 거부(+문서 게이트는 그걸 안 막는다는 것도 같이 확인),
  경계값(정확히 한도면 안 걸림), 인증 계정은 정상 쿼터를 쓴다, `0`이면
  게이트가 꺼진다.
- **`tests/test_verification_api.py`** — HTTP 레벨 테스트 1개
  (`test_a_single_oversized_upload_is_refused_even_as_the_first_document`).
  `BROKEN_PDF` 는 쪽수가 없어 이 경로를 못 타므로, `pymupdf` 로 실제
  파싱되는 51쪽 PDF 를 만드는 헬퍼 `_multi_page_pdf` 를 추가했다.
  `eval/pdf.py` 의 기존 패턴을 따라 폰트는 `"china-s"` 가 아니라
  `"korea"`(한글 글리프가 안 지워지는 쪽).
- **`docs/superpowers/specs/2026-09-10-email-verification-design.md`**
  — "알려진 한계" 절을 문서 개수 단위("최대 5개")에서 쪽수 단위로
  바꿔 실제 비용을 적었다. 아래 "스펙 갱신" 참조.

### 사이드이펙트 확인 (스캔 결과)

`UsageEvent.pages` 를 합산하는 곳은 코드베이스 전체에 두 곳뿐이었다:
`usage.py`(`kind=="ingest"` 로만 필터) 와 `scripts/cost_report.py`
(`kind!="upload"` 로 업로드를 아예 제외 — "업로드 수락은 비용이 0"이라는
기존 주석 그대로). 즉 `upload` 이벤트에 실제 쪽수를 채워 넣어도 월간
쿼터 계산과 비용 리포트 둘 다 영향받지 않는다 — 둘 다 `kind` 로 이미
격리돼 있었다.

### Before/After 증거 (요구사항: 게이트를 빼면 새 테스트가 실패해야 한다)

```
$ git stash push --keep-index -m "finding1-documents-py-only" -- app/routers/documents.py
$ git diff HEAD -- app/routers/documents.py   # (출력 없음 = HEAD 상태로 복귀 확인)

$ .venv/bin/python -m pytest tests/test_unverified_gate.py tests/test_verification_api.py -q
......................F.                                                 [100%]
FAILED tests/test_verification_api.py::test_a_single_oversized_upload_is_refused_even_as_the_first_document
  AssertionError: {"id":"...","filename":"big.pdf","status":"pending"}
  assert 202 == 403
1 failed, 23 passed

$ git stash pop   # 복원
```

정확히 예상한 방식으로 깨졌다 — 게이트 없이 51쪽 PDF 를 첫 업로드로
올리면 403 대신 **202**(수락)가 온다. `documents.py` 를 되돌린 뒤 같은
명령을 다시 돌리면 24 passed.

**중요한 뉘앙스 하나**: `test_unverified_gate.py` 의 새 테스트 4개는
`documents.py` 를 stash 해도 계속 통과한다(23 passed 에 포함) — 이건
버그가 아니라 설계다. 그 4개는 `usage.py` 의 새 함수를 **직접** 부르므로
(라우터를 거치지 않으므로) 라우터 배선을 되돌려도 영향을 안 받는다.
그 4개가 검증하는 것은 "헬퍼의 산술이 맞는가"이고, HTTP 테스트가
검증하는 것은 "라우터가 그 헬퍼를 실제로 부르는가"다 — 이 둘은 서로
다른 것을 지키는 테스트이고, "게이트를 빼면 실패해야 하는" 테스트는
후자다. (덧붙여: 그 4개는 애초에 `usage.unverified_pages_exceeded` 가
존재하지 않으면 `AttributeError` 로 죽으므로, "이 커밋 이전 상태"로
완전히 되돌리면 — `usage.py` 까지 포함해서 — 당연히 실패한다. 이번
실험은 과제 지시대로 `documents.py` 한 파일만 stash 했다.)

### 검증

```
$ .venv/bin/python -m pytest tests/test_unverified_gate.py tests/test_verification_api.py -q
24 passed

$ .venv/bin/python -m pytest tests/ -q
166 passed, 6 warnings in 13.19s   # 161 + 신규 5
```

---

## Finding 2 (Important) — 테스트 스위트가 실제 메일을 보낼 수 있었음

### 문제

`tests/conftest.py` 는 `storage_dir` 과 `session_cookie_secure` 를
세션 내내 못박아 실제 자원을 못 건드리게 하는데, `mail_provider` 는
안 건드린다. `app/config.py` 는 앱과 테스트가 같은 `.env` 를 읽으므로,
누군가 README 대로 배포 준비를 하며 `MAIL_PROVIDER=resend` +
`RESEND_API_KEY` 를 `.env` 에 넣어두면 다음 `pytest` 실행이 그대로
`api.resend.com` 에 실제 발송을 시도한다.

숫자로 확인: `test_auth_ratelimit.py::test_signup_is_throttled_too` 하나가
`rate_limit_auth_per_min + 2 = 12` 번 가입을 발생시키고(`grep` 으로는
`/auth/signup` 리터럴이 1번만 보이는데 이는 `path` 파라미터로 넘기는
루프 안에서 12번 실행되기 때문 — 소스 상 매치 수와 런타임 호출 수가
다르다는 점을 직접 확인했다), `/auth/signup` 을 부르는 테스트는
`grep -l "/auth/signup" tests/*.py` 기준 9개 파일에 흩어져 있다.

### 변경 파일

- **`tests/conftest.py`** — `no_real_email` autouse 세션 픽스처 추가.
  `isolated_storage`/`http_session_cookies` 바로 아래, 같은 모양(원본
  저장 → yield → 복원), 왜 위험한지 설명하는 한국어 주석 포함.
  `settings.mail_provider = "console"`, `settings.resend_api_key = ""`.

### 검증

```
$ .venv/bin/python -m pytest tests/ -q
166 passed   # 새 테스트 없이 순수 안전장치이므로 카운트 불변
```

`test_mailer.py` 가 자체적으로 `mail_provider`/`resend_api_key` 를
바꿨다 복원하는 기존 테스트들과 이 세션 픽스처가 서로 간섭하지 않는지도
확인됐다(전체 그린).

이 메커니즘은 `isolated_storage`/`http_session_cookies` 와 완전히
동일한, 이미 이 파일에서 검증된 패턴(설정값을 세션 동안 강제로 덮어쓰고
끝나면 복원)이라 별도의 침투 실험은 하지 않았다 — `.env` 에 실제로
`MAIL_PROVIDER=resend` 를 써넣고 돌려보는 것은 그 자체로 이 Finding이
막으려는 행위이기도 해서 하지 않았다.

---

## Finding 3 (Important) — 배포 문서에 `MAIL_PROVIDER`·`APP_BASE_URL` 누락

### 문제

`app/config.py` 기본값은 `mail_provider="console"`,
`app_base_url="http://localhost:3000"`. README 의 배포 env 표
(`HTTP_PORT`·`SITE_ADDRESS`·`ADMIN_TOKEN`·`MODEL_THREADS`)에는 이 둘이
없고 180줄 아래 "설정" 절에만 있다. 그래서 실제 배포의 기본 결과는
"메일이 전혀 안 나간다"이고, `MAIL_PROVIDER=resend` 만 켜고
`APP_BASE_URL` 을 안 바꾸면 모든 인증 메일이 배포 도메인이 아니라
발신자의 localhost 를 가리키는 죽은 링크로 나간다 — 그리고 발송 자체는
200으로 "성공"하므로 아무 로그도 이상을 알리지 않는다.

### 변경 파일

- **`README.md`** — 배포 env 표에 두 행 추가:
  `MAIL_PROVIDER`(기본 `console` 이면 발송 자체가 안 됨을 명시),
  `APP_BASE_URL`(안 바꾸면 죽은 링크가 나간다는 것과 재발송도 같은 링크를
  다시 보낼 뿐이라는 것 명시).
- **`app/services/mailer.py`** — `_looks_local(url)` 과
  `warn_if_base_url_looks_local()` 추가. 조건은
  `mail_provider == "resend" and _looks_local(app_base_url)` — `resend`
  를 켰다는 것 자체가 "로컬에서 그냥 써본다"가 아니라는 신호라는 논리를
  그대로 조건으로 옮겼다. `get_mailer()` 와 같은 태도(막지 않고
  `logger.warning`).
- **`app/main.py`** — `lifespan()` 안, `run_migrations()` 호출 전에
  `mailer.warn_if_base_url_looks_local()` 한 줄 추가. 요청마다가 아니라
  프로세스 기동 시 한 번만 실행된다는 주석 포함.
- **`tests/test_mailer.py`** — 3케이스: 경고함(resend + localhost),
  경고 안 함(resend + 실도메인), 경고 안 함(기본 설정 그대로 — 가장
  흔한 상태에서 매 기동마다 경고가 찍히면 안 되므로).

### 검증 / 한계 고지

```
$ .venv/bin/python -m pytest tests/test_mailer.py -q
7 passed   # 기존 4 + 신규 3

$ .venv/bin/python -m pytest tests/ -q
169 passed, 6 warnings in 12.70s   # 166 + 신규 3
```

**고지할 것 하나**: `httpx.ASGITransport.__init__` 시그니처를 직접
확인했는데 (`self, app, raise_app_exceptions=True, root_path="", client=...`)
`lifespan` 파라미터가 아예 없다 — 이 테스트 스위트가 쓰는 ASGI 전송은
FastAPI 의 `lifespan()` 자체를 절대 실행하지 않는다. 즉 이 스위트
안에서는 `run_migrations()` 도, 내가 새로 추가한
`warn_if_base_url_looks_local()` 호출도 앱 기동 경로로는 한 번도
실행되지 않는다(둘 다 기존부터 그랬다 — 내가 만든 문제가 아니다). 그래서
`test_mailer.py` 의 3개 테스트는 `lifespan()` 을 거치지 않고 함수를
직접 호출해 검증한다 — 실제 `uvicorn` 기동 경로에서 이 줄이 실행되는
것 자체는 별도로 확인하지 않았다(코드 리딩으로는 `lifespan` 컨텍스트
매니저 진입 시 무조건 실행되는 순서라 의심할 지점은 없다).

---

## Finding 4 (Minor) — 토큰 해시 테스트가 알고리즘 교체를 못 잡음

### 문제

`test_raw_token_is_never_stored` 는 `stored == [verification.hash_token(raw)]`
만 검사한다. 자기 자신과 비교하는 것이라 `hash_token` 이 sha256 을 다른
다이제스트로 바꿔도 항상 일치해 초록불이 뜬다 — 배포 순간 이미 발급된
모든 인증 링크가 조용히 무효화되는 변경도 이 테스트를 통과시킨다.
(참고: 이 문제는 `.superpowers/sdd/progress.md` 의 Task 2 항목에 "브리프
유래" Minor 로 이미 기록돼 있었고, 이번에 닫았다.)

### 변경 파일

- **`tests/test_verification.py`** — `import hashlib` 추가.
  `hashlib.sha256(raw.encode("utf-8")).hexdigest()` 로 독립 계산한 값과
  비교하는 assertion 을 기존 assertion 앞에 추가(기존 것도 유지 —
  "hash_token 이 실제로 쓰이는 경로"라는 별개 사실을 여전히 검증하므로).

### Before/After 증거

`hash_token` 을 임시로 `hashlib.sha3_256` 으로 바꿔 직접 확인했다:

```
$ sed -i.bak 's/hashlib.sha256/hashlib.sha3_256/' app/services/verification.py
$ .venv/bin/python -m pytest tests/test_verification.py::test_raw_token_is_never_stored -q
FAILED — assert stored == [hashlib.sha256(...)]
  'fd5155...4b8732264d59' != '23f7003...16cb8631b0'

$ mv app/services/verification.py.bak app/services/verification.py   # 복원
$ .venv/bin/python -m pytest tests/test_verification.py::test_raw_token_is_never_stored -q
1 passed
```

새 assertion 은 잡고, 기존 assertion(자기 자신과 비교)은 알고리즘이
바뀌어도 여전히 통과했을 것 — 정확히 이 Finding 이 말한 문제 그대로.

### 검증

```
$ .venv/bin/python -m pytest tests/ -q
169 passed   # 기존 테스트 수정이라 카운트 불변
```

---

## 최종 검증

```
$ .venv/bin/python -m pytest tests/ -q
169 passed, 6 warnings in 12.19s

$ npm --prefix web test -- --run
Test Files  3 passed (3) / Tests  39 passed (39)
```

시작 시점(161 passed, vitest 39 passed) 대비 pytest 순증가 +8
(Finding 1: +5, Finding 2: +0, Finding 3: +3, Finding 4: +0). `web/` 아래
파일은 이번 작업에서 전혀 건드리지 않았으므로 vitest/`tsc`/`eslint` 는
재실행만 하고 재검사하지 않았다 — 39 passed 로 시작 시점과 동일.

## 커밋

| SHA | 메시지 | 파일 수 |
|---|---|---|
| `a0a6c58` | fix(security): 미인증 계정의 업로드 한도가 문서 개수만 세고 쪽수를 안 봤다 | 8 |
| `ce591f7` | fix(test): 테스트가 .env 설정에 따라 실제로 메일을 보낼 수 있었다 | 1 |
| `fd7e3d9` | fix(docs): 배포 문서에 MAIL_PROVIDER·APP_BASE_URL이 빠져 있었다 | 4 |
| `f5d83fb` | test(verification): 토큰 해시 테스트가 알고리즘 교체를 잡지 못했다 | 1 |

## 동의하지 않거나 재량으로 결정한 것

1. **Finding 1 의 HTTP 레벨 테스트는 1개만 추가했다** (미인증 거부).
   "인증 계정은 안 걸린다"는 서비스 레벨(`test_unverified_gate.py`)에서
   이미 증명되므로, HTTP 레벨에 같은 내용을 반복하는 것은 과제 문구
   ("Then add an HTTP-level test" — 단수)에도, 골드플레이팅 금지
   지침에도 맞지 않다고 판단했다. 필요하면 쉽게 추가할 수 있다.
2. **`unverified_pages_exceeded` 체크의 위치**: 절대 상한(`max_upload_pages`)
   체크 다음, 월간 쿼터(`upload_quota_exceeded`) 체크 이전에 뒀다. 순서
   자체가 정답을 바꾸지는 않는다(둘 다 걸리면 어느 쪽이 먼저 403/429를
   내는지만 달라진다) — 더 구체적이고 미인증 전용인 규칙을 먼저 보는
   것이 기존 코드의 "문서 게이트를 파일 읽기 전에 본다"는 원칙과
   일관적이라고 판단해 이 순서를 골랐다.
3. **Finding 3 의 "배포가 명백히 로컬이 아니다" 판정 기준**을
   `mail_provider == "resend"` 하나로 잡았다. 이 프로젝트에는 별도의
   "환경" 설정(`ENVIRONMENT=production` 류)이 없고, Finding 자체가 든
   예시("`MAIL_PROVIDER=resend` 만 켜고...")가 정확히 이 신호를
   가리키고 있어 이걸 그대로 조건으로 썼다. 다른 신호(예:
   `session_cookie_secure` 조합)를 더 원한다면 알려달라.
4. **`app/models.py` 의 `pages` 컬럼 주석을 고쳤다** — Finding 목록에는
   없지만, 내 변경이 그 주석("ingest 이벤트만 채운다")을 직접
   부정확하게 만들어서 같은 커밋에 포함했다. 같은 파일의 `kind` 컬럼
   주석(`# query | ingest` — `upload` 가 빠짐)은 내 변경과 무관하게
   이미 부정확했던 것이라 손대지 않았다.
5. **Finding 2 의 침투 테스트는 하지 않았다** — 실제로 `.env` 에
   `MAIL_PROVIDER=resend` 를 써넣고 픽스처가 막는지 확인하는 실험은,
   이 Finding 이 막으려는 바로 그 상태를 로컬에서라도 만들어보는
   것이라 생략했다. 대신 이미 이 파일에서 검증된 동일 패턴
   (`isolated_storage`, `http_session_cookies`)을 그대로 재사용해 그
   신뢰를 넘겨받는 쪽을 택했다.
