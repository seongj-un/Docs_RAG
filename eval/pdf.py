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
# 13pt 로 안 들어가는 쪽은 글자를 줄여서 담는다. M7 W5 의 명세 코퍼스
# (eval/corpora/spec.py)는 헤딩과 표가 많아 줄 수가 많고, 그래서 글자 수는
# deep 코퍼스와 비슷한데도 13pt 로는 넘친다. 쪽을 잘라 맞추면 조항이 사라지고
# (verify_pdf 가 잡아 주기는 하지만) 코퍼스 설계가 렌더러 사정에 끌려간다.
# 실제 명세 문서도 본문 글자가 작다 — 줄이는 쪽이 픽스처를 덜 왜곡한다.
_MIN_FONTSIZE = 7


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
            for size in range(_FONTSIZE, _MIN_FONTSIZE - 1, -1):
                if page.insert_textbox(rect, text, fontsize=size, fontname=_FONT) >= 0:
                    break
            else:
                raise RuntimeError(
                    f"clause did not fit on a page even at {_MIN_FONTSIZE}pt: "
                    f"{text[:40]!r}"
                )
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
