"""Document provider for the synthetic datasets.

A dataset's ``gold_spans[].doc`` names a document; something has to turn that
name into the pages the harness indexes and resolves snippets against. This is
that something for the synthetic corpora shipped in ``eval/corpora``.

The real M7 golden set is built on work documents that stay in a private repo,
so the harness takes ``--provider`` and imports whichever module exposes
``names()`` / ``pages_for()``. That keeps the document text out of this
repository while the harness, the format and the metrics stay public — which
is the M7 공개/비공개 원칙 in one seam.
"""

from eval.corpora import golden, hard

# 이름 -> 쪽 텍스트(인덱스 0 == 1쪽). eval/pdf.py 가 한 쪽에 한 조항씩 렌더한다.
_DOCS: dict[str, list[str]] = {
    "lease": golden.LEASE,
    "insurance": golden.INSURANCE,
    "saas": golden.SAAS,
    "fees": hard.CLAUSES,
}


def names() -> list[str]:
    return sorted(_DOCS)


def pages_for(doc: str) -> list[str]:
    try:
        return _DOCS[doc]
    except KeyError:
        raise KeyError(
            f"알 수 없는 문서 {doc!r} — 이 provider 가 아는 것은 {names()} 뿐이다. "
            "비공개 골든셋이면 --provider 로 그쪽 모듈을 가리켜야 한다."
        ) from None
