"""Document endpoints: upload, status, list, delete."""

import os
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import Document
from app.schemas import DocumentCreated, DocumentStatus
from app.services import ingest

router = APIRouter(prefix="/documents", tags=["documents"])


def _storage_path(document_id: uuid.UUID, filename: str) -> str:
    os.makedirs(settings.storage_dir, exist_ok=True)
    safe = os.path.basename(filename)
    return os.path.join(settings.storage_dir, f"{document_id}_{safe}")


@router.post("", status_code=http_status.HTTP_202_ACCEPTED, response_model=DocumentCreated)
async def upload_document(
    file: UploadFile,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> DocumentCreated:
    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise HTTPException(
            status_code=http_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PDF uploads are supported in M1",
        )

    doc = Document(
        filename=file.filename or "upload.pdf",
        mime_type=file.content_type,
        status="pending",
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    path = _storage_path(doc.id, doc.filename)
    with open(path, "wb") as fh:
        fh.write(await file.read())

    background.add_task(ingest.index_document, doc.id, path)
    return DocumentCreated(id=doc.id, filename=doc.filename, status=doc.status)


@router.get("", response_model=list[DocumentStatus])
async def list_documents(
    session: AsyncSession = Depends(get_session),
) -> list[Document]:
    return await ingest.list_documents(session)


@router.get("/{document_id}", response_model=DocumentStatus)
async def get_document(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> Document:
    doc = await ingest.get_document(session, document_id)
    if doc is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="not found")
    return doc


@router.delete("/{document_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    doc = await ingest.get_document(session, document_id)
    if doc is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="not found")
    await session.delete(doc)  # chunks cascade
    await session.commit()
