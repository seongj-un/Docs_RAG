"""Page-aware chunking.

M1 splits text page-by-page so every chunk maps to an exact page range, which
keeps ``[p.N]`` citations trustworthy (a completion criterion). Token budgets
are approximated by word count to avoid a tokenizer dependency; M2 replaces this
with the real BGE-M3 tokenizer for precise token windows.
"""

from dataclasses import dataclass

# Rough words-per-token factor. English prose runs ~0.75 words/token; we bias
# slightly high so chunks stay under the embedding model's context window.
_WORDS_PER_TOKEN = 0.75


@dataclass
class Chunk:
    chunk_index: int
    page_from: int
    page_to: int
    content: str
    token_count: int


def approx_token_count(text: str) -> int:
    words = len(text.split())
    return max(1, round(words / _WORDS_PER_TOKEN))


def _word_budget(chunk_size_tokens: int) -> int:
    return max(1, round(chunk_size_tokens * _WORDS_PER_TOKEN))


def chunk_pages(
    pages: list[str],
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
    """Split per-page text into overlapping word windows.

    Args:
        pages: page texts, index 0 == page 1.
        chunk_size: target chunk size in (approximate) tokens.
        chunk_overlap: overlap between consecutive windows in (approx) tokens.

    Returns chunks in document order with a global ``chunk_index``. Each chunk
    stays within a single page, so ``page_from == page_to``.
    """
    budget = _word_budget(chunk_size)
    overlap = min(_word_budget(chunk_overlap), budget - 1) if budget > 1 else 0
    step = max(1, budget - overlap)

    chunks: list[Chunk] = []
    index = 0
    for page_no, raw in enumerate(pages, start=1):
        words = raw.split()
        if not words:
            continue
        start = 0
        while start < len(words):
            window = words[start : start + budget]
            content = " ".join(window)
            chunks.append(
                Chunk(
                    chunk_index=index,
                    page_from=page_no,
                    page_to=page_no,
                    content=content,
                    token_count=approx_token_count(content),
                )
            )
            index += 1
            if start + budget >= len(words):
                break
            start += step
    return chunks
