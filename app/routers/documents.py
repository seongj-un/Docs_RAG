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
    # 이유가 없다. 잠금 없이 미리 보는 값이라 이 통과는 최종 결정이 아니다
    # — 진짜 결정은 아래, 쪽수까지 알고 잠금을 쥔 뒤에 다시 본다.
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
    # 절대 상한(개별 업로드의 쪽수 자체)은 다른 요청과 무관하다 — 경쟁이
    # 있을 수 없으니 잠글 이유도 없다. 계정 이력에 기대는 쿼터 검사만
    # 아래 잠금 구간으로 옮긴다.
    if pages is not None and pages > settings.max_upload_pages:
        raise HTTPException(
            status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"document exceeds {settings.max_upload_pages} pages",
        )

    # 여기서부터 커밋까지가 "세고 나서 쓴다"를 원자로 만드는 구간이다.
    # 잠금 없이는 분당 5회 버스트 전체가 위의 사전 검사와 똑같이(터지기
    # 전) 낡은 집계를 보고 전부 통과해버린다 — 미인증 계정의 평생 한도가
    # 문서 1개/50쪽이어도 버스트 한 번으로 5개/약 250쪽까지 새어나가는
    # 것이 바로 이 틈이었다. 느릴 수 있는 일(파일 읽기·PDF 파싱)은 이미
    # 위에서 끝났으므로, 잠근 구간에는 로컬 쿼리 몇 번과 행 두 개 삽입만
    # 남는다 — 짧은 이유는 원래 가벼워서가 아니라 무거운 일을 잠그기
    # 전에 이미 다 끝내 두었기 때문이다.
    await ingest.usage.acquire_quota_lock(session, user.id, "upload")
    try:
        if not user.email_verified and await ingest.usage.unverified_upload_exceeded(
            session, user.id
        ):
            raise VERIFICATION_REQUIRED
        if pages is not None:
            # 문서 개수 게이트(위)는 이력만 본다 — 미인증 계정의 첫 업로드는
            # 그 이력이 0이라 그냥 통과한다. 그런데 비용은 문서 수가 아니라
            # 쪽수에 비례하므로, 첫 업로드라도 500쪽짜리면 여기서 따로
            # 막아야 한다.
            if not user.email_verified and await ingest.usage.unverified_pages_exceeded(
                session, user.id, pages
            ):
                raise VERIFICATION_REQUIRED
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

        # 수락 시점에 남긴다. 색인 성공 시 기록되는 ``ingest`` 와 다른
        # 사건이다: 그쪽은 백그라운드가 끝나야 생겨서, 색인 전에 연달아
        # 던진 업로드가 카운터를 0으로 본 채 전부 통과하고 실패한 업로드는
        # 세어지지도 않는다. 쪽수도 함께 남긴다 — ``unverified_pages_exceeded``
        # 가 이력을 볼 때 쓰는 유일한 값이라, 안 남기면 미인증 계정의 쪽수
        # 게이트는 매번 "이력 0"만 보게 된다. 페이지 수를 모르는 파일
        # (``pages is None``)은 0으로 남는다 — 어차피 그런 파일은 위의
        # 쪽수 게이트 자체가 건너뛴다.
        #
        # Document 행과 한 커밋으로 묶는다 — 이 업로드를 받아들인다는 것은
        # 하나의 결정이라, 둘을 따로 커밋하면 그 사이에 죽었을 때 하나만
        # 살아남는 절반짜리 상태가 생긴다.
        await ingest.usage.reserve(session, user.id, "upload", pages=pages or 0)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    await session.refresh(doc)

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
