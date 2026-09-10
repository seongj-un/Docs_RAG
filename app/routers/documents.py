"""Document endpoints: upload, status, list, delete.

All routes require authentication and operate only on the caller's own
documents. A document owned by someone else answers 404, not 403, so the API
never confirms that another user's document exists.
"""

import os
import uuid

import pymupdf
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, UploadFile
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.deps import VERIFICATION_REQUIRED, get_current_user
from app.models import Document, User
from app.schemas import DocumentCreated, DocumentStatus
from app.services import cache, ingest
from app.services.ratelimit import upload_limiter

router = APIRouter(prefix="/documents", tags=["documents"])

_NOT_FOUND = HTTPException(
    status_code=http_status.HTTP_404_NOT_FOUND, detail="not found"
)


def _storage_path(document_id: uuid.UUID, filename: str) -> str:
    os.makedirs(settings.storage_dir, exist_ok=True)
    safe = os.path.basename(filename)
    return os.path.join(settings.storage_dir, f"{document_id}_{safe}")


def _discard_stored_file(document_id: uuid.UUID, filename: str) -> None:
    """Delete the uploaded PDF that belonged to a now-deleted document.

    Called after the row is gone, and never allowed to fail the request: the
    document is already deleted as far as the user is concerned, and turning
    that into a 500 would invite a retry that 404s. A file left behind is a
    leak to clean up, not a reason to report the delete as failed.
    """
    try:
        os.remove(_storage_path(document_id, filename))
    except FileNotFoundError:
        pass  # never written (indexed from a path) or already removed
    except OSError:
        pass  # permissions, read-only mount — the row is still gone


def _page_count(data: bytes) -> int | None:
    """Page count, or None if the bytes are not a readable PDF.

    Unreadable files are *not* rejected here — M1 requires them to be accepted
    and then recorded as ``failed`` by the indexer, so the failure path stays
    observable instead of turning into a 4xx.
    """
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        return None


@router.post("", status_code=http_status.HTTP_202_ACCEPTED, response_model=DocumentCreated)
async def upload_document(
    file: UploadFile,
    background: BackgroundTasks,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DocumentCreated:
    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise HTTPException(
            status_code=http_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PDF uploads are supported in M1",
        )

    client_ip = request.client.host if request.client else "unknown"
    if not upload_limiter.allow(f"user:{user.id}") or not upload_limiter.allow(
        f"ip:{client_ip}"
    ):
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="upload rate limit exceeded",
            headers={"Retry-After": "60"},
        )

    # 인증 여부는 파일을 읽기 전에 본다 — 거절할 업로드에 50MB 를 읽을
    # 이유가 없다.
    if not user.email_verified and await ingest.usage.unverified_upload_exceeded(
        session, user.id
    ):
        raise VERIFICATION_REQUIRED

    # Guards run before the row is created, so rejected uploads cost nothing
    # and leave no half-state behind.
    data = await file.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds {settings.max_upload_mb}MB",
        )

    pages = _page_count(data)
    if pages is not None:
        if pages > settings.max_upload_pages:
            raise HTTPException(
                status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"document exceeds {settings.max_upload_pages} pages",
            )
        if await ingest.usage.upload_quota_exceeded(session, user.id, pages):
            raise HTTPException(
                status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
                detail="monthly upload page quota exceeded",
                headers={"Retry-After": "3600"},
            )

    doc = Document(
        user_id=user.id,
        filename=file.filename or "upload.pdf",
        mime_type=file.content_type,
        status="pending",
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    # 수락 시점에 남긴다. 색인 성공 시 기록되는 ``ingest`` 와 다른 사건이다:
    # 그쪽은 백그라운드가 끝나야 생겨서, 색인 전에 연달아 던진 업로드가
    # 카운터를 0으로 본 채 전부 통과하고 실패한 업로드는 세어지지도 않는다.
    await ingest.usage.record(session, user.id, "upload")

    path = _storage_path(doc.id, doc.filename)
    with open(path, "wb") as fh:
        fh.write(data)

    background.add_task(ingest.index_document, doc.id, path)
    return DocumentCreated(id=doc.id, filename=doc.filename, status=doc.status)


@router.get("", response_model=list[DocumentStatus])
async def list_documents(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Document]:
    return await ingest.list_documents(session, user_id=user.id)


@router.get("/{document_id}", response_model=DocumentStatus)
async def get_document(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Document:
    doc = await ingest.get_document(session, document_id, user_id=user.id)
    if doc is None:
        raise _NOT_FOUND
    return doc


@router.delete("/{document_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    doc = await ingest.get_document(session, document_id, user_id=user.id)
    if doc is None:
        raise _NOT_FOUND
    stored = (doc.id, doc.filename)  # read before the row goes away
    await session.delete(doc)  # chunks and document-scoped cache cascade
    await session.commit()
    # The row is authoritative, so it goes first; the file follows. Doing it
    # the other way round could leave a document pointing at nothing.
    _discard_stored_file(*stored)
    # Corpus-wide cached answers were computed over a corpus that no longer
    # exists; no foreign key covers them, so drop them explicitly.
    await cache.invalidate_corpus_wide(session, user.id)
