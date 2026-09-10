# 이메일 인증 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 가입 이메일의 소유를 확인시켜, 계정 단위 쿼터가 재가입 한 번으로 초기화되지 않게 한다.

**Architecture:** DB에 토큰 해시를 저장하는 1회용 링크 방식(세션과 같은 "서버측 상태 = 즉시 무효화" 철학). 메일은 Resend HTTP API로, 키가 없으면 콘솔 어댑터로 자동 폴백해 인프라 없이도 전체 테스트가 돈다. 미인증 계정은 계정 수명 전체 누적 기준의 맛보기 쿼터(질의 5·문서 1)를 받고, 초과하면 403.

**Tech Stack:** FastAPI · SQLAlchemy(async) · Alembic · PostgreSQL · httpx · Next.js 16(App Router) · pytest · vitest

원본 스펙: `docs/superpowers/specs/2026-09-10-email-verification-design.md`

## Global Constraints

- Python은 반드시 `.venv/bin/python`으로 실행한다. 시스템 python에는 pymupdf가 없어 테스트가 수집 단계에서 죽는다.
- 새 detail 문자열을 추가하면 `web/lib/api/errors.ts`의 `BY_DETAIL`과 `tests/test_error_details.py`의 `DETAIL_MAPPED` **양쪽**에 넣는다. 한쪽만 넣으면 그 테스트가 실패하고, 둘 다 빠뜨리면 사용자가 틀린 조언을 받는다.
- 그 대조는 양방향이라 백엔드와 프론트가 **같이** 있어야 통과한다. 백엔드 detail 이 먼저 들어가는 Task 4~6 동안 `tests/test_error_details.py`는 빨간불이며, 각 태스크의 테스트 명령이 `--ignore` 로 제외한다. Task 7 이 카피와 `DETAIL_MAPPED`를 함께 넣어 다시 초록불로 만든다. **그때까지 이 파일을 건드리지 않는다.**
- 사용자에게 보이는 문구는 (원인 + 해결 방법) 둘 다 준다. 제목에 시스템 용어(영문 4글자 이상 소문자)를 넣지 않는다 — `web/lib/api/errors.test.ts`가 정규식으로 막는다.
- 마이그레이션은 autogenerate 하지 않고 손으로 쓴다. 기존 `0001`~`0007`이 전부 그렇다.
- 커밋 메시지는 한국어 본문. 무엇을 왜 바꿨는지 적는다.
- 브랜치는 `feat/email-verification` (이미 생성됨, 스펙 커밋 `d716428`이 올라가 있다).
- 새 설정값은 `app/config.py`와 `.env.example` 양쪽에 넣는다.

---

## File Structure

**생성**

| 파일 | 책임 |
|---|---|
| `alembic/versions/0008_email_verification.py` | 스키마: `users.email_verified_at`, `email_verification_tokens`, 기존 계정 grandfather |
| `app/services/verification.py` | 토큰 발급·소비·무효화, 링크/메일 본문 조립. DB만 알고 HTTP는 모른다 |
| `app/services/mailer.py` | 전송만. 프로토콜 + Resend/Console 어댑터 |
| `tests/test_verification.py` | 토큰 수명(해시 저장·만료·재사용·재발송 무효화) |
| `tests/test_verification_api.py` | `/auth/verify`·`/auth/resend-verification` 라우트 |
| `tests/test_unverified_gate.py` | 맛보기 쿼터가 실제로 누적인지 |
| `web/app/verify/page.tsx` | 메일 링크 착지 페이지. `(app)` 그룹 밖 |
| `web/components/VerifyBanner.tsx` | 미인증 배너 |

**수정**

| 파일 | 변경 |
|---|---|
| `app/config.py` | 메일·토큰·맛보기 쿼터 설정 8개 |
| `app/models.py` | `User.email_verified_at` + `email_verified` 프로퍼티, `EmailVerificationToken` |
| `app/schemas.py` | `UserOut.email_verified`, `VerifyRequest`, `UsageOut` 확장 |
| `app/deps.py` | `VERIFICATION_REQUIRED` (403) 공용 예외 |
| `app/services/usage.py` | `queries_total`·`documents_total`·미인증 초과 판정 |
| `app/services/ratelimit.py` | `verify_resend_limiter` |
| `app/routers/auth.py` | signup 훅, `/verify`, `/resend-verification` |
| `app/routers/documents.py` | 업로드 게이트 |
| `app/routers/usage.py` | 미인증 한도 노출 |
| `app/services/pipeline.py` | 질의 게이트 |
| `scripts/cost_report.py` | 새 `upload` 이벤트를 비용 집계에서 제외 |
| `tests/test_error_details.py` | `DETAIL_MAPPED` 4개 추가 |
| `web/lib/api/types.ts` · `auth.ts` · `errors.ts` | 타입·호출·카피 |
| `web/lib/session.tsx` | 배너가 쓸 `refresh()` |
| `web/components/AppShell.tsx` | 배너 자리 |
| `web/app/(app)/settings/page.tsx` | 미인증일 때 다른 사용량 바 |
| `.env.example` · `README.md` | 새 환경변수 |

---

### Task 1: 스키마 — 설정 · 모델 · 마이그레이션 0008

**Files:**
- Modify: `app/config.py`
- Modify: `app/models.py`
- Create: `alembic/versions/0008_email_verification.py`
- Modify: `.env.example`
- Test: `tests/test_verification.py` (이 태스크에서는 스키마 검사만)

**Interfaces:**
- Consumes: 없음 (첫 태스크)
- Produces:
  - `settings.mail_provider: str`, `settings.resend_api_key: str`, `settings.mail_from: str`, `settings.app_base_url: str`, `settings.verify_token_ttl_hours: int`, `settings.unverified_quota_queries: int`, `settings.unverified_quota_documents: int`, `settings.rate_limit_verify_resend_per_min: int`
  - `User.email_verified_at: datetime | None`, `User.email_verified: bool` (읽기 전용 프로퍼티)
  - `EmailVerificationToken` (컬럼: `id`, `user_id`, `token_hash`, `expires_at`, `consumed_at`, `created_at`)

- [ ] **Step 1: 설정 추가**

`app/config.py`의 `# --- M3 Phase 2: abuse & cost defense ---` 블록 **뒤**에 붙인다.

```python
    # --- 이메일 인증 ---
    # console 은 링크를 로그로만 찍는다. RESEND_API_KEY 가 비어 있으면
    # resend 로 설정돼 있어도 console 로 내려온다 — 키 없이 clone 해도
    # 앱이 뜨고 테스트가 돌아야 하기 때문이다.
    mail_provider: str = "console"
    resend_api_key: str = ""
    # 도메인이 없으면 Resend 는 이 주소로만 보낼 수 있고, 수신도 계정
    # 소유자 본인에게만 전달된다. 공개 배포 시 no-reply@<도메인> 으로 바꾼다.
    mail_from: str = "onboarding@resend.dev"
    # 인증 링크가 가리키는 프론트엔드 오리진. 백엔드가 아니다 — 메일 링크는
    # /verify 페이지로 가고 그 페이지가 POST 를 친다.
    app_base_url: str = "http://localhost:3000"
    verify_token_ttl_hours: int = 24
    # 미인증 계정의 맛보기 한도. 기존 쿼터와 달리 **계정 수명 전체 누적**이다.
    # "하루 5회"로 두면 미인증 계정이 매일 5회씩 영원히 쓸 수 있어, 막으려던
    # 재가입 어뷰즈가 그대로 통과한다. 0 이면 게이트를 끈다.
    unverified_quota_queries: int = 5
    unverified_quota_documents: int = 1
    # 인증 메일 재발송. 자기 메일함만 채우는 행위지만 Resend 무료 한도가
    # 하루 100통이라 태울 수 있다.
    rate_limit_verify_resend_per_min: int = 1
```

- [ ] **Step 2: 모델 추가**

`app/models.py`의 `User` 클래스에 컬럼과 프로퍼티를 추가한다.

```python
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL 이면 미인증. boolean 이 아니라 시각인 이유는 "인증했나"보다
    # "가입 후 얼마 만에 인증했나"가 어뷰즈 조사에서 실제로 쓰이기 때문이다.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    @property
    def email_verified(self) -> bool:
        """UserOut(from_attributes=True) 이 그대로 읽어간다."""
        return self.email_verified_at is not None
```

`Session` 클래스 **바로 뒤**에 토큰 테이블을 추가한다.

```python
class EmailVerificationToken(Base):
    """1회용 이메일 인증 토큰. 저장하는 것은 sha256 해시이고 원문은 링크에만.

    argon2 를 쓰지 않는 이유는 토큰이 사용자가 고른 비밀번호가 아니라 256비트
    난수라서다. 사전 공격 대상이 아니므로 느린 해시가 방어하는 것이 없고,
    반대로 솔트 때문에 인덱스 조회가 불가능해져 검증마다 전체를 훑게 된다.

    소비된 행은 지우지 않는다. 남겨야 두 번째 클릭에 "만료됐다"가 아니라
    "이미 인증하셨다"고 말할 수 있다.
    """

    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        Index("email_verification_tokens_user_id_idx", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 3: 마이그레이션 작성**

`alembic/versions/0008_email_verification.py`:

```python
"""이메일 인증: 인증 시각 + 1회용 토큰

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-10

기존 계정은 전부 인증된 것으로 채운다. 0003 이 만든 시드 유저는 로그인이
불가능한 계정(UNUSABLE_PASSWORD_HASH)이라 인증할 방법이 아예 없고, eval
코퍼스가 그 계정에 묶여 있다. 미인증으로 두면 되살릴 경로 없이 잠긴다.
게이트는 이 마이그레이션 이후의 새 가입부터 적용된다.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "email_verified_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
    )
    # 기존 계정 grandfather. 새 가입만 NULL 로 들어온다.
    op.execute("UPDATE users SET email_verified_at = now()")

    op.create_table(
        "email_verification_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("consumed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_email_verification_token_hash"),
    )
    op.create_index(
        "email_verification_tokens_user_id_idx",
        "email_verification_tokens",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "email_verification_tokens_user_id_idx",
        table_name="email_verification_tokens",
    )
    op.drop_table("email_verification_tokens")
    op.drop_column("users", "email_verified_at")
```

- [ ] **Step 4: 실패하는 스키마 테스트 작성**

`tests/test_verification.py`:

```python
"""이메일 인증 토큰의 수명. Postgres 가 필요하다."""

import uuid

import pytest
from sqlalchemy import text

from app.db import SessionLocal


pytestmark = pytest.mark.asyncio


async def test_migration_grandfathers_existing_accounts():
    """0008 이전에 있던 계정은 인증된 것으로 남아야 한다.

    특히 0003 의 시드 유저 — 로그인이 불가능한 계정이라 인증할 방법이 없고,
    eval 코퍼스가 거기 묶여 있다. 미인증으로 두면 되살릴 수 없이 잠긴다.
    """
    async with SessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT email_verified_at FROM users "
                "WHERE email = 'seed@local.invalid'"
            )
        )
        row = result.first()

    assert row is not None, "시드 유저가 없다 — 0003 이 적용되지 않았다"
    assert row[0] is not None, "시드 유저가 미인증으로 남았다"


async def test_token_table_exists_with_unique_hash():
    """같은 해시를 두 번 넣을 수 없어야 한다."""
    from app.models import EmailVerificationToken
    from datetime import datetime, timedelta, timezone

    async with SessionLocal() as session:
        user_id = uuid.UUID("00000000-0000-0000-0000-00000000dead")
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        session.add(
            EmailVerificationToken(
                user_id=user_id, token_hash="duplicate-me", expires_at=expires
            )
        )
        await session.commit()

        session.add(
            EmailVerificationToken(
                user_id=user_id, token_hash="duplicate-me", expires_at=expires
            )
        )
        with pytest.raises(Exception):
            await session.commit()
        await session.rollback()

        await session.execute(
            text(
                "DELETE FROM email_verification_tokens "
                "WHERE token_hash = 'duplicate-me'"
            )
        )
        await session.commit()
```

- [ ] **Step 5: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_verification.py -v`
Expected: FAIL — `email_verification_tokens` 테이블이 없어 `ProgrammingError`, 또는 `ImportError: cannot import name 'EmailVerificationToken'`

- [ ] **Step 6: 마이그레이션 적용**

Run: `.venv/bin/python -m alembic upgrade head`
Expected: `Running upgrade 0007 -> 0008, 이메일 인증: 인증 시각 + 1회용 토큰`

- [ ] **Step 7: 테스트가 통과하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_verification.py -v`
Expected: 2 passed

- [ ] **Step 8: 되돌리기가 되는지 확인하고 다시 올린다**

Run: `.venv/bin/python -m alembic downgrade 0007 && .venv/bin/python -m alembic upgrade head`
Expected: 양쪽 다 에러 없이 끝난다. 되돌릴 수 없는 마이그레이션은 배포에서 손발을 묶는다.

- [ ] **Step 9: `.env.example` 갱신**

`RATE_LIMIT_AUTH_PER_MIN` 항목 뒤에 붙인다.

```bash
# --- 이메일 인증 ---
# console 은 링크를 로그로만 찍는다(개발·테스트). resend 로 두더라도
# RESEND_API_KEY 가 비면 console 로 폴백한다.
MAIL_PROVIDER=console
RESEND_API_KEY=
# 도메인이 없으면 Resend 는 이 주소로만 보내고 계정 소유자 본인에게만
# 전달된다. 남이 가입해서 인증까지 마치게 하려면 도메인 + SPF/DKIM 이
# 필요하고, 그때 no-reply@<도메인> 으로 바꾼다.
MAIL_FROM=onboarding@resend.dev
# 인증 링크가 가리키는 곳. 백엔드가 아니라 프론트엔드 오리진이다.
APP_BASE_URL=http://localhost:3000
VERIFY_TOKEN_TTL_HOURS=24
# 미인증 계정의 맛보기 한도. 하루가 아니라 계정 수명 전체 누적이다.
UNVERIFIED_QUOTA_QUERIES=5
UNVERIFIED_QUOTA_DOCUMENTS=1
RATE_LIMIT_VERIFY_RESEND_PER_MIN=1
```

- [ ] **Step 10: 커밋**

```bash
git add app/config.py app/models.py alembic/versions/0008_email_verification.py .env.example tests/test_verification.py
git commit -m "$(cat <<'MSG'
feat(schema): 이메일 인증 스키마

users.email_verified_at 과 1회용 토큰 테이블. 저장하는 것은 sha256 해시라
DB 가 유출돼도 링크를 만들 수 없다.

기존 계정은 마이그레이션에서 전부 인증 처리한다. 0003 의 시드 유저는
로그인이 불가능한 계정이라 인증할 방법이 없고 eval 코퍼스가 거기 묶여
있어서, 미인증으로 두면 되살릴 경로 없이 잠긴다.
MSG
)"
```

---

### Task 2: 토큰 서비스

**Files:**
- Create: `app/services/verification.py`
- Modify: `tests/test_verification.py`

**Interfaces:**
- Consumes: Task 1의 `EmailVerificationToken`, `User.email_verified_at`, `settings.verify_token_ttl_hours`, `settings.app_base_url`
- Produces:
  - `hash_token(raw: str) -> str`
  - `issue_token(session: AsyncSession, user_id: uuid.UUID) -> str` — 원문 반환, 기존 미소비 토큰 삭제
  - `consume_token(session: AsyncSession, raw: str) -> tuple[VerifyResult, User | None]`
  - `VerifyResult` — `OK` / `ALREADY` / `INVALID`
  - `build_link(raw: str) -> str`
  - `build_email(link: str) -> tuple[str, str, str]` — (subject, html, text)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_verification.py` 끝에 덧붙인다.

```python
# --- 토큰 수명 ---

async def _make_user(session, email: str):
    from app.services import auth

    return await auth.create_user(session, email, "password-123")


async def test_raw_token_is_never_stored():
    """DB 에는 해시만 있어야 한다. 유출돼도 링크를 만들 수 없어야 하므로."""
    from app.services import verification

    async with SessionLocal() as session:
        user = await _make_user(session, f"raw-{uuid.uuid4().hex}@example.com")
        raw = await verification.issue_token(session, user.id)

        result = await session.execute(
            text("SELECT token_hash FROM email_verification_tokens "
                 "WHERE user_id = :uid").bindparams(uid=str(user.id))
        )
        stored = [row[0] for row in result]

    assert stored == [verification.hash_token(raw)]
    assert raw not in stored


async def test_valid_token_marks_the_user_verified():
    from app.services import verification

    async with SessionLocal() as session:
        user = await _make_user(session, f"ok-{uuid.uuid4().hex}@example.com")
        assert user.email_verified is False

        raw = await verification.issue_token(session, user.id)
        result, verified = await verification.consume_token(session, raw)

    assert result is verification.VerifyResult.OK
    assert verified is not None
    assert verified.email_verified is True


async def test_a_consumed_token_reports_already_verified():
    """두 번째 클릭은 '만료됐다'가 아니라 '이미 인증하셨다'여야 한다."""
    from app.services import verification

    async with SessionLocal() as session:
        user = await _make_user(session, f"twice-{uuid.uuid4().hex}@example.com")
        raw = await verification.issue_token(session, user.id)
        await verification.consume_token(session, raw)

        result, _ = await verification.consume_token(session, raw)

    assert result is verification.VerifyResult.ALREADY


async def test_an_expired_token_is_refused():
    from datetime import datetime, timedelta, timezone

    from app.services import verification

    async with SessionLocal() as session:
        user = await _make_user(session, f"old-{uuid.uuid4().hex}@example.com")
        raw = await verification.issue_token(session, user.id)
        await session.execute(
            text("UPDATE email_verification_tokens SET expires_at = :past "
                 "WHERE user_id = :uid").bindparams(
                     past=datetime.now(timezone.utc) - timedelta(seconds=1),
                     uid=str(user.id),
                 )
        )
        await session.commit()

        result, _ = await verification.consume_token(session, raw)

    assert result is verification.VerifyResult.INVALID


async def test_an_unknown_token_is_refused_the_same_way_as_an_expired_one():
    """둘을 나누면 임의 문자열을 던져 토큰 존재 여부를 캐낼 수 있게 된다."""
    from app.services import verification

    async with SessionLocal() as session:
        result, user = await verification.consume_token(session, "no-such-token")

    assert result is verification.VerifyResult.INVALID
    assert user is None


async def test_reissuing_kills_the_previous_link():
    """살아 있는 링크가 둘이면 어느 쪽이 유효한지 사용자가 알 수 없다."""
    from app.services import verification

    async with SessionLocal() as session:
        user = await _make_user(session, f"resend-{uuid.uuid4().hex}@example.com")
        first = await verification.issue_token(session, user.id)
        second = await verification.issue_token(session, user.id)

        stale, _ = await verification.consume_token(session, first)
        fresh, _ = await verification.consume_token(session, second)

    assert stale is verification.VerifyResult.INVALID
    assert fresh is verification.VerifyResult.OK


async def test_reissuing_keeps_consumed_rows():
    """소비 기록까지 지우면 '이미 인증하셨다'를 말할 수 없게 된다."""
    from app.services import verification

    async with SessionLocal() as session:
        user = await _make_user(session, f"keep-{uuid.uuid4().hex}@example.com")
        used = await verification.issue_token(session, user.id)
        await verification.consume_token(session, used)
        await verification.issue_token(session, user.id)

        result, _ = await verification.consume_token(session, used)

    assert result is verification.VerifyResult.ALREADY


def test_link_points_at_the_frontend_not_the_api():
    """메일 스캐너가 GET 을 미리 밟아도 토큰이 타지 않아야 한다."""
    from app.config import settings
    from app.services import verification

    link = verification.build_link("abc123")

    assert link.startswith(settings.app_base_url)
    assert "/verify?token=abc123" in link


def test_email_body_carries_the_link_in_both_parts():
    from app.services import verification

    subject, html, plain = verification.build_email("https://example.test/verify?token=x")

    assert subject
    assert "https://example.test/verify?token=x" in html
    assert "https://example.test/verify?token=x" in plain
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_verification.py -v`
Expected: 새 테스트들이 `ModuleNotFoundError: No module named 'app.services.verification'`로 실패

- [ ] **Step 3: 서비스 구현**

`app/services/verification.py`:

```python
"""이메일 인증 토큰: 발급 · 소비 · 무효화.

토큰은 세션과 같은 방식으로 다룬다 — 서버에 행이 있고, 지우면 즉시 죽는다.
서명 토큰(JWT/itsdangerous)을 쓰지 않은 이유도 세션에서와 같다: 발급한
링크를 취소할 수 없기 때문이다.

저장하는 것은 sha256 해시다. argon2 가 아닌 이유는 토큰이 사용자가 고른
비밀번호가 아니라 256비트 난수라서 사전 공격 대상이 아니고, 솔트 때문에
인덱스 조회가 불가능해지면 검증마다 미소비 토큰 전체를 훑어야 하기 때문이다.

이 모듈은 DB 만 안다. HTTP 상태 코드로의 번역은 라우터가 한다.
"""

import enum
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import EmailVerificationToken, User

# 256비트. 무차별 대입이 의미를 갖지 못하는 크기라서 해시를 느리게 만들
# 필요가 없다.
_TOKEN_BYTES = 32


class VerifyResult(enum.Enum):
    OK = "ok"
    ALREADY = "already"
    INVALID = "invalid"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def issue_token(session: AsyncSession, user_id: uuid.UUID) -> str:
    """새 토큰을 발급하고 **원문**을 돌려준다. 원문은 여기서만 존재한다.

    아직 쓰지 않은 이전 토큰은 지운다. 살아 있는 링크가 둘이면 어느 쪽이
    유효한지 사용자가 알 수 없다. 이미 소비된 행은 남긴다 — 그게 두 번째
    클릭에 "이미 인증하셨다"고 말할 수 있는 근거다.
    """
    await session.execute(
        delete(EmailVerificationToken).where(
            EmailVerificationToken.user_id == user_id,
            EmailVerificationToken.consumed_at.is_(None),
        )
    )

    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    session.add(
        EmailVerificationToken(
            user_id=user_id,
            token_hash=hash_token(raw),
            expires_at=datetime.now(timezone.utc)
            + timedelta(hours=settings.verify_token_ttl_hours),
        )
    )
    await session.commit()
    return raw


async def consume_token(
    session: AsyncSession, raw: str
) -> tuple[VerifyResult, User | None]:
    """토큰을 한 번 쓰고 사용자를 인증 처리한다.

    만료와 "그런 토큰 없음"을 같은 INVALID 로 묶는 것은 의도된 것이다.
    나누면 임의의 문자열을 던져 토큰의 존재 여부를 물을 수 있게 된다.
    """
    result = await session.execute(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == hash_token(raw)
        )
    )
    row = result.scalars().first()
    if row is None:
        return VerifyResult.INVALID, None

    if row.consumed_at is not None:
        return VerifyResult.ALREADY, None

    now = datetime.now(timezone.utc)
    if row.expires_at <= now:
        return VerifyResult.INVALID, None

    user = await session.get(User, row.user_id)
    if user is None:  # 계정이 그사이 지워졌다
        return VerifyResult.INVALID, None

    row.consumed_at = now
    user.email_verified_at = now
    await session.commit()
    await session.refresh(user)
    return VerifyResult.OK, user


def build_link(raw: str) -> str:
    """프론트엔드의 착지 페이지를 가리킨다. 백엔드가 아니다.

    메일 클라이언트와 보안 스캐너가 본문 링크를 미리 GET 으로 밟는다. 링크가
    곧 검증 엔드포인트면 사용자가 클릭하기 전에 토큰이 소진되고, 화면에는
    "이미 사용된 링크"가 뜬다.
    """
    return f"{settings.app_base_url.rstrip('/')}/verify?token={quote(raw)}"


def build_email(link: str) -> tuple[str, str, str]:
    """(제목, HTML, 평문). 평문은 HTML 을 막아둔 클라이언트를 위한 것이다."""
    subject = "이메일 주소를 확인해 주세요"
    hours = settings.verify_token_ttl_hours
    plain = (
        "아래 주소를 열면 이메일 확인이 끝납니다.\n\n"
        f"{link}\n\n"
        f"이 링크는 {hours}시간 뒤에 닫힙니다.\n"
        "이 가입을 요청한 적이 없다면 아무것도 하지 않으셔도 됩니다."
    )
    html = (
        '<div style="font-family:system-ui,sans-serif;line-height:1.6">'
        "<p>아래 버튼을 누르면 이메일 확인이 끝납니다.</p>"
        f'<p><a href="{link}">이메일 확인하기</a></p>'
        f"<p>이 링크는 {hours}시간 뒤에 닫힙니다.</p>"
        "<p>이 가입을 요청한 적이 없다면 아무것도 하지 않으셔도 됩니다.</p>"
        "</div>"
    )
    return subject, html, plain
```

- [ ] **Step 4: 테스트가 통과하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_verification.py -v`
Expected: 11 passed

- [ ] **Step 5: 커밋**

```bash
git add app/services/verification.py tests/test_verification.py
git commit -m "$(cat <<'MSG'
feat(verification): 1회용 인증 토큰 발급·소비

세션과 같은 방식이다 — 서버에 행이 있고 지우면 즉시 죽는다. 저장하는 것은
sha256 해시라 DB 가 유출돼도 링크를 만들 수 없다.

만료와 "그런 토큰 없음"을 같은 결과로 묶었다. 나누면 임의의 문자열을 던져
토큰 존재 여부를 캐낼 수 있게 된다.

링크는 백엔드가 아니라 프론트 /verify 를 가리킨다. 메일 스캐너가 본문
링크를 미리 GET 으로 밟기 때문에, 링크가 곧 검증 엔드포인트면 사용자가
클릭하기 전에 토큰이 소진된다.
MSG
)"
```

---

### Task 3: 메일러

**Files:**
- Create: `app/services/mailer.py`
- Create: `tests/test_mailer.py`

**Interfaces:**
- Consumes: `settings.mail_provider`, `settings.resend_api_key`, `settings.mail_from`
- Produces:
  - `Mailer` 프로토콜 — `async def send(self, *, to: str, subject: str, html: str, text: str) -> None`
  - `ConsoleMailer`, `ResendMailer`
  - `get_mailer() -> Mailer`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_mailer.py`:

```python
"""메일 전송 어댑터. 인프라가 필요 없다 — 네트워크는 가짜로 대체한다."""

import pytest

from app.config import settings
from app.services import mailer


def test_missing_key_falls_back_to_console():
    """키 없이 clone 해도 앱이 뜨고 테스트가 돌아야 한다."""
    original_provider, original_key = settings.mail_provider, settings.resend_api_key
    try:
        settings.mail_provider = "resend"
        settings.resend_api_key = ""
        assert isinstance(mailer.get_mailer(), mailer.ConsoleMailer)
    finally:
        settings.mail_provider, settings.resend_api_key = original_provider, original_key


def test_resend_is_used_when_configured():
    original_provider, original_key = settings.mail_provider, settings.resend_api_key
    try:
        settings.mail_provider = "resend"
        settings.resend_api_key = "re_test_key"
        assert isinstance(mailer.get_mailer(), mailer.ResendMailer)
    finally:
        settings.mail_provider, settings.resend_api_key = original_provider, original_key


@pytest.mark.asyncio
async def test_console_mailer_logs_the_link(caplog):
    """개발 중에는 로그가 메일함이다. 링크가 보이지 않으면 쓸모가 없다."""
    with caplog.at_level("INFO"):
        await mailer.ConsoleMailer().send(
            to="who@example.com",
            subject="제목",
            html="<a href='https://example.test/verify?token=abc'>x</a>",
            text="https://example.test/verify?token=abc",
        )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "who@example.com" in logged
    assert "https://example.test/verify?token=abc" in logged


@pytest.mark.asyncio
async def test_resend_posts_the_expected_payload(monkeypatch):
    """계약을 고정한다. 필드 이름이 틀리면 조용히 안 보내진다."""
    captured: dict = {}

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr(mailer.httpx, "AsyncClient", _Client)

    await mailer.ResendMailer(api_key="re_test_key", sender="no-reply@example.test").send(
        to="who@example.com", subject="제목", html="<p>본문</p>", text="본문"
    )

    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer re_test_key"
    assert captured["json"]["from"] == "no-reply@example.test"
    assert captured["json"]["to"] == ["who@example.com"]
    assert captured["json"]["subject"] == "제목"
    assert captured["json"]["html"] == "<p>본문</p>"
    assert captured["json"]["text"] == "본문"
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_mailer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.mailer'`

- [ ] **Step 3: 메일러 구현**

`app/services/mailer.py`:

```python
"""메일 전송. 전송만 한다 — 무슨 내용을 보낼지는 호출자가 정한다.

어댑터가 둘인 이유는 개발과 운영이 서로를 막지 않게 하기 위해서다.
RESEND_API_KEY 가 없으면 콘솔로 내려오므로, 키 없이 clone 한 사람도 앱을
띄우고 전체 테스트를 돌릴 수 있다.

SMTP 가 아니라 HTTP API 를 쓰는 이유: 많은 호스팅이 25/465/587 포트를
막아두는데, 그 경우 SMTP 는 타임아웃으로만 실패해서 원인을 찾기 어렵다.
"""

import logging
from typing import Protocol

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"
# 발송은 요청 밖(BackgroundTasks)에서 일어나지만, 무한정 매달려 있으면
# 워커를 잡는다.
_TIMEOUT = 10.0


class Mailer(Protocol):
    async def send(self, *, to: str, subject: str, html: str, text: str) -> None: ...


class ConsoleMailer:
    """링크를 로그로 찍는다. 개발과 테스트에서 이게 메일함이다."""

    async def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        logger.info("[mail] to=%s subject=%s\n%s", to, subject, text)


class ResendMailer:
    def __init__(self, api_key: str, sender: str) -> None:
        self._api_key = api_key
        self._sender = sender

    async def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                _RESEND_ENDPOINT,
                json={
                    "from": self._sender,
                    "to": [to],
                    "subject": subject,
                    "html": html,
                    "text": text,
                },
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()


def get_mailer() -> Mailer:
    """설정이 resend 라도 키가 없으면 콘솔로 내려온다.

    조용히 성공한 척하는 것보다 로그에 남기는 편이 낫고, 키가 없다는 이유로
    앱이 뜨지 않는 것보다도 낫다.
    """
    if settings.mail_provider == "resend" and settings.resend_api_key:
        return ResendMailer(settings.resend_api_key, settings.mail_from)
    if settings.mail_provider == "resend":
        logger.warning("MAIL_PROVIDER=resend 인데 RESEND_API_KEY 가 비어 콘솔로 보낸다")
    return ConsoleMailer()
```

- [ ] **Step 4: 테스트가 통과하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_mailer.py -v`
Expected: 4 passed

- [ ] **Step 5: 커밋**

```bash
git add app/services/mailer.py tests/test_mailer.py
git commit -m "$(cat <<'MSG'
feat(mailer): Resend HTTP API + 콘솔 폴백

SMTP 가 아니라 HTTP API 다. 많은 호스팅이 25/465/587 을 막아두는데 그
경우 SMTP 는 타임아웃으로만 실패해 원인을 찾기 어렵다.

RESEND_API_KEY 가 비면 콘솔 어댑터로 내려온다. 키 없이 clone 한 사람도
앱을 띄우고 전체 테스트를 돌릴 수 있어야 한다.
MSG
)"
```

---

### Task 4: 인증 API — signup 훅 · verify · resend

**Files:**
- Modify: `app/schemas.py`
- Modify: `app/services/ratelimit.py`
- Modify: `app/routers/auth.py`
- Modify: `tests/test_error_details.py`
- Create: `tests/test_verification_api.py`

**Interfaces:**
- Consumes: Task 2의 `verification.issue_token` / `consume_token` / `VerifyResult` / `build_link` / `build_email`, Task 3의 `mailer.get_mailer`
- Produces:
  - `POST /auth/verify` — 본문 `{"token": "..."}` → 200 `UserOut` / 400 `invalid or expired token` / 409 `email already verified`
  - `POST /auth/resend-verification` — 204 / 409 `email already verified` / 429 `verification email rate limit exceeded`
  - `UserOut.email_verified: bool`
  - `verify_resend_limiter`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_verification_api.py`:

```python
"""인증 라우트. Postgres 가 필요하다 (메일은 콘솔 어댑터로 나간다)."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.services import verification
from app.services.ratelimit import auth_limiter, verify_resend_limiter

pytestmark = pytest.mark.asyncio


async def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    )


async def _signup(client: AsyncClient) -> tuple[str, dict]:
    auth_limiter.reset()
    email = f"verify-{uuid.uuid4().hex}@example.com"
    response = await client.post(
        "/auth/signup", json={"email": email, "password": "password-123"}
    )
    assert response.status_code == 201, response.text
    return email, response.json()


async def _token_for(email: str) -> str:
    """콘솔 어댑터로는 메일을 읽을 수 없으므로 새로 발급해 대신 쓴다."""
    from app.services import auth

    async with SessionLocal() as session:
        user = await auth.get_user_by_email(session, email)
        assert user is not None
        return await verification.issue_token(session, user.id)


async def test_signup_starts_unverified():
    async with await _client() as client:
        _, body = await _signup(client)

    assert body["email_verified"] is False


async def test_verify_marks_the_account_and_me_reflects_it():
    async with await _client() as client:
        email, _ = await _signup(client)
        token = await _token_for(email)

        response = await client.post("/auth/verify", json={"token": token})
        assert response.status_code == 200, response.text
        assert response.json()["email_verified"] is True

        me = await client.get("/auth/me")
        assert me.status_code == 200
        assert me.json()["email_verified"] is True


async def test_a_second_click_says_already_verified():
    async with await _client() as client:
        email, _ = await _signup(client)
        token = await _token_for(email)
        await client.post("/auth/verify", json={"token": token})

        again = await client.post("/auth/verify", json={"token": token})

    assert again.status_code == 409
    assert again.json()["detail"] == "email already verified"


async def test_a_bad_token_is_a_400():
    async with await _client() as client:
        await _signup(client)
        response = await client.post("/auth/verify", json={"token": "nonsense"})

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid or expired token"


async def test_verify_needs_no_session():
    """메일 링크는 다른 브라우저에서 열린다. 로그인 상태를 요구하면 막힌다."""
    async with await _client() as client:
        email, _ = await _signup(client)
        token = await _token_for(email)

    async with await _client() as fresh:  # 쿠키 없는 새 클라이언트
        response = await fresh.post("/auth/verify", json={"token": token})

    assert response.status_code == 200


async def test_resend_requires_a_session():
    verify_resend_limiter.reset()
    async with await _client() as client:
        response = await client.post("/auth/resend-verification")

    assert response.status_code == 401


async def test_resend_is_throttled():
    verify_resend_limiter.reset()
    async with await _client() as client:
        await _signup(client)

        first = await client.post("/auth/resend-verification")
        second = await client.post("/auth/resend-verification")

    assert first.status_code == 204
    assert second.status_code == 429
    assert second.json()["detail"] == "verification email rate limit exceeded"


async def test_resend_to_a_verified_account_is_a_409():
    verify_resend_limiter.reset()
    async with await _client() as client:
        email, _ = await _signup(client)
        token = await _token_for(email)
        await client.post("/auth/verify", json={"token": token})

        response = await client.post("/auth/resend-verification")

    assert response.status_code == 409
    assert response.json()["detail"] == "email already verified"
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_verification_api.py -v`
Expected: FAIL — `ImportError: cannot import name 'verify_resend_limiter'`

- [ ] **Step 3: 스키마 확장**

`app/schemas.py`의 `UserOut`을 고치고, `LoginRequest` 뒤에 `VerifyRequest`를 넣는다.

```python
class VerifyRequest(BaseModel):
    # 링크에서 온 토큰. GET 이 아니라 본문으로 받는 이유는 메일 스캐너가
    # 링크를 미리 밟아 1회용 토큰을 태우기 때문이다.
    token: str = Field(..., min_length=1, max_length=512)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    # User.email_verified 프로퍼티에서 읽어온다.
    email_verified: bool = False
    created_at: datetime | None = None
```

- [ ] **Step 4: 리미터 추가**

`app/services/ratelimit.py` 맨 끝에 붙인다.

```python
# 인증 메일 재발송. 세션이 필요하므로 남의 메일함은 채울 수 없지만, Resend
# 무료 한도가 하루 100통이라 자기 계정으로 그걸 태울 수는 있다.
verify_resend_limiter = TokenBucketLimiter(settings.rate_limit_verify_resend_per_min)
```

- [ ] **Step 5: 라우터 구현**

`app/routers/auth.py`의 import 블록에 추가한다.

```python
from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Request, Response
from app.schemas import LoginRequest, SignupRequest, UserOut, VerifyRequest
from app.services import auth, mailer, verification
from app.services.ratelimit import auth_limiter, verify_resend_limiter
```

`_set_session_cookie` 뒤에 발송 헬퍼를 넣는다.

```python
async def _deliver_verification(email: str, raw_token: str) -> None:
    """응답 밖에서 실행된다. 여기서 나는 예외는 요청에 영향을 주지 않는다.

    대가로 사용자는 발송 실패를 알 수 없다 — /auth/resend-verification 이
    그 유일한 복구 경로이므로 화면에서 항상 닿을 수 있어야 한다.
    """
    subject, html, text = verification.build_email(
        verification.build_link(raw_token)
    )
    try:
        await mailer.get_mailer().send(
            to=email, subject=subject, html=html, text=text
        )
    except Exception:  # noqa: BLE001 - 배달 실패가 가입을 되돌리면 안 된다
        logger.exception("인증 메일 발송 실패: %s", email)
```

파일 상단(라우터 정의 위)에 로거를 둔다.

```python
import logging

logger = logging.getLogger(__name__)
```

`signup`에 `background: BackgroundTasks`를 받고 토큰 발급을 붙인다.

```python
@router.post("/signup", status_code=http_status.HTTP_201_CREATED, response_model=UserOut)
async def signup(
    body: SignupRequest,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> User:
    _guard_attempts(request, body.email)
    try:
        user = await auth.create_user(session, body.email, body.password)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="email already registered",
        ) from None

    # 토큰 발급은 요청 안에서(DB 가 필요하다), 발송만 밖에서.
    raw = await verification.issue_token(session, user.id)
    background.add_task(_deliver_verification, user.email, raw)

    row = await auth.create_session(session, user.id)
    _set_session_cookie(response, row.id)
    return user
```

`me` 앞에 두 라우트를 추가한다.

```python
@router.post("/verify", response_model=UserOut)
async def verify(
    body: VerifyRequest,
    session: AsyncSession = Depends(get_session),
) -> User:
    """세션을 요구하지 않는다 — 메일 링크는 다른 브라우저에서 열린다."""
    result, user = await verification.consume_token(session, body.token)

    if result is verification.VerifyResult.ALREADY:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="email already verified",
        )
    if result is not verification.VerifyResult.OK or user is None:
        # 만료와 "그런 토큰 없음"을 나누지 않는다. 나누면 임의의 문자열을
        # 던져 토큰의 존재 여부를 물을 수 있게 된다.
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired token",
        )
    return user


@router.post("/resend-verification", status_code=http_status.HTTP_204_NO_CONTENT)
async def resend_verification(
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """이메일 주소가 아니라 세션을 받는다.

    주소를 받으면 "이 주소가 가입돼 있나"를 묻는 열거 창구가 하나 더 생긴다.
    """
    if user.email_verified:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="email already verified",
        )
    if not verify_resend_limiter.allow(f"user:{user.id}"):
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="verification email rate limit exceeded",
            headers={"Retry-After": "60"},
        )

    raw = await verification.issue_token(session, user.id)
    background.add_task(_deliver_verification, user.email, raw)
```

- [ ] **Step 6: 테스트가 통과하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_verification_api.py -v`
Expected: 8 passed

> **`tests/test_error_details.py`는 이 태스크에서 건드리지 않는다.** 그 테스트는 백엔드의 detail 문자열과 프론트의 `errors.ts` 표를 **양방향**으로 대조하므로, 둘 중 하나만 있으면 어느 쪽을 먼저 넣어도 빨간불이다. 새 detail 넷과 그 카피는 Task 7에서 한 번에 들어가고, 거기서 이 테스트가 다시 초록불이 된다. Task 4~6 동안에는 아래처럼 제외하고 돌린다.

- [ ] **Step 7: 전체 테스트로 회귀 확인**

Run: `.venv/bin/python -m pytest tests/ -v -x --ignore=tests/test_error_details.py`
Expected: 기존 테스트가 전부 통과. `UserOut`에 필드가 늘어난 것은 기존 응답을 깨지 않는다.

- [ ] **Step 8: 커밋**

```bash
git add app/schemas.py app/services/ratelimit.py app/routers/auth.py tests/test_verification_api.py
git commit -m "$(cat <<'MSG'
feat(auth): 가입 시 인증 메일, /auth/verify 와 재발송

토큰 발급은 요청 안에서(DB 가 필요하다), 발송만 BackgroundTasks 로 뺐다.
Resend 가 느리거나 죽었을 때 가입 응답이 같이 막히면 안 된다. 대가로
사용자는 발송 실패를 알 수 없어서, 재발송이 유일한 복구 경로가 된다.

재발송은 이메일 주소가 아니라 세션을 받는다. 주소를 받으면 "이 주소가
가입돼 있나"를 묻는 열거 창구가 하나 더 생긴다.

/auth/verify 는 세션을 요구하지 않는다 — 메일 링크는 다른 브라우저에서
열린다.
MSG
)"
```

---

### Task 5: 게이트 — 미인증 맛보기 쿼터

**Files:**
- Modify: `app/services/usage.py`
- Modify: `app/deps.py`
- Modify: `app/services/pipeline.py:105-112`
- Modify: `app/routers/documents.py:100-113`
- Modify: `tests/test_error_details.py`
- Create: `tests/test_unverified_gate.py`

**Interfaces:**
- Consumes: Task 1의 `User.email_verified`, `settings.unverified_quota_queries`, `settings.unverified_quota_documents`
- Produces:
  - `usage.queries_total(session, user_id) -> int`
  - `usage.documents_total(session, user_id) -> int`
  - `usage.unverified_query_exceeded(session, user_id) -> bool`
  - `usage.unverified_upload_exceeded(session, user_id) -> bool`
  - `deps.VERIFICATION_REQUIRED` — 403 `email verification required`
  - 새 usage 이벤트 종류 `"upload"` (업로드 **수락** 시점, 동기)

> **설계 문서 수정 — 무엇을 세는가.** 스펙은 "`usage_events`의 ingest 행을
> 센다"고 적었는데 그대로는 게이트가 새어나간다. `ingest` 행은 백그라운드
> 인덱싱이 **성공한 뒤에야** 생긴다(`app/services/ingest.py:82`). 결과가 둘:
> ① 인덱싱이 끝나기 전에 업로드를 연달아 던지면 카운터가 0인 채로 전부
> 통과한다(업로드 레이트리밋 5/분이 유일한 방벽), ② 깨진 PDF나 임베딩 서버
> 장애로 실패한 업로드는 아예 세어지지 않는다.
>
> 그래서 업로드가 **수락되는 시점에** 동기로 `kind="upload"` 이벤트를 남기고
> 게이트는 그것을 센다. `kind="ingest"`는 지금처럼 인덱싱 성공 시에만 쓰고
> 쪽수 쿼터가 계속 사용한다. 둘을 나눈 덕에 "수락 5 / 색인 3 = 실패 2"가
> 그냥 읽히는 것은 덤이다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_unverified_gate.py`:

```python
"""미인증 계정의 맛보기 쿼터. Postgres 가 필요하다.

핵심은 한도가 아니라 **창**이다. 기존 쿼터처럼 "오늘"로 세면 미인증 계정이
매일 다시 5회를 받아, 막으려던 재가입 어뷰즈가 그대로 통과한다.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal
from app.services import usage

pytestmark = pytest.mark.asyncio


async def _fresh_user(session, verified: bool):
    from app.services import auth

    user = await auth.create_user(
        session, f"gate-{uuid.uuid4().hex}@example.com", "password-123"
    )
    if verified:
        user.email_verified_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(user)
    return user


async def _record_queries(session, user_id, count: int) -> None:
    for _ in range(count):
        await usage.record(session, user_id, "query")


async def _record_uploads(session, user_id, count: int) -> None:
    """업로드 '수락' 이벤트. 인덱싱 성공 여부와 무관하게 남는다."""
    for _ in range(count):
        await usage.record(session, user_id, "upload")


async def test_query_gate_opens_and_then_closes():
    async with SessionLocal() as session:
        user = await _fresh_user(session, verified=False)

        assert await usage.unverified_query_exceeded(session, user.id) is False

        await _record_queries(session, user.id, settings.unverified_quota_queries)

        assert await usage.unverified_query_exceeded(session, user.id) is True


async def test_the_query_window_is_lifetime_not_today():
    """어제 쓴 것도 센다. '오늘'로 세면 매일 5회가 다시 열린다."""
    async with SessionLocal() as session:
        user = await _fresh_user(session, verified=False)
        await _record_queries(session, user.id, settings.unverified_quota_queries)

        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        await session.execute(
            text(
                "UPDATE usage_events SET created_at = :then WHERE user_id = :uid"
            ).bindparams(then=yesterday, uid=str(user.id))
        )
        await session.commit()

        assert await usage.queries_today(session, user.id) == 0
        assert await usage.unverified_query_exceeded(session, user.id) is True


async def test_upload_gate_counts_accepted_uploads_not_surviving_documents():
    """올렸다 지워도 되돌아가지 않아야 한다.

    usage_events 는 documents 가 아니라 users 를 참조하므로 문서를 지워도
    행이 남는다. documents 를 셌다면 지우기로 한도가 초기화된다.
    """
    async with SessionLocal() as session:
        user = await _fresh_user(session, verified=False)

        await _record_uploads(session, user.id, settings.unverified_quota_documents)

        assert await usage.unverified_upload_exceeded(session, user.id) is True

        # 문서를 전부 지운 상황을 흉내낸다 (애초에 documents 행이 없다).
        assert await usage.documents_total(session, user.id) == (
            settings.unverified_quota_documents
        )


async def test_indexing_events_do_not_count_toward_the_upload_gate():
    """수락과 색인은 다른 사건이다. ingest 를 세면 색인이 끝나기 전에
    던진 업로드가 전부 통과하고, 실패한 업로드는 아예 세어지지 않는다."""
    async with SessionLocal() as session:
        user = await _fresh_user(session, verified=False)
        await usage.record(session, user.id, "ingest", pages=40)

        assert await usage.documents_total(session, user.id) == 0
        assert await usage.unverified_upload_exceeded(session, user.id) is False


async def test_a_verified_account_is_not_gated():
    async with SessionLocal() as session:
        user = await _fresh_user(session, verified=True)
        await _record_queries(session, user.id, settings.unverified_quota_queries + 5)

        assert user.email_verified is True
        # 게이트 함수 자체는 인증 여부를 묻지 않는다 — 호출자가 먼저 본다.
        # 여기서 확인하는 것은 정상 쿼터가 아직 살아 있다는 것.
        assert await usage.query_quota_exceeded(session, user.id) is False


async def test_zero_disables_the_gate():
    original = settings.unverified_quota_queries
    try:
        settings.unverified_quota_queries = 0
        async with SessionLocal() as session:
            user = await _fresh_user(session, verified=False)
            await _record_queries(session, user.id, 3)
            assert await usage.unverified_query_exceeded(session, user.id) is False
    finally:
        settings.unverified_quota_queries = original
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_unverified_gate.py -v`
Expected: FAIL — `AttributeError: module 'app.services.usage' has no attribute 'unverified_query_exceeded'`

- [ ] **Step 3: usage 헬퍼 추가**

`app/services/usage.py`의 `pages_this_month` 뒤에 넣는다.

```python
async def queries_total(session: AsyncSession, user_id: uuid.UUID) -> int:
    """계정 수명 전체의 질의 수. 시간 창이 없는 것이 요점이다."""
    result = await session.execute(
        select(func.count(UsageEvent.id)).where(
            UsageEvent.user_id == user_id,
            UsageEvent.kind == "query",
        )
    )
    return int(result.scalar_one())


async def documents_total(session: AsyncSession, user_id: uuid.UUID) -> int:
    """지금까지 **수락된** 업로드 수.

    ``documents`` 를 세지 않는다. ``UsageEvent`` 의 외래키는 ``documents``
    가 아니라 ``users`` 를 향하므로 문서를 지워도 행이 남고, 올렸다 지워서
    한도를 되돌리는 우회가 구조적으로 막힌다.

    ``ingest`` 도 세지 않는다. 그 행은 백그라운드 인덱싱이 성공한 뒤에야
    생겨서, 색인이 끝나기 전에 연달아 던진 업로드는 카운터가 0인 채로 전부
    통과하고 실패한 업로드는 아예 세어지지 않는다. ``upload`` 는 업로드를
    수락하는 그 요청 안에서 기록된다.
    """
    result = await session.execute(
        select(func.count(UsageEvent.id)).where(
            UsageEvent.user_id == user_id,
            UsageEvent.kind == "upload",
        )
    )
    return int(result.scalar_one())


async def unverified_query_exceeded(
    session: AsyncSession, user_id: uuid.UUID
) -> bool:
    """미인증 계정의 맛보기 질의 한도를 넘겼는지. 0 이면 게이트를 끈다.

    호출자가 인증 여부를 먼저 확인한다 — 이 함수는 세기만 한다.
    """
    if settings.unverified_quota_queries <= 0:
        return False
    return await queries_total(session, user_id) >= settings.unverified_quota_queries


async def unverified_upload_exceeded(
    session: AsyncSession, user_id: uuid.UUID
) -> bool:
    if settings.unverified_quota_documents <= 0:
        return False
    return (
        await documents_total(session, user_id)
        >= settings.unverified_quota_documents
    )
```

- [ ] **Step 4: 공용 403 정의**

`app/deps.py`의 `_UNAUTHENTICATED` 뒤에 넣는다.

```python
# 403 이고 429 가 아닌 이유: 기다린다고 풀리지 않는다. 사용자가 메일의
# 링크를 눌러야 한다. 429 로 두면 프론트가 "잠시 뒤에 다시"라는 틀린
# 조언을 하게 된다.
VERIFICATION_REQUIRED = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN, detail="email verification required"
)
```

- [ ] **Step 5: 질의 게이트**

`app/services/pipeline.py`의 import에 추가한다.

```python
from app.deps import VERIFICATION_REQUIRED
```

`enforce_limits`를 고친다.

```python
    async def enforce_limits(self, client_ip: str) -> None:
        if not query_limiter.allow(f"user:{self.user.id}") or not query_limiter.allow(
            f"ip:{client_ip}"
        ):
            raise too_many("query rate limit exceeded")
        # 미인증 계정은 다른 한도를 다른 창으로 센다 — 하루가 아니라 계정
        # 수명 전체. 그래야 재가입으로 초기화되지 않는다.
        if not self.user.email_verified:
            if await usage.unverified_query_exceeded(self.session, self.user.id):
                raise VERIFICATION_REQUIRED
        elif await usage.query_quota_exceeded(self.session, self.user.id):
            raise too_many("daily query quota exceeded")
```

- [ ] **Step 6: 업로드 게이트와 수락 이벤트**

`app/routers/documents.py`의 import에 추가한다.

```python
from app.deps import VERIFICATION_REQUIRED, get_current_user
```

레이트 리밋 블록 **바로 뒤**, `data = await file.read()` **앞**에 게이트를 넣는다. 파일을 읽기 전에 막아 바이트를 낭비하지 않는다.

```python
    # 인증 여부는 파일을 읽기 전에 본다 — 거절할 업로드에 50MB 를 읽을
    # 이유가 없다.
    if not user.email_verified and await ingest.usage.unverified_upload_exceeded(
        session, user.id
    ):
        raise VERIFICATION_REQUIRED
```

그리고 문서 행을 만든 직후 — `await session.refresh(doc)` **바로 뒤**, 파일을 디스크에 쓰기 전에 — 수락 이벤트를 남긴다.

```python
    # 수락 시점에 남긴다. 색인 성공 시 기록되는 ``ingest`` 와 다른 사건이다:
    # 그쪽은 백그라운드가 끝나야 생겨서, 색인 전에 연달아 던진 업로드가
    # 카운터를 0으로 본 채 전부 통과하고 실패한 업로드는 세어지지도 않는다.
    await ingest.usage.record(session, user.id, "upload")
```

- [ ] **Step 7: 비용 리포트에서 수락 이벤트를 뺀다**

`scripts/cost_report.py`의 집계는 `UsageEvent` 전체를 세어 "요청 N건"으로 찍는다. 업로드 수락은 토큰을 쓰지 않으므로 그 숫자에 들어가면 안 된다 — 넣으면 요청 대비 캐시 적중률이 조용히 낮아 보인다.

`collect()`의 첫 `select(...).where(...)`에 조건을 하나 더 건다.

```python
                ).where(
                    UsageEvent.created_at >= since,
                    # 업로드 수락은 비용이 0 이다. 세면 "요청 N건"과
                    # 캐시 적중률이 함께 왜곡된다.
                    UsageEvent.kind != "upload",
                )
```

Run: `.venv/bin/python -m pytest tests/test_cost_report.py -v`
Expected: 통과 (이 테스트는 집계 결과 dict 를 직접 만들어 검사하므로 쿼리를 타지 않는다 — 회귀만 확인한다)

- [ ] **Step 9: 테스트가 통과하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_unverified_gate.py -v`
Expected: 5 passed

- [ ] **Step 10: 게이트가 HTTP 로도 걸리는지 확인**

`tests/test_verification_api.py` 끝에 덧붙인다.

```python
async def test_the_sixth_question_is_refused_until_verified():
    """맛보기를 다 쓰면 429 가 아니라 403 이어야 한다 — 기다려도 안 풀린다."""
    from app.services import usage

    async with await _client() as client:
        email, body = await _signup(client)
        user_id = uuid.UUID(body["id"])

        async with SessionLocal() as session:
            for _ in range(settings.unverified_quota_queries):
                await usage.record(session, user_id, "query")

        response = await client.post(
            "/query", json={"question": "맛보기를 다 쓴 뒤의 질문"}
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "email verification required"


async def test_verifying_restores_the_normal_quota():
    from app.services import usage

    async with await _client() as client:
        email, body = await _signup(client)
        user_id = uuid.UUID(body["id"])

        async with SessionLocal() as session:
            for _ in range(settings.unverified_quota_queries):
                await usage.record(session, user_id, "query")

        token = await _token_for(email)
        await client.post("/auth/verify", json={"token": token})

        response = await client.post(
            "/query", json={"question": "인증한 뒤의 질문"}
        )

    # 403 만 아니면 된다. 임베딩 서버가 없으면 503 이 정상이고, 그건 게이트
    # 가 열렸다는 뜻이다.
    assert response.status_code != 403
```

또한 파일 상단 import 아래에 픽스처를 둔다 (`tests/test_storage_cleanup.py`와 같은 것 — 인덱싱은 실패하지만 업로드는 수락된다. 게이트가 색인 성공이 아니라 **수락**을 세는지 확인하는 데 정확히 맞는 픽스처다).

```python
from app.services.ratelimit import auth_limiter, upload_limiter, verify_resend_limiter

BROKEN_PDF = b"%PDF-1.4 not actually a pdf"
```

```python
async def test_the_second_upload_is_refused_and_deleting_does_not_reopen_it():
    """지우기로 한도가 초기화되면 무제한 임베딩이 공짜가 된다."""
    upload_limiter.reset()
    async with await _client() as client:
        await _signup(client)

        first = await client.post(
            "/documents",
            files={"file": ("one.pdf", BROKEN_PDF, "application/pdf")},
        )
        assert first.status_code == 202, first.text
        doc_id = first.json()["id"]

        second = await client.post(
            "/documents",
            files={"file": ("two.pdf", BROKEN_PDF, "application/pdf")},
        )
        assert second.status_code == 403
        assert second.json()["detail"] == "email verification required"

        assert (await client.delete(f"/documents/{doc_id}")).status_code == 204

        third = await client.post(
            "/documents",
            files={"file": ("three.pdf", BROKEN_PDF, "application/pdf")},
        )

    assert third.status_code == 403, "문서를 지우자 한도가 초기화됐다"
```

Run: `.venv/bin/python -m pytest tests/test_verification_api.py -v`
Expected: 11 passed

- [ ] **Step 11: 전체 회귀**

Run: `.venv/bin/python -m pytest tests/ -v --ignore=tests/test_error_details.py`
Expected: 기존 테스트 전부 통과. 기존 계정은 전부 verified 이므로 게이트에 걸리지 않는다.

- [ ] **Step 12: 커밋**

```bash
git add app/services/usage.py app/deps.py app/services/pipeline.py app/routers/documents.py scripts/cost_report.py tests/test_unverified_gate.py tests/test_verification_api.py
git commit -m "$(cat <<'MSG'
feat(quota): 미인증 계정은 맛보기 쿼터만

질의 5회·문서 1개까지 되고 그 뒤로 403. 한도보다 중요한 건 창이다 —
기존 쿼터처럼 "오늘"로 세면 미인증 계정이 매일 5회를 다시 받아서
막으려던 재가입 어뷰즈가 그대로 통과한다. 계정 수명 전체 누적으로 센다.

usage_events 를 그대로 쓴다. 그 테이블의 외래키가 documents 가 아니라
users 를 향해서, 문서를 지워도 ingest 행이 남는다 — 올렸다 지워서 한도를
되돌리는 우회가 구조적으로 막힌다.

429 가 아니라 403 인 이유는 기다린다고 풀리지 않기 때문이다. 429 면
프론트가 "잠시 뒤에 다시"라는 틀린 조언을 한다.
MSG
)"
```

---

### Task 6: `/usage` — 미인증일 때 다른 한도

**Files:**
- Modify: `app/schemas.py` (`UsageOut`)
- Modify: `app/routers/usage.py`
- Create: `tests/test_usage_unverified.py`

**Interfaces:**
- Consumes: Task 5의 `usage.queries_total` / `documents_total`
- Produces: `UsageOut`에 `email_verified`, `queries_total`, `documents_total`, `unverified_query_limit`, `unverified_document_limit`

> **설계 문서 수정:** 스펙은 "숫자만 바뀌므로 프론트 분기가 없다"고 적었는데 그건 틀렸다. 기존 바의 문구가 "오늘 한 질문 / 내일 다시 채워집니다"인데, 미인증 한도는 수명 전체 누적이라 **내일 채워지지 않는다.** 숫자만 바꾸면 화면이 거짓말을 한다. 그래서 응답에 `email_verified`를 실어 프론트가 문구까지 바꾸게 한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_usage_unverified.py`:

```python
"""사용량 화면이 미인증 계정에 거짓말하지 않는지. Postgres 가 필요하다."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.services import usage
from app.services.ratelimit import auth_limiter

pytestmark = pytest.mark.asyncio


async def test_unverified_usage_reports_the_taster_limits():
    auth_limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        signup = await client.post(
            "/auth/signup",
            json={
                "email": f"usage-{uuid.uuid4().hex}@example.com",
                "password": "password-123",
            },
        )
        assert signup.status_code == 201
        user_id = uuid.UUID(signup.json()["id"])

        async with SessionLocal() as session:
            await usage.record(session, user_id, "query")
            # 게이트가 세는 것은 수락된 업로드다 — 색인 성공(ingest)이 아니다.
            await usage.record(session, user_id, "upload")
            await usage.record(session, user_id, "ingest", pages=4)

        body = (await client.get("/usage")).json()

    assert body["email_verified"] is False
    assert body["queries_total"] == 1
    assert body["documents_total"] == 1
    assert body["unverified_query_limit"] == settings.unverified_quota_queries
    assert body["unverified_document_limit"] == settings.unverified_quota_documents
    # 기존 필드는 그대로 남는다 — 인증하고 나면 화면이 이쪽을 쓴다.
    assert body["queries_per_day"] == settings.quota_queries_per_day
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_usage_unverified.py -v`
Expected: FAIL — `KeyError: 'email_verified'`

- [ ] **Step 3: 스키마 확장**

`app/schemas.py`의 `UsageOut`을 바꾼다.

```python
class UsageOut(BaseModel):
    """Current consumption against the configured quotas.

    Both halves are returned so the caller can phrase the remainder itself
    ("82쪽 남았습니다") instead of receiving a percentage it cannot explain.

    미인증 계정은 다른 한도를 **다른 창**으로 센다(수명 전체 누적). 숫자만
    바꿔서는 안 되는 이유가 여기 있다 — 화면의 "내일 다시 채워집니다"가
    거짓이 된다. ``email_verified`` 를 함께 주어 문구까지 바꾸게 한다.
    """

    queries_today: int
    queries_per_day: int
    pages_this_month: int
    pages_per_month: int

    email_verified: bool
    queries_total: int
    documents_total: int
    unverified_query_limit: int
    unverified_document_limit: int
```

- [ ] **Step 4: 라우터 확장**

`app/routers/usage.py`의 `get_usage`를 바꾼다.

```python
@router.get("", response_model=UsageOut)
async def get_usage(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UsageOut:
    return UsageOut(
        queries_today=await usage_service.queries_today(session, user.id),
        queries_per_day=settings.quota_queries_per_day,
        pages_this_month=await usage_service.pages_this_month(session, user.id),
        pages_per_month=settings.quota_upload_pages_per_month,
        email_verified=user.email_verified,
        queries_total=await usage_service.queries_total(session, user.id),
        documents_total=await usage_service.documents_total(session, user.id),
        unverified_query_limit=settings.unverified_quota_queries,
        unverified_document_limit=settings.unverified_quota_documents,
    )
```

- [ ] **Step 5: 테스트가 통과하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_usage_unverified.py -v`
Expected: 1 passed

- [ ] **Step 6: 커밋**

```bash
git add app/schemas.py app/routers/usage.py tests/test_usage_unverified.py
git commit -m "$(cat <<'MSG'
feat(usage): 미인증 계정의 한도를 사용량 응답에 싣는다

설계 문서는 "숫자만 바뀌므로 프론트 분기가 없다"고 적었는데 틀렸다. 바의
문구가 "오늘 한 질문 / 내일 다시 채워집니다"인데 미인증 한도는 수명 전체
누적이라 내일 채워지지 않는다. 숫자만 바꾸면 화면이 거짓말을 한다.

email_verified 와 누적 카운트를 함께 내려보내 프론트가 문구까지 바꾸게
한다. 기존 네 필드는 그대로 두어 인증한 계정의 화면은 변하지 않는다.
MSG
)"
```

---

### Task 7: 프론트엔드 — 타입 · API · 에러 카피

**Files:**
- Modify: `web/lib/api/types.ts`
- Modify: `web/lib/api/auth.ts`
- Modify: `web/lib/api/errors.ts`
- Modify: `web/lib/api/errors.test.ts`

**Interfaces:**
- Consumes: Task 4·6의 응답 모양
- Produces:
  - `User.email_verified: boolean`, `Usage`의 새 필드 5개
  - `auth.verify(token)`, `auth.resendVerification()`
  - `BY_DETAIL`의 새 항목 4개

- [ ] **Step 1: 실패하는 테스트 작성**

`web/lib/api/errors.test.ts`의 `describe("describeError", ...)` 안에 추가한다.

```typescript
  it("인증이 필요한 403은 기다리라고 하지 않는다", () => {
    /* 쿼터와 달리 시간이 지나도 풀리지 않는다. 사용자가 메일의 링크를
     * 눌러야 한다 — "잠시 뒤에 다시"는 틀린 조언이다. */
    const copy = describeError(
      new ApiError(403, "email verification required"),
    );

    expect(copy.title).toBe("이메일 확인이 필요합니다");
    expect(copy.hint).not.toContain("잠시 뒤");
    expect(copy.hint).toContain("메일");
  });

  it("만료된 링크와 이미 쓴 링크를 다르게 안내한다", () => {
    /* 둘 다 "링크가 안 된다"지만 다음 행동이 다르다: 하나는 새 링크를
     * 받아야 하고, 다른 하나는 아무것도 할 필요가 없다. */
    const expired = describeError(new ApiError(400, "invalid or expired token"));
    const already = describeError(new ApiError(409, "email already verified"));

    expect(expired).not.toEqual(already);
    expect(expired.hint).toContain("새 링크");
  });

  it("재발송 제한은 스팸함을 함께 안내한다", () => {
    const copy = describeError(
      new ApiError(429, "verification email rate limit exceeded"),
    );

    expect(copy.title).toBe("메일을 방금 보냈습니다");
    expect(copy.hint).toContain("스팸함");
  });
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `cd web && npm test -- errors`
Expected: FAIL — 새 detail 들이 폴백("문제가 생겼습니다")으로 떨어진다

- [ ] **Step 3: 카피 추가**

`web/lib/api/errors.ts`의 `BY_DETAIL`에 `"invalid email or password"` 항목 뒤에 넣는다.

```typescript
  /* 이메일 인증(403). 쿼터의 429와 달리 기다려서 풀리지 않는다 —
   * 사용자가 메일의 링크를 눌러야 한다. */
  "email verification required": {
    title: "이메일 확인이 필요합니다",
    hint: "가입할 때 보낸 메일의 링크를 눌러 주세요. 안 왔다면 다시 보낼 수 있습니다.",
  },
  "invalid or expired token": {
    title: "링크가 만료됐습니다",
    hint: "24시간이 지나면 링크가 닫힙니다. 새 링크를 받아 주세요.",
  },
  /* 실패가 아니라 이미 끝났다는 뜻이다. 사용자가 할 일이 없다. */
  "email already verified": {
    title: "이미 확인된 이메일입니다",
    hint: "그대로 사용하시면 됩니다.",
  },
  "verification email rate limit exceeded": {
    title: "메일을 방금 보냈습니다",
    hint: "1분 뒤에 다시 요청해 주세요. 스팸함도 확인해 보세요.",
  },
```

- [ ] **Step 4: 타입 확장**

`web/lib/api/types.ts`:

```typescript
export type User = {
  id: string;
  email: string;
  /** 미인증이면 맛보기 쿼터만 쓸 수 있다. */
  email_verified: boolean;
  created_at: string | null;
};
```

같은 파일의 `Usage`:

```typescript
export type Usage = {
  queries_today: number;
  queries_per_day: number;
  pages_this_month: number;
  pages_per_month: number;
  /* 미인증 계정은 다른 한도를 다른 창(계정 수명 전체 누적)으로 센다.
   * 그래서 숫자만이 아니라 "내일 다시 채워집니다" 같은 문구도 달라진다. */
  email_verified: boolean;
  queries_total: number;
  documents_total: number;
  unverified_query_limit: number;
  unverified_document_limit: number;
};
```

- [ ] **Step 5: API 호출 추가**

`web/lib/api/auth.ts` 끝에 붙인다.

```typescript
/** 메일 링크의 토큰을 검증한다. 세션이 없어도 된다 — 다른 브라우저에서
 *  열리는 것이 정상 경로다. */
export function verify(token: string): Promise<User> {
  return request<User>("/auth/verify", {
    method: "POST",
    json: { token },
  });
}

/** 인증 메일 재발송. 현재 세션의 계정으로만 보낸다. */
export function resendVerification(): Promise<void> {
  return request<void>("/auth/resend-verification", { method: "POST" });
}
```

- [ ] **Step 6: 테스트가 통과하는지 확인**

Run: `cd web && npm test`
Expected: 전부 통과 (기존 테스트 포함 — `BACKEND_DETAILS`를 순회하는 검사가 새 4개도 함께 본다)

- [ ] **Step 7: `DETAIL_MAPPED` 갱신과 대조 확인**

`tests/test_error_details.py`의 `DETAIL_MAPPED` 집합에 네 줄을 추가한다. 프론트 카피(Step 3)와 **같은 태스크에서** 넣는 이유는 이 테스트가 양방향 대조라서다 — 한쪽만 있으면 어느 쪽을 먼저 넣어도 실패한다.

```python
    "email verification required",
    "invalid or expired token",
    "email already verified",
    "verification email rate limit exceeded",
```

Run: `.venv/bin/python -m pytest tests/test_error_details.py -v`
Expected: PASS — 백엔드의 detail 넷과 프론트 카피 넷이 맞물린다

- [ ] **Step 8: 타입 검사**

Run: `cd web && npx tsc --noEmit`
Expected: 에러 없음

- [ ] **Step 9: 커밋**

```bash
git add web/lib/api/types.ts web/lib/api/auth.ts web/lib/api/errors.ts web/lib/api/errors.test.ts tests/test_error_details.py
git commit -m "$(cat <<'MSG'
feat(web): 인증 API 타입과 에러 카피 4개

email verification required 를 429 가 아니라 403 으로 받는 이유가 카피에서
드러난다 — "잠시 뒤에 다시"가 아니라 "메일의 링크를 눌러 주세요"다.

만료된 링크와 이미 쓴 링크도 나눴다. 둘 다 "링크가 안 된다"지만 다음
행동이 다르다: 하나는 새 링크를 받아야 하고, 다른 하나는 할 일이 없다.
MSG
)"
```

---

### Task 8: 프론트엔드 — verify 페이지 · 배너 · 사용량 바

**Files:**
- Create: `web/app/verify/page.tsx`
- Create: `web/components/VerifyBanner.tsx`
- Modify: `web/lib/session.tsx`
- Modify: `web/components/AppShell.tsx`
- Modify: `web/app/(app)/settings/page.tsx`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 7의 `auth.verify`, `auth.resendVerification`, `User.email_verified`, `Usage`의 새 필드
- Produces: 없음 (마지막 태스크)

- [ ] **Step 1: 세션에 재조회 추가**

`web/lib/session.tsx`의 `SessionValue`에 `refresh`를 추가한다. 배너가 인증 후 스스로 사라지려면 `/auth/me`를 다시 물어야 한다.

```typescript
type SessionValue = {
  user: User | null;
  /** 아직 /auth/me 응답을 못 받은 상태. 이때 화면을 그리면 깜빡인다. */
  loading: boolean;
  setUser: (user: User | null) => void;
  /** 서버 상태를 다시 읽는다. 인증을 마친 뒤 배너를 내리는 데 쓴다. */
  refresh: () => Promise<void>;
  signOut: () => Promise<void>;
};
```

`SessionProvider` 안, `signOut` 위에 넣는다.

```typescript
  const refresh = useCallback(async () => {
    try {
      setUser(await auth.me());
    } catch {
      /* 401이면 로그인하지 않은 것. 그 외 실패도 여기서는 같게 다룬다. */
      setUser(null);
    }
  }, []);
```

`Provider`의 value에 `refresh`를 넣는다.

```typescript
    <SessionContext.Provider value={{ user, loading, setUser, refresh, signOut }}>
```

- [ ] **Step 2: 배너 작성**

`web/components/VerifyBanner.tsx`:

```typescript
"use client";

import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { auth, describeError } from "@/lib/api";
import { useSession } from "@/lib/session";

/* 발송은 응답 밖(BackgroundTasks)에서 일어나서 실패해도 사용자에게 도달할
 * 길이 없다. 그래서 이 배너의 "다시 보내기"가 유일한 복구 경로다 —
 * 미인증인 동안에는 어느 화면에서든 닿을 수 있어야 한다. */
export function VerifyBanner() {
  const { user } = useSession();
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  const [error, setError] = useState<string | null>(null);

  if (!user || user.email_verified) return null;

  async function onResend() {
    setState("sending");
    setError(null);
    try {
      await auth.resendVerification();
      setState("sent");
    } catch (cause) {
      const copy = describeError(cause);
      setError(`${copy.title} ${copy.hint}`);
      setState("idle");
    }
  }

  return (
    <div role="status">
      <span>
        {state === "sent"
          ? `${user.email} 로 링크를 다시 보냈습니다. 메일함을 확인해 주세요.`
          : "이메일을 확인해 주세요. 확인 전에는 질문과 업로드가 몇 번으로 제한됩니다."}
      </span>
      {state !== "sent" && (
        <Button onClick={() => void onResend()} disabled={state === "sending"}>
          {state === "sending" ? "보내는 중…" : "다시 보내기"}
        </Button>
      )}
      {error !== null && <span>{error}</span>}
    </div>
  );
}
```

- [ ] **Step 3: 셸에 배너 자리 만들기**

`web/components/AppShell.tsx`에 import를 추가한다.

```typescript
import { VerifyBanner } from "@/components/VerifyBanner";
```

본문 영역의 최상단 — `crumb`을 그리는 상단 바 **바로 아래**에 `<VerifyBanner />`를 넣는다. 사이드바가 아니라 본문 위에 두는 이유는 좁은 화면에서 사이드바가 덮개로 바뀌어 배너가 가려지기 때문이다.

- [ ] **Step 4: 착지 페이지 작성**

`web/app/verify/page.tsx`:

```typescript
"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { auth, describeError, type ErrorCopy } from "@/lib/api";
import { useSession } from "@/lib/session";

/* (app) 그룹 밖에 있다. 메일 링크는 로그인하지 않은 다른 브라우저에서
 * 열리는 것이 정상 경로인데, 그룹 안이면 로그인 리다이렉트에 걸려 주소의
 * 토큰이 유실된다. */

type State =
  | { kind: "checking" }
  | { kind: "done" }
  | { kind: "failed"; copy: ErrorCopy };

function VerifyInner() {
  const token = useSearchParams().get("token");
  const { refresh } = useSession();
  const [state, setState] = useState<State>({ kind: "checking" });
  /* React 18의 개발용 이중 실행에서 토큰을 두 번 쓰면, 두 번째가
   * "이미 인증됨"으로 실패해 성공 화면이 실패 화면으로 뒤집힌다. */
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;

    if (token === null || token === "") {
      setState({
        kind: "failed",
        copy: {
          title: "링크가 올바르지 않습니다",
          hint: "메일의 링크를 그대로 열어 주세요. 주소가 잘렸을 수 있습니다.",
        },
      });
      return;
    }

    auth
      .verify(token)
      .then(async () => {
        await refresh();
        setState({ kind: "done" });
      })
      .catch((cause) => {
        setState({ kind: "failed", copy: describeError(cause) });
      });
  }, [token, refresh]);

  if (state.kind === "checking") return <p>확인하고 있습니다…</p>;

  if (state.kind === "done") {
    return (
      <div>
        <h1>이메일 확인이 끝났습니다</h1>
        <p>이제 한도 없이 질문하고 문서를 올릴 수 있습니다.</p>
        <Link href="/">돌아가기</Link>
      </div>
    );
  }

  return (
    <div>
      <h1>{state.copy.title}</h1>
      <p>{state.copy.hint}</p>
      <ResendOrSignIn />
    </div>
  );
}

/** 재발송은 세션이 있어야 한다. 없으면 로그인부터 안내한다. */
function ResendOrSignIn() {
  const { user, loading } = useSession();
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (loading) return null;
  if (user === null) return <Link href="/login">로그인하고 다시 받기</Link>;
  if (user.email_verified) return <Link href="/">돌아가기</Link>;
  if (sent) return <p>링크를 다시 보냈습니다. 메일함을 확인해 주세요.</p>;

  async function onResend() {
    setError(null);
    try {
      await auth.resendVerification();
      setSent(true);
    } catch (cause) {
      const copy = describeError(cause);
      setError(`${copy.title} ${copy.hint}`);
    }
  }

  return (
    <div>
      <Button onClick={() => void onResend()}>새 링크 받기</Button>
      {error !== null && <p>{error}</p>}
    </div>
  );
}

export default function VerifyPage() {
  /* useSearchParams 는 Suspense 경계를 요구한다. */
  return (
    <Suspense fallback={<p>확인하고 있습니다…</p>}>
      <VerifyInner />
    </Suspense>
  );
}
```

- [ ] **Step 5: 사용량 바를 정직하게**

`web/app/(app)/settings/page.tsx`의 두 `<UsageBar />`를 감싼 블록을 바꾼다.

```typescript
          {usage !== null && (
            <>
              {usage.email_verified ? (
                <>
                  <UsageBar
                    label="오늘 한 질문"
                    used={usage.queries_today}
                    limit={usage.queries_per_day}
                    unit="개"
                    refill="내일 다시 채워집니다."
                  />
                  <UsageBar
                    label="이번 달 올린 쪽수"
                    used={usage.pages_this_month}
                    limit={usage.pages_per_month}
                    unit="쪽"
                    refill="다음 달 1일에 다시 채워집니다."
                  />
                </>
              ) : (
                /* 미인증 한도는 수명 전체 누적이라 다시 채워지지 않는다.
                 * 인증한 계정의 문구("내일 다시 채워집니다")를 그대로 쓰면
                 * 화면이 거짓말을 한다. */
                <>
                  <UsageBar
                    label="지금까지 한 질문"
                    used={usage.queries_total}
                    limit={usage.unverified_query_limit}
                    unit="개"
                    refill="이메일을 확인하면 하루 200개로 늘어납니다."
                  />
                  <UsageBar
                    label="지금까지 올린 문서"
                    used={usage.documents_total}
                    limit={usage.unverified_document_limit}
                    unit="개"
                    refill="이메일을 확인하면 한 달 1000쪽으로 늘어납니다."
                  />
                </>
              )}
            </>
          )}
```

- [ ] **Step 6: 타입 검사와 린트**

Run: `cd web && npx tsc --noEmit && npm run lint`
Expected: 에러 없음

- [ ] **Step 7: 프론트 테스트**

Run: `cd web && npm test`
Expected: 전부 통과

- [ ] **Step 8: 손으로 한 번 통과시킨다**

터미널 셋:

```bash
# 1) 인프라와 백엔드
docker compose up -d db
.venv/bin/python -m uvicorn app.main:app --reload

# 2) 프론트 (Turbopack 은 이 맥에서 포트를 안 잡는다)
cd web && npm run dev:webpack
```

확인 순서:
1. `http://localhost:3000/login`에서 새 계정으로 가입 → 앱에 들어가지고 상단에 배너가 보인다
2. 백엔드 로그에서 `[mail]` 줄의 링크를 복사한다 (`MAIL_PROVIDER=console`)
3. **로그인하지 않은 다른 브라우저(시크릿 창)**에 그 링크를 붙여 넣는다 → "이메일 확인이 끝났습니다"
4. 원래 창을 새로고침 → 배너가 사라진다
5. 같은 링크를 다시 연다 → "이미 확인된 이메일입니다"
6. 설정 화면의 사용량 바가 "오늘 한 질문 / 내일 다시 채워집니다"로 돌아와 있다

- [ ] **Step 9: README 갱신**

`README.md`의 환경변수 표(또는 그에 준하는 절)에 `MAIL_PROVIDER` · `RESEND_API_KEY` · `MAIL_FROM` · `APP_BASE_URL` · `VERIFY_TOKEN_TTL_HOURS` · `UNVERIFIED_QUOTA_QUERIES` · `UNVERIFIED_QUOTA_DOCUMENTS` · `RATE_LIMIT_VERIFY_RESEND_PER_MIN` 여덟 줄을 추가하고, 다음 문단을 넣는다.

```markdown
### 이메일 인증

가입하면 확인 메일이 나간다. 확인 전에는 질문 5회·문서 1개까지만 쓸 수
있고 그 뒤로는 403이 나온다 — 이 한도는 하루가 아니라 **계정 수명 전체
누적**이다. "하루 5회"로 두면 미인증 계정이 매일 5회씩 다시 받아서, 막으려던
재가입 어뷰즈가 그대로 통과하기 때문이다.

`MAIL_PROVIDER=console`(기본)이면 메일 대신 링크가 서버 로그에 `[mail]`
줄로 찍힌다. 실제 발송은 `MAIL_PROVIDER=resend` + `RESEND_API_KEY`.

**도메인이 없으면 본인 주소로만 전달된다.** Resend 는 도메인을 검증하기
전까지 `onboarding@resend.dev` 발신만 허용하고, 그 경우 수신도 계정
소유자에게만 간다. 남이 가입해서 인증까지 마치게 하려면 도메인을 붙이고
SPF/DKIM 을 설정한 뒤 `MAIL_FROM` 을 바꾼다. 코드 변경은 필요 없다.
```

- [ ] **Step 10: 전체 테스트**

Run: `.venv/bin/python -m pytest tests/ -v && cd web && npm test`
Expected: 양쪽 전부 통과

- [ ] **Step 12: 커밋**

```bash
git add web/app/verify/page.tsx web/components/VerifyBanner.tsx web/lib/session.tsx web/components/AppShell.tsx "web/app/(app)/settings/page.tsx" README.md
git commit -m "$(cat <<'MSG'
feat(web): 인증 착지 페이지와 배너

/verify 는 (app) 라우트 그룹 밖에 둔다. 메일 링크는 로그인하지 않은 다른
브라우저에서 열리는 것이 정상 경로인데, 그룹 안이면 로그인 리다이렉트에
걸려 주소의 토큰이 유실된다.

검증은 ref 로 한 번만 실행한다. 개발 모드의 이중 실행에서 두 번째가
"이미 인증됨"으로 실패해 성공 화면이 실패 화면으로 뒤집힌다.

설정 화면의 사용량 바는 미인증일 때 문구까지 바꾼다. 미인증 한도는 수명
전체 누적이라 "내일 다시 채워집니다"가 거짓이다.
MSG
)"
```

---

## 완료 기준

1. 새 계정으로 가입하면 `email_verified=false`이고 메일(또는 콘솔 로그)에 링크가 나간다
2. 로그인하지 않은 브라우저에서 링크를 열면 인증이 끝난다
3. 같은 링크를 다시 열면 "이미 확인된 이메일입니다"(409)
4. 만료·위조 토큰은 400이고, 둘을 구분할 수 없다
5. 미인증 계정이 질의 5회를 쓰면 6번째는 403 `email verification required`
6. 미인증 계정이 문서 1개를 올리면 두 번째는 403이고, **첫 문서를 지워도 여전히 403**
   (색인 성공 여부와 무관하다 — 게이트는 수락된 업로드를 센다)
7. 인증하면 정상 쿼터(200/일, 1000쪽/월)로 돌아온다
8. 마이그레이션 이전부터 있던 계정(시드 유저 포함)은 전부 인증 상태
9. `RESEND_API_KEY` 없이 `pytest`와 `npm test`가 전부 통과한다
10. `.venv/bin/python -m alembic downgrade 0007 && upgrade head`가 에러 없이 돈다
