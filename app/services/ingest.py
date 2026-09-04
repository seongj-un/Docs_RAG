"""Background indexing pipeline: parse -> chunk -> embed -> store.

Runs as a FastAPI background task. Owns its own DB session (the request session
is already closed by the time this runs) and never raises out to the caller:
any failure is captured on the document row as ``status='failed'`` + ``error``.
"""

import uuid

import pymupdf
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Chunk, Document
from app.services import chunking, embeddings


def _extract_pages(path: str) -> list[str]:
    """Return per-page text (index 0 == page 1)."""
    pages: list[str] = []
    with pymupdf.open(path) as doc:
        for page in doc:
            pages.append(page.get_text("text"))
    return pages


async def index_document(document_id: uuid.UUID, file_path: str) -> None:
    async with SessionLocal() as session:
        doc = await session.get(Document, document_id)
        if doc is None:
            return
        doc.status = "processing"
        await session.commit()

        try:
            pages = _extract_pages(file_path)
            doc.num_pages = len(pages)

            parts = chunking.chunk_pages(
                pages,
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
            )
            if not parts:
                raise ValueError("no extractable text in PDF")

            contents = [p.content for p in parts]
            # M2: store dense + sparse when hybrid is on so docs are
            # hybrid-ready; fall back to dense-only otherwise.
            if settings.hybrid_enabled:
                dense, sparse = await embeddings.embed_full(contents)
            else:
                dense = await embeddings.embed_texts(contents)
                sparse = [None] * len(contents)

            session.add_all(
                [
                    Chunk(
                        document_id=doc.id,
                        chunk_index=p.chunk_index,
                        page_from=p.page_from,
                        page_to=p.page_to,
                        content=p.content,
                        token_count=p.token_count,
                        embed_model=settings.embed_model,
                        embedding=dvec,
                        sparse_embedding=svec,
                    )
                    for p, dvec, svec in zip(parts, dense, sparse, strict=True)
                ]
            )
            doc.status = "ready"
            doc.error = None
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - failures must be recorded, not raised
            await session.rollback()
            # Re-load in case the session state was lost during rollback.
            doc = await session.get(Document, document_id)
            if doc is not None:
                doc.status = "failed"
                doc.error = f"{type(exc).__name__}: {exc}"[:2000]
                await session.commit()


async def get_document(session, document_id: uuid.UUID) -> Document | None:
    return await session.get(Document, document_id)


async def list_documents(session) -> list[Document]:
    result = await session.execute(select(Document).order_by(Document.created_at.desc()))
    return list(result.scalars().all())
