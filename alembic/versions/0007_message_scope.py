"""M5: record the scope each answer was given under

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-07

The refusal notice says "지금 보고 있는 문서 안에는…" or "올려둔 문서
어디에도…" depending on what the question was asked against. Reading the
screen's *current* scope made that text change whenever the selector moved —
a past refusal would start describing a scope it was never asked under. The
frontend fixed that for the live session by carrying the scope on the message
it just received, but a thread reopened from the server had nothing to read.

No foreign key, and no ON DELETE: this is a record of what was asked, and it
has to stay readable after the document is gone. ``conversations`` uses SET
NULL for its own scope because that column selects a default for the *next*
question; this one describes a question already asked, so erasing it would
destroy the thing it exists to remember. Same reasoning as ``traces``.

NULL is ambiguous in principle — it means both "asked across everything" and
"written before this column existed". In practice there are no rows from
before, and the frontend treats NULL as "everything", which is what a row
written from here on actually means.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("scope_document_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "scope_document_id")
