"""Keep the test suite from leaving anything behind.

Tests go through the real HTTP API, which writes uploaded PDFs to
``STORAGE_DIR`` and creates user rows. Cleaning those up is easy to forget in
any single test, and forgetting is silent — the run passes and the mess only
shows up later as a directory full of ``broken.pdf`` and a user table nobody
can read. Two runs left 42 accounts and 23 files behind before this existed.

So the cleanup lives here instead of in each test: storage is redirected to a
temporary directory for the whole session, and any account created against
``@example.com`` is swept afterwards. Individual tests can still clean up
eagerly — this is the net under them, not a replacement.
"""

import asyncio
import tempfile

import pytest
from sqlalchemy import text

from app.config import settings


@pytest.fixture(autouse=True, scope="session")
def isolated_storage():
    """Point uploads at a temp directory so the repo's storage/ stays clean.

    ``_storage_path`` reads ``settings.storage_dir`` per call, so swapping the
    value is enough — no monkeypatching of the route.
    """
    original = settings.storage_dir
    with tempfile.TemporaryDirectory(prefix="docs-rag-test-") as tmp:
        settings.storage_dir = tmp
        yield tmp
    settings.storage_dir = original


@pytest.fixture(autouse=True, scope="session")
def sweep_test_accounts(isolated_storage):
    """Remove accounts the suite created, after it finishes.

    Only ``@example.com`` — the seed account that owns the evaluation corpus
    uses a different domain and must survive. Documents, chunks, conversations,
    usage and traces all cascade from the user row.
    """
    yield

    async def sweep() -> int:
        # Imported late: this runs after the tests, and importing the engine at
        # module scope would bind it to whichever loop imported it first.
        from app.db import SessionLocal, engine

        try:
            async with SessionLocal() as session:
                result = await session.execute(
                    text("DELETE FROM users WHERE email LIKE '%@example.com'")
                )
                await session.commit()
                return result.rowcount or 0
        finally:
            await engine.dispose()

    try:
        removed = asyncio.run(sweep())
    except Exception:
        return  # No database: nothing was created, nothing to sweep.

    if removed:
        print(f"\n[conftest] 테스트 계정 {removed}개 정리")
