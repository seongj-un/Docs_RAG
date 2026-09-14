"""Index a synthetic document through the real ingest pipeline.

Every eval runner needs the same four steps — render the clauses to a PDF,
delete whatever a previous run left behind under that filename, insert a
``Document`` owned by the seed account, and run ``ingest.index_document`` — so
they live here instead of being copied into each runner.

Going through the production ingest path is the point: a fixture indexed by a
shortcut would not exercise the chunker or the sparse vectors, and the numbers
would describe a pipeline nobody runs.
"""

import uuid

from sqlalchemy import select

from eval import pdf

from app.constants import SEED_USER_ID
from app.db import SessionLocal
from app.models import Chunk, Document
from app.services import ingest


async def index_pages(
    clauses: list[str],
    path: str,
    doc_name: str,
    *,
    owner_id: uuid.UUID = SEED_USER_ID,
    quiet: bool = False,
) -> uuid.UUID:
    """Build the fixture PDF and index it, replacing any prior run's copy."""
    pdf.build_pdf(path, clauses)
    pdf.verify_pdf(path, clauses)

    async with SessionLocal() as session:
        stale = (
            (await session.execute(select(Document).where(Document.filename == doc_name)))
            .scalars()
            .all()
        )
        for doc in stale:
            await session.delete(doc)
        await session.commit()

        # Eval fixtures default to the seed account (the same owner migration
        # 0003 assigned pre-M3 documents to), so runs stay isolated from real
        # users. M7 하네스는 코퍼스를 정확히 통제해야 해서 자기 계정을 넘긴다
        # (app/constants.py 의 EVAL_USER_ID).
        doc = Document(
            user_id=owner_id,
            filename=doc_name,
            mime_type="application/pdf",
            status="pending",
        )
        session.add(doc)
        await session.commit()
        await session.refresh(doc)
        doc_id = doc.id

    await ingest.index_document(doc_id, path)

    async with SessionLocal() as session:
        doc = await session.get(Document, doc_id)
        chunks = (
            (await session.execute(select(Chunk).where(Chunk.document_id == doc_id)))
            .scalars()
            .all()
        )
        with_sparse = sum(1 for c in chunks if c.sparse_embedding is not None)
        if not quiet:
            print(f"[index] {doc_name} status={doc.status} pages={doc.num_pages} "
                  f"chunks={len(chunks)} with_sparse={with_sparse}")
        if doc.status != "ready":
            raise RuntimeError(f"indexing failed: {doc.error}")
    return doc_id


async def load_chunks(session, doc_id: uuid.UUID) -> list[Chunk]:
    """Every chunk of a document in index order — what gold resolution needs."""
    result = await session.execute(
        select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.chunk_index)
    )
    return list(result.scalars().all())
