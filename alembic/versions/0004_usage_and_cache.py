"""M3 Phase 2: usage_events and query_cache

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-07

``usage_events`` backs the quotas (counted in the database so they survive
restarts and hold across processes). ``query_cache`` backs the semantic cache;
it is scoped to (user, document) and cascades from both, so deleting a document
or a user drops the answers derived from them.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op
from app.config import settings

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "usage_events_user_created_idx", "usage_events", ["user_id", "created_at"]
    )

    op.create_table(
        "query_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("question_embedding", Vector(settings.embed_dim), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("refused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "citations", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
    )
    op.create_index("query_cache_user_doc_idx", "query_cache", ["user_id", "document_id"])


def downgrade() -> None:
    op.drop_index("query_cache_user_doc_idx", table_name="query_cache")
    op.drop_table("query_cache")
    op.drop_index("usage_events_user_created_idx", table_name="usage_events")
    op.drop_table("usage_events")
