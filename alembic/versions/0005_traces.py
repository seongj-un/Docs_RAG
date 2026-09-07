"""M4 Phase 1: traces and trace_chunks

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-07

Query-level observability. ``traces`` holds one row per request with per-stage
latency; ``trace_chunks`` holds the chunk ids seen at each retrieval stage
(dense / sparse / rrf / rerank), which is what makes retrieval-vs-generation
failures separable.

Neither ``traces.document_id`` nor ``trace_chunks.chunk_id`` carries a foreign
key on purpose: a trace is history and must stay readable after the document or
chunk it referenced is deleted. Traces do cascade from ``users``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "traces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("hybrid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("refused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("llm_model", sa.Text(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embed_ms", sa.Integer(), nullable=True),
        sa.Column("retrieve_ms", sa.Integer(), nullable=True),
        sa.Column("rerank_ms", sa.Integer(), nullable=True),
        sa.Column("generate_ms", sa.Integer(), nullable=True),
        sa.Column("total_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("traces_user_created_idx", "traces", ["user_id", "created_at"])

    op.create_table(
        "trace_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("page_from", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["trace_id"], ["traces.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "trace_chunks_trace_stage_idx", "trace_chunks", ["trace_id", "stage", "rank"]
    )


def downgrade() -> None:
    op.drop_index("trace_chunks_trace_stage_idx", table_name="trace_chunks")
    op.drop_table("trace_chunks")
    op.drop_index("traces_user_created_idx", table_name="traces")
    op.drop_table("traces")
