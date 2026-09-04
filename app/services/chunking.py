"""Page-aware, tokenizer-based chunking.

M1 splits text page-by-page so every chunk maps to an exact page range, which
keeps ``[p.N]`` citations trustworthy (a completion criterion).

Sizing uses the real BGE-M3 tokenizer (``EMBED_MODEL``) so chunk_size /
chunk_overlap are honest token counts that match what the embedder sees. We
window over the tokenizer's *offset mapping* and slice the original page text,
so a chunk's ``content`` is always a verbatim substring of the source (exact
snippets for citations, and clean text for M2's BM25). If the tokenizer can't
be loaded (e.g. offline), we fall back to whitespace word offsets — the same
windowing logic, just coarser token accounting.
"""

import re
from dataclasses import dataclass
from functools import lru_cache

from app.config import settings

# Fallback token proxy when the real tokenizer is unavailable: ~0.75 words/token.
_WORDS_PER_TOKEN = 0.75
_WORD_RE = re.compile(r"\S+")


@dataclass
class Chunk:
    chunk_index: int
    page_from: int
    page_to: int
    content: str
    token_count: int


@lru_cache(maxsize=1)
def _tokenizer():
    """Load the BGE-M3 fast tokenizer once, or None if unavailable."""
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(settings.embed_model)
    except Exception:
        return None


def _offsets(text: str) -> list[tuple[int, int]]:
    """Return (start, end) char spans per token for ``text``.

    Uses the BGE-M3 tokenizer's offset mapping when available; otherwise falls
    back to whitespace word spans.
    """
    tok = _tokenizer()
    if tok is not None:
        enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
        # Drop zero-width spans (some special/whitespace pieces map to (0, 0)).
        return [(s, e) for s, e in enc["offset_mapping"] if e > s]
    return [(m.start(), m.end()) for m in _WORD_RE.finditer(text)]


def _word_budget(chunk_size_tokens: int) -> int:
    """Fallback-mode budget: convert a token target to a word count."""
    return max(1, round(chunk_size_tokens * _WORDS_PER_TOKEN))


def chunk_pages(
    pages: list[str],
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
    """Split per-page text into overlapping token windows.

    Args:
        pages: page texts, index 0 == page 1.
        chunk_size: target chunk size in tokens.
        chunk_overlap: overlap between consecutive windows in tokens.

    Returns chunks in document order with a global ``chunk_index``. Each chunk
    stays within a single page, so ``page_from == page_to``. When the tokenizer
    is unavailable, ``chunk_size``/``chunk_overlap`` are interpreted against a
    word proxy instead of true tokens.
    """
    real_tokens = _tokenizer() is not None
    budget = chunk_size if real_tokens else _word_budget(chunk_size)
    ov = chunk_overlap if real_tokens else _word_budget(chunk_overlap)
    overlap = min(ov, budget - 1) if budget > 1 else 0
    step = max(1, budget - overlap)

    chunks: list[Chunk] = []
    index = 0
    for page_no, raw in enumerate(pages, start=1):
        if not raw.strip():
            continue
        spans = _offsets(raw)
        if not spans:
            continue
        start = 0
        while start < len(spans):
            window = spans[start : start + budget]
            content = raw[window[0][0] : window[-1][1]].strip()
            if content:
                chunks.append(
                    Chunk(
                        chunk_index=index,
                        page_from=page_no,
                        page_to=page_no,
                        content=content,
                        token_count=len(window),
                    )
                )
                index += 1
            if start + budget >= len(spans):
                break
            start += step
    return chunks
