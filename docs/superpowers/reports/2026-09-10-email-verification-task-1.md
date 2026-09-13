# Task 1 실행 보고: 스키마 — 설정 · 모델 · 마이그레이션 0008

상태: **DONE_WITH_CONCERNS** (스키마·마이그레이션은 브리프 그대로 완료. 테스트 파일 하나에서
브리프 코드를 그대로 쓰면 영영 통과할 수 없는 구조적 결함을 발견해 최소한으로 고쳤다 — 상세는 "브리프에서 벗어난 부분" 참고.)

커밋: `fce861205c3468b9bef0ca440679f41cd4c97027` (브랜치 `feat/email-verification`)

## 파일별 변경 내용

### `app/config.py`
`# --- M3 Phase 2: abuse & cost defense ---` 블록 끝(`semantic_cache_threshold` 다음) 뒤에
브리프 Step 1 블록을 그대로 삽입. 8개 설정 추가: `mail_provider`, `resend_api_key`,
`mail_from`, `app_base_url`, `verify_token_ttl_hours`, `unverified_quota_queries`,
`unverified_quota_documents`, `rate_limit_verify_resend_per_min`. 코드 1바이트도 브리프와
다르지 않다. 섹션 사이 빈 줄 1개, `settings = Settings()` 앞 빈 줄 2개인 기존 파일의 관행을
그대로 유지했다(빈 줄 개수까지 diff로 확인).

### `app/models.py`
- `User` 클래스: `password_hash` 와 `created_at` 사이에 `email_verified_at` 컬럼 삽입,
  클래스 끝에 `email_verified` 읽기 전용 프로퍼티 추가. 브리프 그대로.
- `Session` 클래스와 `Document` 클래스 사이에 `EmailVerificationToken` 클래스 신설. 브리프
  그대로 — 컬럼 5개(`id`, `user_id`, `token_hash`, `expires_at`, `consumed_at`,
  `created_at`), `user_id` 인덱스, `token_hash` unique.
- 사전 확인한 대로 새 import 는 필요 없었다(`datetime`, `Index`, `ForeignKey`, `Text`,
  `TIMESTAMP`, `UUID`, `func`, `Mapped`, `mapped_column` 모두 이미 top-level에 있었음).

### `alembic/versions/0008_email_verification.py` (신규)
브리프 Step 3 코드를 그대로 파일로 만들었다. `users.email_verified_at` 추가 +
`UPDATE users SET email_verified_at = now()` 로 기존 계정 전부 grandfather, 그 다음
`email_verification_tokens` 테이블 생성(+ FK CASCADE, `token_hash` unique, `user_id` 인덱스).
`downgrade()` 는 역순으로 인덱스 → 테이블 → 컬럼 제거. 0001~0007 의 스타일(`Sequence, Union`
typing, `sqlalchemy.dialects.postgresql`, docstring에 "왜"를 적는 방식)과 100% 일치시켰다.

### `.env.example`
`RATE_LIMIT_AUTH_PER_MIN=10` 다음, `# Postgres 자격증명` 섹션 앞에 브리프 Step 9 블록을
그대로 삽입 (8개 키, 주석 포함).

### `tests/test_verification.py` (신규, 브리프에서 벗어난 유일한 파일 — 아래 상세)
브리프가 지정한 두 테스트(`test_migration_grandfathers_existing_accounts`,
`test_token_table_exists_with_unique_hash`)의 이름·검증 내용·docstring·주석을 전부 그대로
살렸다. 다른 점은 딱 하나: `async def test_...()` + `pytestmark = pytest.mark.asyncio` 대신,
이 저장소의 다른 7개 Postgres 통합 테스트 파일(`test_isolation.py`, `test_phase2.py`,
`test_m5_api.py`, `test_tracing.py`, `test_conversations.py`, `test_admin_stats.py`,
`test_storage_cleanup.py`)이 이미 쓰고 있는 `run_async()` + `_db_available()` +
`pytest.mark.skipif` 패턴으로 async 실행부만 감쌌다.

## 브리프에서 벗어난 부분과 이유

**발견**: 이 프로젝트에는 `pytest-asyncio` 가 설치돼 있지 않다. 세 군데에서 확인했다.
1. `.venv/bin/python -m pip list | grep -i "pytest\|anyio"` → `anyio 4.15.0`, `pytest 9.1.1` 뿐,
   `pytest-asyncio` 없음.
2. `requirements.txt` 에 `pytest` 자체가 아예 없다(테스트 의존성을 이 파일이 추적하지 않음).
3. `.github/workflows/*.yml` 의 CI 설치 스텝이 `pip install -r requirements.txt pytest` —
   **pytest-asyncio 를 설치하지 않는다.** 심지어 CI 는 `pytest -q` 결과에서 "N skipped" 를
   grep 해서 0 초과면 빌드를 실패시키는 스텝까지 있다(Postgres 서비스가 항상 떠 있으므로
   통합 테스트가 스킵되면 안 된다는 전제).

브리프 그대로 `async def` + `pytest.mark.asyncio` 를 썼을 때 실제로 어떻게 되는지
스크래치 디렉터리에서 재현했다:
```
async def functions are not natively supported.
You need to install a suitable plugin for your async framework, for example:
  - anyio
  - pytest-asyncio
  - pytest-tornasync
  - pytest-trio
  - pytest-twisted
1 failed, 1 warning in 0.00s
```
`anyio` 가 설치돼 있어도 소용없다 — anyio 의 pytest 플러그인은 `pytest.mark.anyio` 를
찾지 `asyncio` 마커를 처리하지 않는다(플러그인이 로드돼 있다는 게 pytest 헤더에 찍히는데도
동일하게 실패하는 것으로 확인).

**왜 이게 "일단 넘어갈 사안"이 아닌가**: 이 실패는 스키마 구현 여부와 무관하게 항상
일어난다 — 코루틴 본문이 아예 실행되지 않고 pytest 내장 shim 이 즉시 실패시키기 때문이다.
즉 브리프 코드를 그대로 뒀다면:
- Step 5("FAIL")는 어쩌다 보니 통과하는 것처럼 보이지만, 브리프가 의도한 이유
  (`ProgrammingError`/`ImportError`)가 아니라 "async 러너가 없다"는 전혀 다른 이유였을 것이고,
- Step 7("2 passed")은 스키마를 완벽하게 구현해도 **절대 도달 불가능**했다. 이 태스크의
  유일한 합격 기준이 원천적으로 성립할 수 없는 상태였다는 뜻이다.
- 그대로 커밋했다면 CI 는 이 파일에서 영구적으로 빨간불이었을 것이다.

**대안으로 "pytest-asyncio 설치"를 검토했지만 기각했다**: (a) 브리프의 파일 목록에
`requirements.txt` 나 CI 워크플로가 없다 — 태스크 범위 밖. (b) CI 설치 스텝도 함께 고쳐야
실제로 효과가 있는데, 이는 이 태스크 하나가 조용히 결정할 사안이 아니라고 판단했다.
(c) 이 저장소는 이미 7개 파일에서 동일한 문제(비동기 통합 테스트를 순수 pytest 로 돌려야
함)를 `run_async`/`_db_available` 패턴으로 풀어놓았다 — 새 의존성 없이, 기존 관행과
100% 일치하게 고치는 쪽이 더 안전하고 최소 침습적이라고 판단해 이 쪽을 택했다.

두 테스트의 검증 로직·assert 문·한글 docstring 은 브리프 문구를 그대로 옮겼다. 두 번째
테스트에서 `from app.models import EmailVerificationToken` 을 (모듈 top-level 이 아니라)
`scenario()` 안에 그대로 남겨둔 것도 브리프의 의도를 그대로 지켰다 — 클래스가 없던
시점(Step 5)에 이 import 가 실패해도 첫 번째 테스트의 수집(collection)에는 영향을 주지
않게 하려는 의도로 읽었다.

이 판단이 틀렸다고 보시면 알려주십시오. 되돌리기는 `tests/test_verification.py` 한 파일만
다시 쓰면 되고, 스키마 커밋(config/models/migration/.env.example)에는 영향이 없다.

## 실행한 테스트 명령과 결과

### Step 5 — 마이그레이션 적용 전, 실패 확인
```
$ .venv/bin/python -m pytest tests/test_verification.py -v
```
```
tests/test_verification.py::test_migration_grandfathers_existing_accounts FAILED
tests/test_verification.py::test_token_table_exists_with_unique_hash FAILED
2 failed in 0.53s
```
- 첫 번째: `asyncpg.exceptions.UndefinedColumnError: column "email_verified_at" does not exist`
- 두 번째: `asyncpg.exceptions.UndefinedTableError: relation "email_verification_tokens" does not exist`
  (모델 클래스는 이미 Step 2 에서 추가돼 있었으므로 import 는 성공하고, INSERT 시점에
  테이블 부재로 실패 — 브리프가 예상한 두 실패 모드 중 `ProgrammingError` 쪽과 정확히 일치)

브리프의 "Expected: FAIL" 을 정확한 이유로 충족.

### Step 6 — 마이그레이션 적용
```
$ .venv/bin/python -m alembic upgrade head
```
```
INFO  [alembic.runtime.migration] Running upgrade 0007 -> 0008, 이메일 인증: 인증 시각 + 1회용 토큰
```
브리프가 명시한 로그 문구와 정확히 일치.

### Step 7 — 마이그레이션 적용 후, 통과 확인
```
$ .venv/bin/python -m pytest tests/test_verification.py -v
```
```
tests/test_verification.py::test_migration_grandfathers_existing_accounts PASSED
tests/test_verification.py::test_token_table_exists_with_unique_hash PASSED
2 passed in 0.16s
```

### Step 8 — 되돌리기 왕복
```
$ .venv/bin/python -m alembic downgrade 0007
INFO  ... Running downgrade 0008 -> 0007, 이메일 인증: 인증 시각 + 1회용 토큰
$ .venv/bin/python -m alembic current
0007
$ .venv/bin/python -m alembic upgrade head
INFO  ... Running upgrade 0007 -> 0008, 이메일 인증: 인증 시각 + 1회용 토큰
$ .venv/bin/python -m alembic current
0008 (head)
```
양쪽 다 에러 없이 끝났고, 최종적으로 `0008 (head)`. 재상승 직후 `pytest
tests/test_verification.py -v` 를 다시 돌려 여전히 `2 passed` 인 것도 확인했다
(`UPDATE users SET email_verified_at = now()` 가 재실행돼도 멱등하게 동작).

### 추가로 한 것 — 회귀 여부 확인 (브리프에 없는 항목)
`app/config.py`, `app/models.py` 는 앱 전역에서 import 되는 공유 파일이라, 손대고 나서
전체 스위트를 한 번 돌려봤다.
```
$ .venv/bin/python -m pytest -q
21 failed, 105 passed
```
21개 실패가 이 변경 때문인지 확인하려고 `git stash push -u` 로 이번 태스크의 변경분(수정
2개 + 신규 파일 2개)을 전부 치우고 원래 코드로 동일하게 돌려봤다:
```
$ git stash push -u -m "task1-wip-baseline-check"
$ .venv/bin/python -m pytest -q
21 failed, 103 passed
$ git stash pop
```
실패한 21개 테스트 이름과 에러 종류(`KeyError: 'id'`, `AttributeError` 등, 전부
`test_conversations.py`/`test_isolation.py`/`test_m5_api.py`/`test_phase2.py`/
`test_storage_cleanup.py`/`test_tracing.py`)가 변경 전후로 **완전히 동일**했다. 통과 개수
차이(105 vs 103)는 정확히 이 태스크가 추가한 `test_verification.py` 의 테스트 2개다.
즉 이 21개는 이 태스크 이전부터 있던 환경 문제(추정: 임베딩/재랭크용 로컬 모델 서버가 이
샌드박스에 안 떠 있음 — 브리프의 컨텍스트는 Postgres 만 띄워져 있다고 했지 다른 서버는
언급이 없었다)이고, 이번 변경과 무관함을 확인했다. `git stash pop` 이후 파일 상태와
`alembic current`(`0008 (head)`) 를 다시 확인해 정상 복구됐음을 검증하고 커밋을 진행했다.

## 놀란 점

1. **가장 큰 것**: 브리프와 함께 주어진 "컨텍스트"("existing tests use pytest.mark.asyncio;
   pytest-asyncio is already configured in this project")가 사실이 아니었다. 실제로는 이
   저장소의 async 통합 테스트 전부가 수동 `asyncio.run` 래퍼를 쓰고, CI 도 pytest-asyncio 를
   설치하지 않는다. 브리프의 Step 4 코드를 그대로 커밋했다면 스키마가 완벽해도 Step 7의
   "2 passed"는 영영 볼 수 없었을 것이다 — 위 "브리프에서 벗어난 부분" 참고.
2. 시드 유저 ID(`00000000-0000-0000-0000-00000000dead`, `app/constants.py` 의
   `SEED_USER_ID`)가 브리프 Step 4 테스트의 `user_id` 리터럴과 정확히 같다는 걸 알아채고
   나서야, 두 번째 테스트가 FK 제약을 진짜로 통과할 수 있는 이유(0003 이 만든 그 시드
   유저가 실제로 존재하기 때문)를 이해했다 — 브리프가 우연이 아니라 의도적으로 그 상수를
   재사용했다는 뜻이라 안심했다.
3. 그 외에는 브리프가 정확했다: import 목록, 파일 배치 위치, 빈 줄 개수까지 기존 파일
   관행과 다 맞아떨어졌고, 마이그레이션 업/다운그레이드도 첫 시도에 깨끗하게 왕복했다.
