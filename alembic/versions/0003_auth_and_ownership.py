"""M3: users, sessions, and document ownership

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-07

Adds authentication tables and converts ``documents.user_id`` from the M1
placeholder (TEXT, default 'local') to a real UUID FK into ``users``.

Pre-M3 rows are preserved, not deleted: a fixed seed user is created and every
existing document is reassigned to it. The seed account cannot be logged into —
its password hash is a sentinel that can never verify — so preserving the rows
does not create a usable account.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.constants import SEED_USER_EMAIL, SEED_USER_ID, UNUSABLE_PASSWORD_HASH

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("sessions_user_id_idx", "sessions", ["user_id"])

    # Seed owner for pre-M3 documents.
    op.execute(
        sa.text(
            "INSERT INTO users (id, email, password_hash) "
            "VALUES (CAST(:id AS uuid), :email, :hash)"
        ).bindparams(
            id=str(SEED_USER_ID), email=SEED_USER_EMAIL, hash=UNUSABLE_PASSWORD_HASH
        )
    )

    # documents.user_id: TEXT 'local' -> UUID FK. Done as add/backfill/swap so
    # the backfill is explicit and every existing row keeps its data.
    op.add_column("documents", sa.Column("owner_id", postgresql.UUID(as_uuid=True)))
    op.execute(
        sa.text("UPDATE documents SET owner_id = CAST(:id AS uuid)").bindparams(
            id=str(SEED_USER_ID)
        )
    )
    op.alter_column("documents", "owner_id", nullable=False)
    op.drop_column("documents", "user_id")
    op.alter_column("documents", "owner_id", new_column_name="user_id")
    op.create_foreign_key(
        "documents_user_id_fkey", "documents", "users", ["user_id"], ["id"],
        ondelete="CASCADE",
    )
    op.create_index("documents_user_id_idx", "documents", ["user_id"])


def downgrade() -> None:
    op.drop_index("documents_user_id_idx", table_name="documents")
    op.drop_constraint("documents_user_id_fkey", "documents", type_="foreignkey")
    op.alter_column(
        "documents",
        "user_id",
        type_=sa.Text(),
        postgresql_using="'local'",
        server_default="local",
    )
    op.drop_index("sessions_user_id_idx", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("users")
