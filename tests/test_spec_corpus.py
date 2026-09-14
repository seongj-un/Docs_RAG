"""M7 W5: the spec corpus has to keep the properties the experiment needs.

``eval/corpora/spec.py`` 는 import 시점에 자기 검사를 돌린다(wide·deep 의
관례). 여기서 한 번 더 보는 것은 그 검사가 **토크나이저 없이 잴 수 있는
것만** 보기 때문이다. 정작 실험이 성립하는지를 결정하는 것은 토큰 수와
PDF 왕복이라, 그쪽은 여기서 잰다.

이 파일이 막으려는 사고는 하나다 — 코퍼스를 손보다가 쪽이 짧아지거나 표가
앞으로 밀려서, 고정 청킹과 섹션 청킹이 **같은 결과를 내는 코퍼스**가 되는
것. 그러면 W5 는 또 "변화 없음"을 결과라고 보고하게 된다. 이 저장소가 이미
두 번 당한 방식이다(RERANK_MAX_CHARS, CAND_K).
"""

import tempfile
from pathlib import Path

import pymupdf
import pytest

from app.config import settings
from app.services import chunking
from eval import datasets, gold, pdf
from eval.corpora import spec
from eval.datasets import synthetic

DATASET = Path(__file__).resolve().parent.parent / "eval" / "datasets" / "spec_golden.jsonl"


@pytest.fixture(scope="module")
def extracted() -> dict[str, list[str]]:
    """Pages as the indexer actually sees them: after a PDF round trip.

    코퍼스 원문이 아니라 **추출 결과**로 재는 것이 요점이다. 헤딩 표기가 PDF
    를 통과하지 못하면 섹션 청킹은 실사용에서 아무 일도 하지 않는데, 원문으로
    재면 그 사실이 영영 안 보인다.
    """
    out: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, pages in spec.DOCUMENTS.items():
            path = str(Path(tmp) / f"{name}.pdf")
            pdf.build_pdf(path, pages)
            pdf.verify_pdf(path, pages)  # 조항이 잘리면 여기서 시끄럽게 죽는다
            with pymupdf.open(path) as doc:
                out[name] = [page.get_text("text") for page in doc]
    return out


def test_headings_survive_pdf_extraction(extracted):
    for name, pages in extracted.items():
        for i, text in enumerate(pages, start=1):
            levels = {
                len(line) - len(line.lstrip("#"))
                for line in text.splitlines()
                if line.startswith("#")
            }
            assert {1, 2, 3} <= levels, f"{name} p{i}: 추출 후 헤딩 단계가 {sorted(levels)}"


def test_every_page_exceeds_the_chunk_budget(extracted):
    """쪽이 CHUNK_SIZE 보다 커야 고정 창이 실제로 쪽을 쪼갠다.

    이걸 어기면 "고정 vs 섹션"은 비교가 아니라 같은 것의 두 이름이 된다 —
    deep.py 가 산 교훈을 청킹 축으로 옮겨 놓은 검사다.
    """
    for name, pages in extracted.items():
        for i, text in enumerate(pages, start=1):
            tokens = len(chunking._offsets(text))
            assert tokens > settings.chunk_size, (
                f"{name} p{i}: {tokens}토큰 — CHUNK_SIZE({settings.chunk_size})를 "
                "넘지 않아 고정 창이 이 쪽을 쪼개지 않는다"
            )


def _table_blocks(text: str) -> list[tuple[int, int]]:
    """(start, end) of each run of consecutive pipe-table lines."""
    blocks: list[tuple[int, int]] = []
    run: tuple[int, int] | None = None
    pos = 0
    for line in text.split("\n"):
        start, end = pos, pos + len(line)
        if line.lstrip().startswith("|"):
            run = (run[0] if run else start, end)
        elif run:
            blocks.append(run)
            run = None
        pos = end + 1
    if run:
        blocks.append(run)
    return blocks


def _tables_cut(pages: list[str], chunks) -> int:
    """How many times a chunk boundary falls strictly inside a table block."""
    by_page: dict[int, list] = {}
    for chunk in chunks:
        by_page.setdefault(chunk.page_from, []).append(chunk)
    cut = 0
    for page_no, text in enumerate(pages, start=1):
        on_page = by_page.get(page_no, [])
        ends = [text.find(c.content) + len(c.content) for c in on_page[:-1]]
        for lo, hi in _table_blocks(text):
            cut += sum(1 for end in ends if lo < end < hi)
    return cut


def test_fixed_chunking_cuts_tables_and_section_chunking_does_not(extracted):
    """Notion W5 가 지목한 실패 모드가 이 코퍼스에서 실제로 일어나는가.

    일어나지 않으면 "섹션 청킹이 표를 지켰다"는 결론은 잴 대상이 없는 주장이다.
    """
    pages = extracted[spec.DOC_API]
    fixed = chunking.chunk_pages(pages, settings.chunk_size, settings.chunk_overlap)
    section = chunking.chunk_pages(
        pages, settings.chunk_size, settings.chunk_overlap, strategy="section"
    )

    assert _tables_cut(pages, fixed) > 0, "고정 창이 표를 한 번도 자르지 않는다"
    assert _tables_cut(pages, section) == 0


def test_the_two_strategies_actually_produce_different_chunks(extracted):
    for name, pages in extracted.items():
        fixed = chunking.chunk_pages(pages, settings.chunk_size, settings.chunk_overlap)
        section = chunking.chunk_pages(
            pages, settings.chunk_size, settings.chunk_overlap, strategy="section"
        )
        assert [c.content for c in fixed] != [c.content for c in section], (
            f"{name}: 두 전략이 같은 청크를 낸다 — 이 문서로는 실험이 성립하지 않는다"
        )


def test_the_heading_prefix_actually_attaches_a_path(extracted):
    chunks = chunking.chunk_pages(
        extracted[spec.DOC_API],
        settings.chunk_size,
        settings.chunk_overlap,
        heading_prefix=True,
    )
    assert all(c.heading_path for c in chunks)
    # 3단 이상 경로가 실재해야 Notion 이 적은 "명세 > 학생 > 목록 조회"가 된다.
    assert any(len(c.heading_path) >= 3 for c in chunks)


def test_duplicate_phrasing_is_real(extracted):
    """중복 표현이 없으면 multi_doc 은 그냥 쉬운 문항이 된다."""
    for sentence in spec._ECHO:
        hits = sum(
            1
            for pages in extracted.values()
            for text in pages
            if gold.normalize(sentence) in gold.normalize(text)
        )
        assert hits >= 12, f"중복 문장이 {hits}쪽에만 있다"


# --- the committed dataset ---


def test_the_dataset_resolves_against_the_provider():
    """스니펫이 문서에서 사라졌거나 여러 곳에 걸리면 여기서 깨진다."""
    dataset = datasets.load(DATASET)
    assert set(dataset.docs) <= set(synthetic.names())

    for question in dataset.questions:
        for span in question.gold_spans:
            count = gold.match_count(span.snippet, synthetic.pages_for(span.doc))
            assert count == 1, f"{question.id}: {span.snippet[:30]!r} 가 {count}곳"


def test_the_dataset_fills_the_types_the_synthetic_set_could_not():
    """freshness 는 synthetic_golden 에서 0문항이었다(FORMAT.md 의 알려진 한계).

    명세 코퍼스에는 변경 이력 쪽이 있어 옛 값과 현행 값이 둘 다 존재한다 —
    그래서 "오래된 값을 물고 오는가"를 처음으로 잴 수 있다.
    """
    counts = datasets.load(DATASET).type_counts()
    assert counts["freshness"] > 0
    assert counts["multi_doc"] > 0
    assert counts["no_answer"] > 0


def test_multi_doc_questions_really_span_two_documents():
    """한 문서 안 두 조항은 multi_doc 이 아니다 — synthetic_golden 의 한계였다."""
    multi = [q for q in datasets.load(DATASET).questions if q.type == "multi_doc"]

    assert multi
    for question in multi:
        assert len(question.docs) >= 2, f"{question.id}: 문서 하나짜리 multi_doc"


def test_gold_snippets_are_short_enough_to_survive_rechunking(extracted):
    """스니펫이 짧아야 청크 경계를 덜 넘는다 — W5 가 경계를 바꾸기 때문이다."""
    dataset = datasets.load(DATASET)
    for strategy in chunking.STRATEGIES:
        placed: dict[str, tuple[str, list]] = {}
        for doc, pages in extracted.items():
            parts = chunking.chunk_pages(
                pages, settings.chunk_size, settings.chunk_overlap, strategy=strategy
            )
            chunks = [
                gold.SourceChunk(uuid_for(i), c.page_from, c.content)
                for i, c in enumerate(parts)
            ]
            doc_text, _ = gold.build_document_text(pages)
            placed[doc] = (doc_text, gold.place_chunks(pages, chunks))

        for question in dataset.scored():
            for span in question.gold_spans:
                doc_text, chunks = placed[span.doc]
                ids = gold.resolve_span(
                    span.snippet, doc_text, chunks, where=f" ({question.id}/{strategy})"
                )
                assert ids, f"{question.id}: {strategy} 에서 정답 청크가 0개"


def uuid_for(index: int):
    import uuid

    return uuid.UUID(int=index)
