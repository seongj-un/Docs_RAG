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
