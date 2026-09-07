"""M5: conversations and messages

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-07

Chat history. ``POST /query`` was a one-shot call that left no record; the
sidebar in M5 needs threads, so turns are persisted here.

``conversations.scope_document_id`` is SET NULL rather than CASCADE: the thread
is the user's record of what they asked, and it should survive the deletion of
the document it was scoped to. ``messages.citations`` snapshots the footnote
mapping at answer time so reopening an old thread shows the same evidence even
after the chunks behind it change.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("scope_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["scope_document_id"], ["documents.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "conversations_user_created_idx", "conversations", ["user_id", "created_at"]
    )

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", postgresql.JSONB(), nullable=True),
        sa.Column("refused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "messages_conversation_idx", "messages", ["conversation_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("messages_conversation_idx", table_name="messages")
    op.drop_table("messages")
    op.drop_index("conversations_user_created_idx", table_name="conversations")
    op.drop_table("conversations")
