"""Deleting a document must delete the uploaded PDF too. Requires Postgres.

An endpoint test on purpose: the original bug was not that file removal was
wrong, it was that ``DELETE /documents/{id}`` never removed the file at all.
A unit test on the helper would have passed throughout.

The fixture is a deliberately unreadable PDF — M1 requires those to be
accepted and marked ``failed`` — so the upload still writes a file while
indexing needs no embedding server.
"""

import asyncio
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.routers.documents import _discard_stored_file, _storage_path


def _db_available() -> bool:
    async def check():
        try:
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.run(check())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")

BROKEN_PDF = b"%PDF-1.4 not actually a pdf"


def test_delete_removes_the_uploaded_file():
    async def scenario():
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                email = f"cleanup-{uuid.uuid4().hex[:8]}@example.com"
                signup = await c.post(
                    "/auth/signup", json={"email": email, "password": "password123"}
                )
                assert signup.status_code == 201

                upload = await c.post(
                    "/documents",
                    files={"file": ("broken.pdf", BROKEN_PDF, "application/pdf")},
                )
                assert upload.status_code == 202
                doc_id = uuid.UUID(upload.json()["id"])
                path = _storage_path(doc_id, "broken.pdf")
                assert os.path.exists(path), "upload should have stored the file"

                deleted = await c.delete(f"/documents/{doc_id}")
                return deleted.status_code, path
        finally:
            await engine.dispose()

    status, path = asyncio.run(scenario())
    assert status == 204
    assert not os.path.exists(path), "the PDF outlived the document it belonged to"


def test_discard_tolerates_a_file_that_was_never_written():
    """Documents indexed from a path (the eval corpora) have no stored file;
    a delete must still succeed rather than 500."""
    _discard_stored_file(uuid.uuid4(), "never-written.pdf")


def test_discard_only_touches_its_own_document():
    other = _storage_path(uuid.uuid4(), "keep.pdf")
    with open(other, "wb") as fh:
        fh.write(b"keep me")
    try:
        _discard_stored_file(uuid.uuid4(), "keep.pdf")
        assert os.path.exists(other)
    finally:
        os.remove(other)


def test_storage_dir_is_not_escaped_by_a_traversal_filename():
    """A stored name is derived from user input; it must stay in storage_dir."""
    path = _storage_path(uuid.uuid4(), "../../etc/passwd")
    assert os.path.dirname(os.path.abspath(path)) == os.path.abspath(
        settings.storage_dir
    )
