"""Render an eval corpus to PDF, one clause per page.

``insert_text`` does not wrap and silently clips at the page edge, which would
drop the very amounts and identifiers that keyword queries target. We use a
text box and verify that every clause round-trips through extraction, so a
clipped fixture fails loudly instead of silently invalidating an experiment.
"""

import pymupdf

_MARGIN = 60
_FONT = "korea"  # PyMuPDF CJK font with Hangul glyphs ("china-s" drops them)
_FONTSIZE = 13


def build_pdf(path: str, clauses: list[str]) -> int:
    doc = pymupdf.open()
    try:
        for text in clauses:
            page = doc.new_page()
            rect = pymupdf.Rect(
                _MARGIN,
                80,
                page.rect.width - _MARGIN,
                page.rect.height - 80,
            )
            if page.insert_textbox(rect, text, fontsize=_FONTSIZE, fontname=_FONT) < 0:
                raise RuntimeError(f"clause did not fit on a page: {text[:40]!r}")
        doc.save(path)
    finally:
        doc.close()
    return len(clauses)


def verify_pdf(path: str, clauses: list[str]) -> None:
    """Assert every clause survives extraction intact (whitespace-normalized)."""
    doc = pymupdf.open(path)
    try:
        if doc.page_count != len(clauses):
            raise AssertionError(f"expected {len(clauses)} pages, got {doc.page_count}")
        for i, page in enumerate(doc):
            got = " ".join(page.get_text("text").split())
            want = " ".join(clauses[i].split())
            if got != want:
                raise AssertionError(
                    f"page {i + 1} did not round-trip:\n  want={want!r}\n  got ={got!r}"
                )
    finally:
        doc.close()
