"""Pure-logic tests for page-aware chunking (no infra required)."""

from app.services import chunking


def test_chunks_stay_within_a_single_page():
    pages = ["word " * 500, "term " * 500]
    chunks = chunking.chunk_pages(pages, chunk_size=700, chunk_overlap=100)

    assert chunks, "expected at least one chunk"
    for c in chunks:
        assert c.page_from == c.page_to  # M1 citations map to one page


def test_chunk_index_is_sequential_and_global():
    pages = ["alpha " * 2000, "beta " * 2000]
    chunks = chunking.chunk_pages(pages, chunk_size=700, chunk_overlap=100)

    indexes = [c.chunk_index for c in chunks]
    assert indexes == list(range(len(chunks)))


def test_pages_are_one_indexed_and_blank_pages_skipped():
    pages = ["", "content here on the second page", ""]
    chunks = chunking.chunk_pages(pages, chunk_size=700, chunk_overlap=100)

    assert len(chunks) == 1
    assert chunks[0].page_from == 2


def test_long_page_splits_into_multiple_overlapping_chunks():
    pages = ["token " * 5000]
    chunks = chunking.chunk_pages(pages, chunk_size=700, chunk_overlap=100)

    assert len(chunks) > 1
    assert all(c.page_from == 1 for c in chunks)


def test_empty_document_yields_no_chunks():
    assert chunking.chunk_pages([], chunk_size=700, chunk_overlap=100) == []
    assert chunking.chunk_pages(["", "  "], chunk_size=700, chunk_overlap=100) == []
