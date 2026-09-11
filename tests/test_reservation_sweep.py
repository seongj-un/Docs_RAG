"""죽은 프로세스가 남긴 쿼터 예약이 계정을 영구히 잠그지 않는지. Postgres 가 필요하다.

f81716e 는 체크-후-기록을 어드바이저리 락 + 예약 행으로 원자화했다. 그런데
예약을 심은 프로세스가 commit_reservation/release_reservation 에 이르기
전에 죽으면(SIGKILL, OOM, 배포 재시작) 그 행은 영원히 미정산으로 남고,
정산된 행과 바이트 단위로 구분이 안 됐다 — 이 파일은 그 구분(settled_at)과
그것을 이용한 자가치유(acquire_quota_lock 안의 sweep)가 실제로 도는지,
그리고 sweep 이 지우면 안 되는 것까지 지우지는 않는지를 증명한다.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, engine
from app.services import auth, usage


def run_async(coro_fn):
    """Run one coroutine, always disposing the engine afterwards."""

    async def wrapper():
        try:
            return await coro_fn()
        finally:
            await engine.dispose()

    return asyncio.run(wrapper())


def _db_available() -> bool:
    async def check():
        try:
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.run(check())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


async def _settled_at(session, event_id: uuid.UUID):
    return (
        await session.execute(
            text("SELECT settled_at FROM usage_events WHERE id = :id").bindparams(
                id=event_id
            )
        )
    ).scalar_one()


async def _age_row(session, event_id: uuid.UUID, age_seconds: float) -> None:
    """이 행의 created_at 을 과거로 옮겨 '한참 전에 심어졌다'를 흉내낸다.

    UUID 는 객체 그대로 바인딩한다 — str(...) 로 바꾸면 타입이 안 맞는다.
    """
    old = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    await session.execute(
        text("UPDATE usage_events SET created_at = :old WHERE id = :id").bindparams(
            old=old, id=event_id
        )
    )
    await session.commit()


def test_orphaned_reservation_past_the_ttl_does_not_lock_the_account_forever(
    monkeypatch,
):
    """죽은 프로세스가 남긴 예약이, 다음 질의까지 영원히 막지는 않는다.

    미인증 계정의 맛보기 한도는 창이 없는 계정 수명 전체 누적이다 — 인증된
    계정의 일일 한도와 달리 "하루가 지나면 저절로 빠진다"가 없다. 그래서
    이 시나리오에서 orphan 하나가 곧 한도 전부다: reserve 를 커밋해 두고
    (enforce_limits 가 실제로 하는 일) commit_reservation/release_reservation
    없이 여기서 멈춘다 — 프로세스가 그 사이에 죽었다는 뜻이다. 그 행을
    타임아웃보다 오래 묵힌 뒤, 이 계정이 "다음에 쓰일 때"(새 질의 하나)가
    실제로 통과하는지를 enforce_limits 로 직접 확인한다 — 불리언 하나가
    아니라 진짜 새 예약이 커밋되는지까지.
    """
    monkeypatch.setattr(settings, "unverified_quota_queries", 1)

    from app.models import User as UserModel
    from app.services.pipeline import QueryRunner

    async def scenario():
        async with SessionLocal() as setup:
            user = await auth.create_user(
                setup, f"orphan-{uuid.uuid4().hex}@example.com", "password-123"
            )
            user_id = user.id

        async with SessionLocal() as session:
            event_id = await usage.reserve(session, user_id, "query")
            await session.commit()  # enforce_limits 도 여기서 커밋하고 손을 뗀다

            settled = await _settled_at(session, event_id)
            assert settled is None, "예약이 태어날 때부터 정산된 채였다"

            # 여기서 프로세스가 죽었다고 친다 — commit_reservation 도
            # release_reservation 도 다시는 불리지 않는다. 타임아웃보다
            # 확실히 오래 묵힌다.
            await _age_row(session, event_id, settings.reservation_ttl_seconds + 5)

        # 계정이 "다음에 쓰일 때": 새 질의 하나가 들어온다. QueryRunner 는
        # user.id/email_verified 만 읽으므로 detached 스탠드인으로 충분하다
        # (tests/test_phase2.py 의 경쟁 테스트와 같은 요령).
        stand_in = UserModel(
            id=user_id, email="x", password_hash="x", email_verified_at=None
        )
        async with SessionLocal() as session:
            runner = QueryRunner(
                session, stand_in, "안녕하세요", document_id=None, hybrid=False
            )
            raised = None
            try:
                await runner.enforce_limits("10.0.0.1")
            except Exception as exc:  # noqa: BLE001 - 실패 자체가 검사 대상
                raised = exc

        async with SessionLocal() as check:
            remaining = await usage.queries_total(check, user_id)

        return raised, remaining

    raised, remaining = run_async(scenario)

    assert raised is None, f"고아 예약 때문에 새 질의가 여전히 막혔다: {raised!r}"
    # 고아 1개가 스윕되고 이번 질의의 새 예약 1개만 남아야 한다 — 둘 다
    # 남으면 스윕이 안 된 것이고, 0개면 이번 질의도 예약을 못 심은 것이다.
    assert remaining == 1, f"{remaining}개가 남았다 — 스윕이나 새 예약 중 하나가 잘못됐다"


def test_a_fresh_reservation_still_blocks_the_next_request(monkeypatch):
    """스윕은 나이 든 예약만 지워야 한다 — 방금 심은 것까지 걷어가면 안 된다.

    이 테스트가 지키는 것은 상호배제(mutual exclusion)가 아니라
    ``reservation_ttl_seconds`` 값 그 자체다. 진짜 동시 요청이 하나만
    이기는 것은 tests/test_phase2.py 와 tests/test_verification_api.py 의
    경쟁 테스트가 스윕 도입 전부터 이미 증명해 두었고, 그 테스트들을 그대로
    다시 돌리는 것만으로는 스윕이 그 보장을 깨는지 확인되지 않는다 — 두
    요청이 어드바이저리 락을 주고받는 간격은 수 밀리초뿐이라, 타임아웃을
    얼마나 잘못 짧게 잡든 그 정도 간격에서는 어차피 안 지워져 우연히
    통과해버린다(운을 검증하는 테스트가 된다는, test_verification.py 의
    경쟁 테스트들이 경고하는 바로 그 함정).

    그래서 여기서는 시간 축을 직접 다룬다: 예약을 하나 심어 나이 0인 채로
    두고, 그 즉시 뒤이은 요청이 같은 잠금 경로(acquire_quota_lock)를 타게
    한다. ``reservation_ttl_seconds`` 를 0에 가깝게 잘못 줄이면 그 예약이
    바로 이 순간 스윕 대상이 되어 이 테스트가 죽는다 — 타임아웃을 너무
    공격적으로 잡았을 때 걸리는 것이 정확히 이 테스트다.
    """
    monkeypatch.setattr(settings, "quota_queries_per_day", 1)

    from fastapi import HTTPException
    from app.models import User as UserModel
    from app.services.pipeline import QueryRunner

    async def scenario():
        async with SessionLocal() as setup:
            user = await auth.create_user(
                setup, f"fresh-{uuid.uuid4().hex}@example.com", "password-123"
            )
            user.email_verified_at = datetime.now(timezone.utc)
            await setup.commit()
            user_id = user.id

        # "이미 처리 중인 요청" 하나가 방금 자기 슬롯을 예약해 두었다 —
        # 아직 embed/rerank/LLM 이 안 끝나 정산 전(NULL)인, 나이 0인 채로.
        async with SessionLocal() as session:
            event_id = await usage.reserve(session, user_id, "query")
            await session.commit()
            settled = await _settled_at(session, event_id)
            assert settled is None, "예약이 태어날 때부터 정산된 채였다"

        verified_now = datetime.now(timezone.utc)
        stand_in = UserModel(
            id=user_id, email="y", password_hash="x", email_verified_at=verified_now
        )
        async with SessionLocal() as session:
            runner = QueryRunner(
                session, stand_in, "q", document_id=None, hybrid=False
            )
            raised = None
            try:
                await runner.enforce_limits("10.0.0.2")
            except Exception as exc:  # noqa: BLE001 - 실패 자체가 검사 대상
                raised = exc

        async with SessionLocal() as check:
            total = await usage.queries_total(check, user_id)

        return raised, total

    raised, total = run_async(scenario)

    assert isinstance(raised, HTTPException), (
        f"방금 심은(나이 0인) 예약이 스윕에 지워져 두 번째 요청까지 통과했다: {raised!r}"
    )
    assert raised.status_code == 429
    # 먼저 있던 예약 1개뿐이어야 한다 — 2개면 스윕이 그것을 지우고 두 번째
    # 요청이 새로 심은 것까지 둘 다 남은 것이라 일일 쿼터(1)가 뚫린 것이다.
    assert total == 1, f"{total}개가 남았다 — 일일 쿼터(1)가 뚫렸다"


def test_record_created_events_are_never_swept_at_any_age():
    """record() 로 남긴 행(ingest 성공 기록 등)은 몇 년이 지나도 스윕 대상이
    아니다 — settled_at 이 그 자리에서 곧바로 채워지기 때문이다.

    이게 깨지면 스윕이 예약이 아니라 실제로 있었던 사용 이력을 지우는
    것이라 월간 쪽수 쿼터가 조용히 틀어진다 — 고아 예약을 청소하려다
    진짜 있었던 사용량을 청소해버리는, 이 기능이 절대 하면 안 되는 실수다.
    """

    async def scenario():
        async with SessionLocal() as setup:
            user = await auth.create_user(
                setup, f"record-{uuid.uuid4().hex}@example.com", "password-123"
            )
            user_id = user.id

        async with SessionLocal() as session:
            await usage.record(session, user_id, "ingest", pages=40)

            row = (
                await session.execute(
                    text(
                        "SELECT id, settled_at FROM usage_events "
                        "WHERE user_id = :uid AND kind = 'ingest'"
                    ).bindparams(uid=user_id)
                )
            ).first()
            assert row is not None
            event_id, settled = row
            assert settled is not None, "record() 가 정산 시각을 채우지 않았다"

            # sweep 이 다루는 시간대(분 단위)와 아예 다른 자릿수로 늙힌다 —
            # reservation_ttl_seconds 의 몇 배가 아니라 10년 전으로.
            await _age_row(session, event_id, 3650 * 24 * 3600)

        async with SessionLocal() as session:
            await usage.acquire_quota_lock(session, user_id, "ingest")
            await session.commit()  # sweep 이 뭔가 지웠다면 여기서 반영된다

        async with SessionLocal() as check:
            pages = await usage.pages_this_month(check, user_id)
            remaining = (
                await check.execute(
                    text("SELECT COUNT(*) FROM usage_events WHERE id = :id").bindparams(
                        id=event_id
                    )
                )
            ).scalar_one()

        return pages, remaining

    pages, remaining = run_async(scenario)

    assert remaining == 1, "record() 로 남긴 실제 사용 이력을 sweep 이 지웠다"
    # 10년 전으로 늙혔으니 이번 달 집계에는 안 잡히는 게 정상이다 — 이
    # 단언은 "행 자체가 사라지지 않았다"를 재확인하는 것이 아니라, sweep
    # 이후에도 집계 함수가 여전히 정상 동작한다는 것을 곁들여 확인한다.
    assert pages == 0


def test_upload_reservations_are_born_settled_and_are_never_swept():
    """documents.py 의 upload 경로는 reserve(..., settled=True) 를 쓴다 —
    쪽수가 잠그기 전에 이미 파싱으로 알려져 있어 나중에 commit_reservation
    으로 채울 값이 없기 때문이다. 이 인자가 빠지면(여기서 그 상황을 직접
    재현한다) 그 행은 영원히 정산 전으로 남아, 몇 분만 지나도 sweep 이
    방금 수락된 업로드를 고아 예약으로 오인해 지운다 — 미인증 계정의
    평생 문서 한도(unverified_quota_documents)가 조용히 되돌아가 버린다.
    """

    async def scenario():
        async with SessionLocal() as setup:
            user = await auth.create_user(
                setup, f"upload-settled-{uuid.uuid4().hex}@example.com", "password-123"
            )
            user_id = user.id

        async with SessionLocal() as session:
            event_id = await usage.reserve(
                session, user_id, "upload", pages=12, settled=True
            )
            await session.commit()

            settled = await _settled_at(session, event_id)
            assert settled is not None, "settled=True 인데도 정산 시각이 비어 있다"

            await _age_row(session, event_id, 3650 * 24 * 3600)

        async with SessionLocal() as session:
            await usage.acquire_quota_lock(session, user_id, "upload")
            await session.commit()

        async with SessionLocal() as check:
            documents = await usage.documents_total(check, user_id)
            pages = await usage.pages_uploaded_total(check, user_id)

        return documents, pages

    documents, pages = run_async(scenario)

    assert documents == 1, "settled=True 로 남긴 업로드가 스윕에 지워졌다"
    assert pages == 12, "업로드 쪽수 이력이 스윕에 지워졌다"
