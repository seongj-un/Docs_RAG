# Task 2 실행 보고: 토큰 서비스 — 발급 · 소비 · 무효화

상태: **DONE**

커밋: `3ae3bff49ae207d85d2698faccbc2d2207474f1f` (브랜치 `feat/email-verification`)

## 파일별 변경 내용

### `app/services/verification.py` (신규)

브리프 Step 3 코드를 그대로 옮겼다 — 로직상 1바이트도 다르지 않다. 모듈 docstring 이
설계 판단을 그대로 담고 있다: 토큰을 세션과 같은 취소 가능한 서버측 행으로 다룬다는 것,
JWT/itsdangerous 대신 DB 행을 쓴 이유, sha256(argon2 아님)을 고른 이유, 그리고 이 모듈은
DB 만 알고 HTTP 상태 코드 번역은 라우터(Task 4) 몫이라는 경계.

공개 인터페이스 — 브리프가 요구한 것과 정확히 일치:
- `hash_token(raw: str) -> str`
- `issue_token(session, user_id) -> str` — 원문 반환, 발급 시 그 사용자의 미소비 토큰을 먼저 삭제
- `consume_token(session, raw) -> tuple[VerifyResult, User | None]`
- `VerifyResult` — `OK` / `ALREADY` / `INVALID`
- `build_link(raw) -> str`, `build_email(link) -> tuple[str, str, str]`

`grep -n "HTTPException\|app.routers" app/services/verification.py` → 매치 없음.
`app/routers`를 임포트하지 않고 `HTTPException`을 쓰지 않는다는 제약을 확인했다.

### `tests/test_verification.py` (Task 1이 만든 파일에 append, 169줄 추가)

브리프 Step 1 블록의 11개 테스트 중 DB 를 쓰는 9개(`_make_user` 헬퍼 포함하면 10개 정의)를
"동기 `def test_*` 안에 `async def scenario(): ...`를 정의하고 `run_async(scenario)`로
돌린다" 패턴으로 감쌌다 — 이 파일 상단에 Task 1이 이미 만들어 둔 `run_async` /
`_db_available` / `pytestmark = pytest.mark.skipif(...)`를 그대로 재사용했고 손대지 않았다.

변환 규칙: 브리프 원문에서 `async with SessionLocal() as session:` 블록 안에 있던 코드는
`scenario()` 안으로, 블록 밖(주로 `async with`가 끝난 뒤의 `assert`들)에 있던 코드는
`run_async(scenario)` 호출 뒤 그대로 남겼다. `scenario()`가 뒤쪽 assert에 필요한 값들을
`return`한다. 함수 최상단의 `from ... import ...`도 브리프가 쓴 위치(‑ `async with` 진입
전)를 그대로 지켰다.

나머지 2개(`test_link_points_at_the_frontend_not_the_api`,
`test_email_body_carries_the_link_in_both_parts`)는 브리프가 이미 DB 를 안 쓰는 plain
`def`로 써놨으므로 그대로 옮겼다 — `run_async` 래핑 불필요, 다만 파일 레벨
`pytestmark`가 있어 Postgres 가 죽어 있으면 이 둘도 함께 스킵된다(브리프의 "Ambiguity
resolved" 항목이 명시적으로 허용한 동작).

테스트 이름·assert 문·한글 docstring은 전부 브리프 원문 그대로다. 실행 골격 변환을
제외하면 실제로 바뀐 것은 아래 "브리프에서 벗어난 부분"의 두 줄뿐이다.

## 브리프에서 벗어난 부분과 이유

**바인드 파라미터 타입 버그 하나, 두 곳.** `test_raw_token_is_never_stored` 와
`test_an_expired_token_is_refused` 가 raw SQL `text(...)`로 `WHERE user_id = :uid`를
걸면서 브리프 원문대로 `.bindparams(uid=str(user.id))`처럼 UUID 를 문자열로 바꿔
넘겼다. SQLAlchemy 의 asyncpg dialect 가 파이썬 값의 타입(str)을 보고 이 바인드를
`$N::VARCHAR`로 명시 캐스팅해 컴파일하는데, `email_verification_tokens.user_id`는
실제로는 `uuid` 컬럼이라 Postgres 가 `operator does not exist: uuid = character
varying`로 죽는다.

재현부터 하고 고쳤다:
1. 브리프 코드를 그대로 실행 → 두 테스트만 이 에러로 실패, 나머지 9개는 통과.
2. 스크래치 디렉터리(`probe_uuid_bind*.py`, 프로젝트 밖)에서 세 가지 대안을 검증:
   `CAST(:uid AS uuid)`를 SQL 에 직접 박는 안, `bindparam("uid", ..., type_=PG_UUID)`로
   타입을 명시하는 안, 그냥 `user.id`(ORM 이 돌려주는 `uuid.UUID` 호환 객체)를 캐스트 없이
   넘기는 안 — 셋 다 성공.
3. 가장 적게 건드리는 세 번째 안을 택해 `str(user.id)` → `user.id` 로 딱 두 곳만 고쳤다.
   테스트 이름·assert·독스트링·SQL 문자열·주석은 전혀 건드리지 않았다 — `sed`로 정확히
   그 두 토큰만 치환했고 diff 로 재확인했다.

이 서비스 코드(`issue_token`/`consume_token`) 자체는 이 문제와 무관하다 — 둘 다 ORM
`select()`/`delete()`를 쓰지 raw `text()`를 쓰지 않는다. 버그는 브리프가 테스트에 직접
박아 넣은 raw SQL 에만 있었다.

그 외 디테일은 브리프 그대로 두었다: 예를 들어 `test_an_expired_token_is_refused` 본문이
파일 상단에 이미 있는 `from datetime import datetime, timedelta, timezone`을 함수
안에서 다시 import 하는 것도 그대로 남겼다 — 동작에 영향이 없고, 임의로 정리하면
"브리프 그대로"라는 요구에서 멀어진다고 판단했다.

## 실행한 테스트 명령과 결과

### 사전 베이스라인 확인 (구현 착수 전)

```
$ .venv/bin/python -m pytest -q
126 passed, 6 warnings in 9.39s
```

브리프가 명시한 베이스라인(126 passed, 0 failed)과 일치하는 것을 먼저 확인했다.

### 새 파일 단독 실행 — 1차 시도 (브리프 원문 그대로, 바인드 버그 있음)

```
$ .venv/bin/python -m pytest tests/test_verification.py -v
...
FAILED tests/test_verification.py::test_raw_token_is_never_stored
  - sqlalchemy.exc.ProgrammingError: ... operator does not exist: uuid = character varying
FAILED tests/test_verification.py::test_an_expired_token_is_refused
  - sqlalchemy.exc.ProgrammingError: ... operator does not exist: uuid = character varying
2 failed, 9 passed in 0.83s
```

### 원인 격리 (스크래치 디렉터리, 프로젝트 파일 아님 — 커밋 대상 아님)

세 변형(SQL 내 CAST / `bindparam(type_=PG_UUID)` / 캐스트 없는 `uuid.UUID` 객체) 모두
성공 확인 → 두 곳을 `uid=user.id`로 수정.

### 새 파일 단독 실행 — 수정 후

```
$ .venv/bin/python -m pytest tests/test_verification.py -v
tests/test_verification.py::test_migration_grandfathers_existing_accounts PASSED
tests/test_verification.py::test_token_table_exists_with_unique_hash PASSED
tests/test_verification.py::test_raw_token_is_never_stored PASSED
tests/test_verification.py::test_valid_token_marks_the_user_verified PASSED
tests/test_verification.py::test_a_consumed_token_reports_already_verified PASSED
tests/test_verification.py::test_an_expired_token_is_refused PASSED
tests/test_verification.py::test_an_unknown_token_is_refused_the_same_way_as_an_expired_one PASSED
tests/test_verification.py::test_reissuing_kills_the_previous_link PASSED
tests/test_verification.py::test_reissuing_keeps_consumed_rows PASSED
tests/test_verification.py::test_link_points_at_the_frontend_not_the_api PASSED
tests/test_verification.py::test_email_body_carries_the_link_in_both_parts PASSED
11 passed in 0.49s
```

브리프 Step 4 의 "Expected: 11 passed"와 정확히 일치 (Task 1의 기존 2개 + 이번에 추가한
9개).

### 전체 스위트 (회귀 확인, 안정성 확인을 위해 2회 반복)

```
$ .venv/bin/python -m pytest -q
135 passed, 6 warnings in 9.73s
$ .venv/bin/python -m pytest -q
135 passed, 6 warnings in 8.70s
```

126(베이스라인) + 9(순수 신규 테스트) = 135. 실패 0, 두 번 다 동일 — 회귀도 flaky 함도
없음.

### 부가 확인

- `SELECT count(*) FROM users WHERE email LIKE '%@example.com'` → `0` (전체 스위트 실행
  후 conftest 의 세션 정리 스윕이 정상 동작해 테스트 계정이 남지 않았다).
- `.venv/bin/python -c "from app.services import verification; ..."` 로 모듈이 단독
  임포트되고 `hash_token`/`build_link`가 기대한 값을 내는 것을 구현 직후 스모크 테스트.

## 놀란 점

1. 브리프 Step 3 의 서비스 코드 자체는 (테스트 하네스 경고를 빼면) 흠이 없었다 — 실패는
   서비스 로직이 아니라 **브리프가 제공한 테스트 코드의 raw SQL 바인드 방식**에 있었다.
   Task 1 보고서가 지적한 것과 같은 패턴이다: 브리프의 예시 코드가 실제 이 저장소의
   Postgres/asyncpg 조합에 대고 끝까지 실행되어 검증된 것은 아닌 것으로 보인다.
2. 같은 문제(문자열로 넘긴 UUID 바인드가 `::VARCHAR`로 캐스팅되어 `uuid` 컬럼과 비교 시
   실패)가 서비스 코드에는 전혀 나타나지 않는다 — `issue_token`/`consume_token`은 전부
   ORM `select()`/`delete()`/`session.get()`을 쓰기 때문에 SQLAlchemy 가 컬럼 타입을 알고
   자동으로 맞춰준다. 오직 브리프가 테스트에 직접 쓴 raw `text()` UPDATE/SELECT 두 곳만
   영향을 받았다 — 서비스 구현이 아니라 테스트 작성 방식의 문제였다는 뜻이라 구현
   자체를 의심하며 시간을 쓰지 않을 수 있었다.
3. 그 외에는 브리프가 정확했다. 11개 테스트를 sync 래퍼로 옮기는 기계적 변환이 한 번에
   깨끗하게 맞아떨어졌고, Task 1이 남긴 사전조건(0008 head, `User.email_verified`,
   `settings.verify_token_ttl_hours`/`app_base_url`, `auth.create_user` 시그니처)도
   브리프가 말한 그대로였다.

## 건드리지 않은 것

- `.superpowers/sdd/progress.md` — Task 1이 자기 완료 항목을 스스로 추가한 전례가 있지만,
  이번 태스크 지시에는 이 파일 갱신이 명시되지 않아 손대지 않았다. 필요하면 알려달라.
- Task 3(라우터 연결)·Task 4(HTTPException 번역)·메일러 연동 — 스코프 밖, 착수하지 않음.
- `app/models.py`, `app/config.py`, 마이그레이션 — 이번 태스크는 Task 1 산출물을
  소비만 했고 수정하지 않았다.

## 수정: 경쟁 조건 2건

상태: **DONE**

커밋: `bb4d44eda9f3250ac34c04f4dbd335cef0aaccc6` (브랜치 `feat/email-verification`)

리뷰가 Important 로 지적한 두 건. 원인은 하나다 — `consume_token` 이
check-then-act 였다: SELECT 로 행을 읽어 `consumed_at`/`expires_at` 을 파이썬에서
판단한 뒤, 나중에 그 객체를 고쳐 커밋했다. 잠금도, 조건부 UPDATE 도 없었다.

**Finding 1 — 이중 소비.** 같은 원문 토큰으로 동시에 두 요청이 오면(중복 제출,
두 탭) 둘 다 커밋 전에 SELECT 를 마쳐 "아직 안 썼다"를 보고, 둘 다 통과해 둘
다 커밋한다 — 둘 다 `OK`. "두 번째 클릭은 ALREADY" 는 두 호출이 순차적일
때만 성립했다.

**Finding 2 — 재발급 경쟁이 죽인다.** `issue_token` 은 그 사용자의 미소비
토큰을 지운다. 세션 A 가 행을 읽어 파이썬 객체로 들고 있는 사이 세션 B 의
`issue_token` 이 같은 행을 지우고 커밋하면, A 가 나중에 `row.consumed_at = now`
를 커밋할 때 나가는 `UPDATE ... WHERE id=:id` 가 0행에 매치되어 SQLAlchemy 가
`StaleDataError` 를 던진다(설치된 2.0.52 의 `orm/persistence.py` 가 기본적으로
matched rowcount 를 확인하고, postgresql 의 `supports_sane_rowcount` 가 True 라
이 검사가 실제로 작동한다). "절대 예외를 던지지 않는다"는 계약 위반이다.

### 수정 (`app/services/verification.py`)

읽기와 쓰기를 하나의 조건부 `UPDATE ... RETURNING` 으로 합쳐 "클레임"으로
바꿨다.

```python
now = datetime.now(timezone.utc)

claimed = await session.execute(
    update(EmailVerificationToken)
    .where(
        EmailVerificationToken.token_hash == hash_token(raw),
        EmailVerificationToken.consumed_at.is_(None),
        EmailVerificationToken.expires_at > now,
    )
    .values(consumed_at=now)
    .returning(EmailVerificationToken.user_id)
    .execution_options(synchronize_session=False)
)
user_id = claimed.scalar_one_or_none()
```

- **못 가져갔다(`user_id is None`)** — 이미 소비됐거나, 만료됐거나, 애초에
  없거나, 방금 재발급이 지웠다. 커밋할 것이 없으므로 존재 여부만 해시로 다시
  조회해 `consumed_at IS NOT NULL` 이면 `ALREADY`, 아니면(만료·미존재·삭제됨을
  구분하지 않고) `INVALID` 를 돌려주고, **롤백** 한다.
- **가져갔다** — 반환된 `user_id` 로 `User` 를 로드해 `email_verified_at = now`
  를 설정하고 커밋한다. 그사이 계정이 지워졌다면 롤백하고 `INVALID`.

왜 이게 두 경쟁을 동시에 닫는지:

1. **이중 소비.** WHERE 에 매치되는 행에 Postgres 가 거는 잠금 때문에 동시
   호출 중 정확히 하나만 실제로 행을 바꿀 수 있다. 먼저 커밋한 쪽이
   `consumed_at` 을 채우면, 대기하던 나머지 UPDATE 는 잠금이 풀린 뒤
   **재평가된** WHERE(`consumed_at IS NULL`) 에서 더는 매치되지 않아 0행으로
   끝난다 — 위의 "못 가져갔다" 분기가 그걸 집어 `ALREADY` 로 답한다.
2. **재발급 경쟁.** 이제 이 함수는 행을 별도로 읽어 들고 있지 않는다.
   `issue_token` 이 먼저 행을 지우고 커밋해버리면, 이 UPDATE 는 그냥 0행에
   매치될 뿐이다 — SQLAlchemy 에게 "1행을 기대했는데 못 찾았다"고 알릴 로드된
   객체 자체가 없으므로 `StaleDataError` 가 던져질 자리가 없다. 0행 매치는
   위와 같은 "못 가져갔다" 분기로 흡수되어 `INVALID` 가 된다.

보존한 것: 만료·미존재·해시불일치는 여전히 전부 `INVALID` 로 구분 불가능하고,
소비된 토큰은 그 후 만료됐어도 여전히 `ALREADY` 이며(존재 여부/소비 여부를
먼저 물어 판별하므로 만료 시각은 이 경로에서 아예 보지 않는다), 함수는 이
경로들에서 예외를 던지지 않고 `tuple[VerifyResult, User | None]` 을 반환한다.
`app/routers` 임포트도 `HTTPException` 사용도 없다(수정 전과 동일).

### 테스트 (`tests/test_verification.py`)

새 테스트 2개를 파일 끝, `# --- 경쟁 조건: consume_token 은 원자적 클레임이어야
한다 ---` 구역 아래에 추가했다. 기존 관례를 그대로 따른다: 동기 `def test_*`
가 `async def scenario(): ...` 를 정의하고 `run_async(scenario)` 로 돌리며,
파일 상단의 `run_async`/`_db_available`/`pytestmark` 를 재정의하지 않고
재사용했다.

**결정적으로 만들기 위해 세 번째 커넥션으로 행 잠금을 걸었다.** 처음에는
`asyncio.gather(consume_token(a), consume_token(b))` 만으로 충분할 것으로
생각했지만, 로컬 Postgres 왕복이 워낙 빨라 두 태스크가 매번 우연히 순차
실행처럼 끝나버릴 위험이 있고, 그러면 "통과"가 아무것도 증명하지 못한다.
그래서:

1. 별도의 `blocker` 세션이 대상 행에 `SELECT ... FOR UPDATE` 로 잠금을 건다.
2. 소비(그리고 두 번째 테스트에서는 재발급)를 `asyncio.create_task` 로
   걸어, 둘 다 그 행을 건드리려다 잠금 뒤에 줄을 서게 만든다.
3. `asyncio.wait(..., timeout=0.3)` 로 "그 시간 안에 아무것도 안 끝났다"를
   확인해 진짜로 막혔는지까지 검증한다(끝나버렸다면 잠금 가정이 틀렸다는
   뜻이므로 조용히 넘어가지 않고 assert 로 드러낸다).
4. 블로커를 롤백해 잠금만 풀고, 승부는 Postgres 의 잠금 대기열에 맡긴다.

`test_concurrent_consume_of_the_same_token_yields_exactly_one_ok` 는 승자·패자
어느 쪽이든 될 수 있으므로 고정 순서가 아니라
`sorted([result_a.value, result_b.value]) == sorted(["ok", "already"])` 로
비교하고, 승자 쪽 `User` 는 `email_verified is True` 로 실재함을, 패자 쪽은
`None` 임을 함께 확인한다.

`test_a_consume_racing_a_reissue_returns_invalid_not_a_crash` 는 재발급(B)을
소비(A)보다 **먼저** 잠금 대기열에 세운다 — 그래야 잠금을 풀었을 때
Postgres 가 대기열 순서대로 B 를 먼저 들여보내 "재발급이 먼저 지우고
커밋한 뒤에 소비가 그 사실을 본다"는, 크래시를 낳는 순서가 강제된다(반대
순서로 A 가 먼저 이기면 이 경쟁 자체가 일어나지 않는다). `consume_token`
이 예외 없이 `INVALID, None` 을 돌려주는지 확인한다.

인터리빙을 "결정론적으로 표현하는 게 불가능하다"고 포기할 필요는 없었다 —
행 잠금을 직접 거는 이 방법으로 매 실행 완전히 결정론적인 재현을 얻었다.

### 실행한 명령과 결과

**변경 전 베이스라인**

```
$ .venv/bin/python -m pytest tests/ -q
135 passed, 6 warnings in 9.94s
```

**수정 + 새 테스트 적용 후 — `test_verification.py` 단독**

```
$ .venv/bin/python -m pytest tests/test_verification.py -v
tests/test_verification.py::test_migration_grandfathers_existing_accounts PASSED
tests/test_verification.py::test_token_table_exists_with_unique_hash PASSED
tests/test_verification.py::test_raw_token_is_never_stored PASSED
tests/test_verification.py::test_valid_token_marks_the_user_verified PASSED
tests/test_verification.py::test_a_consumed_token_reports_already_verified PASSED
tests/test_verification.py::test_an_expired_token_is_refused PASSED
tests/test_verification.py::test_an_unknown_token_is_refused_the_same_way_as_an_expired_one PASSED
tests/test_verification.py::test_reissuing_kills_the_previous_link PASSED
tests/test_verification.py::test_reissuing_keeps_consumed_rows PASSED
tests/test_verification.py::test_link_points_at_the_frontend_not_the_api PASSED
tests/test_verification.py::test_email_body_carries_the_link_in_both_parts PASSED
tests/test_verification.py::test_concurrent_consume_of_the_same_token_yields_exactly_one_ok PASSED
tests/test_verification.py::test_a_consume_racing_a_reissue_returns_invalid_not_a_crash PASSED
13 passed in 1.61s
```

**전체 스위트**

```
$ .venv/bin/python -m pytest tests/ -q
137 passed, 6 warnings in 10.96s
```

135(베이스라인) + 2(신규 경쟁 조건 테스트) = 137. 실패 0, 회귀 없음.

**신규 테스트만 8회 반복 — flaky 여부 확인**

```
$ for i in 1 2 3 4 5 6 7 8; do .venv/bin/python -m pytest tests/test_verification.py -q -k "concurrent or crash"; done
2 passed, 11 deselected   (×8, 매번 동일)
```

### 구현 전(舊) 코드로 되돌려 새 테스트가 실제로 실패하는지 확인

`git stash push -- app/services/verification.py` 로 서비스 코드만 되돌리고
(새 테스트는 그대로 둔 채) 실행:

```
$ .venv/bin/python -m pytest tests/test_verification.py -k "concurrent or crash" --tb=short
FAILED tests/test_verification.py::test_concurrent_consume_of_the_same_token_yields_exactly_one_ok
  AssertionError: assert ['ok', 'ok'] == ['already', 'ok']
    At index 0 diff: 'ok' != 'already'

FAILED tests/test_verification.py::test_a_consume_racing_a_reissue_returns_invalid_not_a_crash
  app/services/verification.py:99: in consume_token
      await session.commit()
  ...
  sqlalchemy.orm.exc.StaleDataError: UPDATE statement on table
  'email_verification_tokens' expected to update 1 row(s); 0 were matched.

2 failed, 11 deselected in 1.39s
```

정확히 두 findings 가 예측한 모양대로 실패했다 — 이중 소비 테스트는 두 응답이
모두 `'ok'` 로 나와 조용한 이중 소비를 직접 보여줬고(크래시가 아니라 잘못된
값), 재발급 경쟁 테스트는 `consume_token` 의 `await session.commit()` 에서
`StaleDataError` 가 그대로 새어 나와 죽었다. 둘 다 새 테스트가 없었다면
"통과"로 보였을 문제가 아니라, 새 테스트가 있어야만 잡히는 회귀라는 뜻이다.

이후 `git stash pop` 으로 수정을 복원하고 위의 "수정 + 새 테스트 적용 후"
결과(13 passed / 137 passed)를 다시 확인했다.

### 결론

두 경쟁 모두 단일 조건부 `UPDATE ... RETURNING` 클레임으로 닫혔다. 이중
소비는 Postgres 의 행 잠금이 동시 호출 중 하나만 통과시켜서, 재발급 경쟁은
더 이상 별도로 읽어 든 객체를 나중에 조건 없이 UPDATE 하지 않아서(0행 매치가
예외가 아니라 그냥 "못 가져갔다"는 정상 분기로 처리되어서) 사라진다. 새
테스트 둘은 舊 구현에 대고 각각 정확히 자신이 겨냥한 방식으로 실패함을
확인했고, 8회 반복에서도 flaky 하지 않았다.
