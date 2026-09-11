"""Usage recording and database-backed quotas.

Quotas are aggregated from ``usage_events`` rather than kept in memory, so they
survive restarts and stay correct behind multiple workers — the opposite
trade-off from the in-process rate limiter, and the reason this is the layer
that actually bounds spend.

Windows are wall-clock calendar windows in UTC (today / this month), which is
what the spec's "일/월" limits mean.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import UsageEvent


def _start_of_day() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_month() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def queries_today(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count(UsageEvent.id)).where(
            UsageEvent.user_id == user_id,
            UsageEvent.kind == "query",
            UsageEvent.created_at >= _start_of_day(),
        )
    )
    return int(result.scalar_one())


async def pages_this_month(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.coalesce(func.sum(UsageEvent.pages), 0)).where(
            UsageEvent.user_id == user_id,
            UsageEvent.kind == "ingest",
            UsageEvent.created_at >= _start_of_month(),
        )
    )
    return int(result.scalar_one())


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


async def pages_uploaded_total(session: AsyncSession, user_id: uuid.UUID) -> int:
    """계정 수명 전체에 걸쳐 **수락된** 업로드의 누적 쪽수.

    ``documents_total`` 과 같은 이유로 ``ingest`` 가 아니라 ``upload`` 를
    센다 — ``ingest`` 는 색인이 성공한 뒤에야 생겨서, 색인이 끝나기 전에
    연달아 던진 업로드는 전부 0쪽으로 보이고 실패한 업로드는 아예 빠진다.
    """
    result = await session.execute(
        select(func.coalesce(func.sum(UsageEvent.pages), 0)).where(
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


async def unverified_pages_exceeded(
    session: AsyncSession, user_id: uuid.UUID, incoming_pages: int = 0
) -> bool:
    """미인증 계정의 맛보기 쪽수 한도를 이 업로드가 넘기는지.

    ``unverified_upload_exceeded`` 와 달리 이력만으로는 판단할 수 없다 —
    첫 업로드 자체가 500쪽이면 이력이 0이어도 그 한 번으로 한도를 넘겨야
    한다. ``upload_quota_exceeded`` 와 같은 모양(이력 + 들어오는 쪽수)인
    이유가 그것이다.
    """
    if settings.unverified_quota_pages <= 0:
        return False
    used = await pages_uploaded_total(session, user_id)
    return used + incoming_pages > settings.unverified_quota_pages


async def query_quota_exceeded(session: AsyncSession, user_id: uuid.UUID) -> bool:
    if settings.quota_queries_per_day <= 0:
        return False
    return await queries_today(session, user_id) >= settings.quota_queries_per_day


async def upload_quota_exceeded(
    session: AsyncSession, user_id: uuid.UUID, incoming_pages: int = 0
) -> bool:
    """True when this upload would push the month past the page allowance."""
    if settings.quota_upload_pages_per_month <= 0:
        return False
    used = await pages_this_month(session, user_id)
    return used + incoming_pages > settings.quota_upload_pages_per_month


async def record(
    session: AsyncSession,
    user_id: uuid.UUID,
    kind: str,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    pages: int = 0,
    cached: bool = False,
) -> None:
    session.add(
        UsageEvent(
            user_id=user_id,
            kind=kind,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            pages=pages,
            cached=cached,
        )
    )
    await session.commit()


# --- check-then-act 를 원자로 만들기 위한 예약 패턴 --------------------
#
# 위의 *_exceeded 함수들은 순수하게 "지금 넘었나"만 answer 한다 — 호출자가
# 그 answer 를 받아 실제로 작업을 하는 사이 다른 요청이 같은 answer 를
# 받아버리는 것이 이 파일이 아니라 pipeline.py/documents.py 가 겪던 경쟁
# 조건이었다. 그 간격을 없애려면 "세다"와 "쓴다" 사이에 아무도 끼어들 수
# 없어야 하는데, 그러자고 체크부터 기록까지 통째로 잠그면(트랜잭션 하나가
# 임베딩·리랭크·LLM 호출까지 물고 있게 되어) 그 시간 내내 커넥션 하나를
# 붙잡고 이 사용자의 다음 요청까지 전부 세워버린다 — 스펙이 금지하는
# 바로 그 트레이드오프다.
#
# 그래서 "체크 + 예약"만 아래 세 함수로 원자화한다: acquire_quota_lock 으로
# 짧게 잠그고, *_exceeded 로 다시 세고, reserve 로 행을 하나 심고, 곧장
# 커밋한다 — 이 전체가 로컬 쿼리 몇 번뿐이라 눈 깜짝할 새 끝난다. 실제
# 작업(임베딩·리트리벌·리랭크·생성)은 잠금이 없는 채로 그 뒤에 일어나고,
# 성공하면 commit_reservation 이, 실패하면(예외 포함) release_reservation
# 이 그 예약을 마무리한다 — 그래야 일어나지 않은 작업에 요금이 매겨지지
# 않는다.


async def acquire_quota_lock(
    session: AsyncSession, user_id: uuid.UUID, kind: str
) -> None:
    """이 사용자의 이 kind 에 대한 "읽고 나서 쓴다"를 원자로 만든다.

    잠금이 없으면 동시에 들어온 요청들이 모두 같은(아직 반영 전) 집계를
    읽고 모두 통과해버린다 — 분당 버킷(services/ratelimit.py)은 요청이
    얼마나 빨리 들어오는지만 막지 이 문제는 막지 못한다. 그래서 집계를
    다시 읽기 직전에 이 잠금으로 같은 (kind, user_id) 의 다른 요청을
    뒤로 세운다.

    ``pg_advisory_xact_lock`` 은 트랜잭션 범위라 짝이 되는 "해제" 호출이
    없다 — 호출자가 다음으로 커밋하거나 롤백하는 순간 저절로 풀린다.
    그래서 호출자는 이 호출 뒤로 임베딩·LLM 호출처럼 느린 await 없이
    곧장 커밋까지 가야 한다: 그러지 않으면 이 사용자의 다른 요청 전부가
    그 시간만큼 붙잡히고, 그건 스펙이 명시적으로 금지하는 바로 그
    상황이다.

    (kind, user_id) 로 묶어 종류가 다르면(query vs upload) 서로 막지
    않게 한다. ``hashtext`` 가 이 쌍을 정수 하나로 접는데, 해시가
    충돌해도 안전하다 — 무관한 두 요청이 잠깐 더 기다릴 뿐, 잠금을 쥔
    다음 실제로 무엇을 볼지는 뒤이어 다시 읽는 집계 쿼리가 결정한다.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))").bindparams(
            key=f"quota:{kind}:{user_id}"
        )
    )


async def reserve(
    session: AsyncSession, user_id: uuid.UUID, kind: str, *, pages: int = 0
) -> uuid.UUID:
    """실제 작업을 시작하기 전에 그 자리를 먼저 차지해 두는 행을 심는다.

    이 행 자체가 예약이다 — 따로 맞춰야 할 카운터가 없다. 작업이
    끝나면 ``commit_reservation`` 이 진짜 값(토큰 수 등)을 채우고,
    끝내 끝나지 못하면 ``release_reservation`` 이 이 행을 지운다.
    커밋은 여기서 하지 않고 호출자에게 맡긴다 — 업로드처럼 같은
    트랜잭션에 다른 행(``Document``)을 함께 넣어야 하는 호출자가
    있어서, 언제 커밋할지는 이 함수가 결정할 일이 아니다.
    """
    # id is generated here rather than left to the column default: that
    # default is only applied at flush time, so reading `event.id` right
    # after construction (before this function's caller has any reason to
    # flush) would still be None — and every caller of `reserve` needs the
    # real id back immediately, before it ever flushes anything.
    event_id = uuid.uuid4()
    event = UsageEvent(id=event_id, user_id=user_id, kind=kind, pages=pages)
    session.add(event)
    return event_id


async def commit_reservation(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cached: bool = False,
) -> None:
    """예약이 서 있던 작업이 실제로 끝났을 때 진짜 값을 채우고 커밋한다."""
    await session.execute(
        update(UsageEvent)
        .where(UsageEvent.id == event_id)
        .values(tokens_in=tokens_in, tokens_out=tokens_out, cached=cached)
    )
    await session.commit()


async def release_reservation(session: AsyncSession, event_id: uuid.UUID) -> None:
    """예약이 서 있던 작업이 끝내 끝나지 못했을 때 그 행을 지운다.

    먼저 롤백부터 한다 — 여기로 오게 만든 실패가 세션의 트랜잭션을 이미
    못 쓰게 만들어 놓았을 수 있어서다(``_discard_unanswered`` 가 같은
    이유로 먼저 롤백하는 것과 같은 사정이다). 아무것도 바꾸지 않은
    세션에서는 그냥 안전한 공짜 호출이다.
    """
    await session.rollback()
    await session.execute(delete(UsageEvent).where(UsageEvent.id == event_id))
    await session.commit()
