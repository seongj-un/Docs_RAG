"""M2: add chunks.sparse_embedding (BGE-M3 lexical weights)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-04

Adds a nullable ``sparsevec`` column for hybrid retrieval. Nullable so existing
M1 rows remain valid until backfilled (scripts/backfill_sparse.py).

No ANN index on the sparse column for M2: sparse top-N runs as a bounded
inner-product scan over the (small, single-user) corpus, and pgvector's
sparsevec indexing constraints are deferred per the spec (D10).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from pgvector.sqlalchemy import SPARSEVEC

from alembic import op
from app.config import settings

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chunks",
        sa.Column(
            "sparse_embedding", SPARSEVEC(settings.embed_sparse_dim), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("chunks", "sparse_embedding")
