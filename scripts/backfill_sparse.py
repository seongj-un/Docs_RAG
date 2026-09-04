"""Backfill BGE-M3 sparse vectors for chunks indexed before M2.

Finds chunks with a NULL ``sparse_embedding`` (rows written by the M1
dense-only pipeline), re-embeds their stored ``content`` via ``/embed_full``,
and fills the sparse column. Dense vectors are left untouched.

Run once after deploying M2 with the sparse-capable embedding server up:

    python -m scripts.backfill_sparse
"""

import asyncio

from sqlalchemy import select

from app.db import SessionLocal, engine
from app.models import Chunk
from app.services import embeddings

_BATCH = 64


async def backfill() -> int:
    total = 0
    async with SessionLocal() as session:
        while True:
            rows = list(
                (
                    await session.execute(
                        select(Chunk)
                        .where(Chunk.sparse_embedding.is_(None))
                        .limit(_BATCH)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                break

            _, sparse = await embeddings.embed_full([r.content for r in rows])
            for row, svec in zip(rows, sparse, strict=True):
                row.sparse_embedding = svec
            await session.commit()

            total += len(rows)
            print(f"backfilled {total} chunks...")
    return total


async def main() -> None:
    n = await backfill()
    await engine.dispose()
    print(f"done. {n} chunks backfilled.")


if __name__ == "__main__":
    asyncio.run(main())
