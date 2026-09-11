"""usage_events 에 정산 시각을 추가한다

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-11

f81716e 는 쿼터 체크-후-기록을 원자로 만들려고 usage_events 에 "예약" 행을
먼저 심고(reserve) 나중에 진짜 값을 채우거나(commit_reservation) 지운다
(release_reservation). 그런데 예약과 완결된 행이 둘 다 tokens_in=0,
tokens_out=0, cached=False 라 바이트 단위로 구분이 안 됐다 — 예약을 심은
프로세스가 commit_reservation/release_reservation 에 이르기 전에 죽으면
(SIGKILL, OOM, 배포 재시작) 그 행은 영원히 미인증 계정의 평생 한도를
갉아먹으면서도 아무도(사용자도 운영자도) 되돌릴 방법이 없었다.

이 컬럼이 그 표식이다. NULL 이면 "아직 정산 전"(예약 중), 값이 있으면
"이 행의 값은 최종"이라는 뜻이다 — email_verification_tokens.consumed_at 이
그 토큰의 소비 여부를 표시하는 것과 같은 모양이다. usage.py 의 sweep 은
NULL 이면서 reservation_ttl_seconds 보다 오래된 행만 고아로 간주해 지운다.

기존 행은 전부 이미 완결된 사건이다 — 이 마이그레이션 이전에는 예약이라는
개념 자체가 없었고, f81716e 이후에 심어진 예약도 정상 경로라면 곧바로
commit_reservation/release_reservation 으로 끝난다. 그래서 전부 정산
완료로 채운다: 안 채우면 sweep 이 이 행들을 전부 "오래된 고아 예약"으로
오인해 지운다 — 사라지는 것은 예약이 아니라 실제로 있었던 사용량이다.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "usage_events",
        sa.Column("settled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    # 기존 행 grandfather. 이 시점 이후의 새 예약만 NULL 로 들어와 sweep 의
    # 대상이 될 수 있다.
    op.execute("UPDATE usage_events SET settled_at = created_at")


def downgrade() -> None:
    op.drop_column("usage_events", "settled_at")
