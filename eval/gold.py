"""Resolve gold *spans* (source snippets) to the chunk ids of a live index.

The M7 golden set does not store chunk ids. It stores the snippet of source
text that answers the question, and this module turns that snippet into the
set of chunks it overlaps **at the moment the harness runs**.

왜 이렇게 도느냐. 정답을 청크 UUID 로 박아두면 두 가지가 동시에 깨진다.
합성 코퍼스는 인덱싱할 때마다 UUID 가 새로 생기므로 데이터셋이 한 번
인덱싱하고 나면 죽고, 더 중요하게는 **W5 가 청킹 전략을 바꾸는 순간 골든셋
전체가 무효가 된다** — 청킹은 M7 의 주요 실험 대상이다(W1 설계 원칙). 원문
스니펫은 청킹이 어떻게 바뀌든 문서에 그대로 남아 있으므로, 종속성이 처음부터
생기지 않는다.

경계에 걸친 스니펫은 겹치는 청크를 **전부** 정답으로 친다. 그래서 문자
위치까지 계산한다 — 단순히 "스니펫을 통째로 포함하는 청크"만 찾으면, 하필
청크 경계를 넘어간 스니펫은 정답이 0개가 되어 멀쩡한 검색이 실패로 기록된다.

매칭이 0건이면 예외를 던진다. 이건 버그가 아니라 신호다: 문서는 바뀌었는데
골든셋이 따라가지 못했다는 뜻이고, 조용히 건너뛰면 그 문항은 영원히 0점으로
평균을 갉아먹으면서 아무도 이유를 모른다.
"""

import uuid
from dataclasses import dataclass

# 한 문서 안에서 스니펫이 이보다 많이 걸리면 그 스니펫은 질문의 정답을
# 지목하지 못한다 — 고유성이 없다는 뜻이라 라벨 쪽을 고쳐야 한다(W1 규칙).
MAX_MATCHES = 3


class GoldResolutionError(RuntimeError):
    """A gold span could not be mapped onto the indexed document."""


@dataclass(frozen=True)
class SourceChunk:
    """A chunk as it exists in the index, reduced to what resolution needs."""

    chunk_id: uuid.UUID
    page_from: int | None
    content: str


@dataclass(frozen=True)
class PlacedChunk:
    """A chunk plus its character span inside the normalized document text."""

    chunk_id: uuid.UUID
    page_from: int | None
    start: int
    end: int


def normalize(text: str) -> str:
    """Collapse all whitespace, so PDF extraction artifacts do not decide hits.

    추출기는 같은 문장을 줄바꿈이나 이중 공백으로 다르게 내놓는다. 스니펫을
    사람이 원문에서 복사해 붙이는 이상, 공백 차이로 매칭이 갈리면 라벨이
    아니라 추출기를 평가하게 된다.
    """
    return " ".join(text.split())


def build_document_text(pages: list[str]) -> tuple[str, list[tuple[int, int]]]:
    """Normalized full-document text plus each page's span within it.

    Pages are joined with a single space: a snippet that crosses a page break
    then still matches, and the page spans let a match be reported by page.
    """
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for page in pages:
        text = normalize(page)
        if parts:
            cursor += 1  # 구분용 공백 한 칸
        spans.append((cursor, cursor + len(text)))
        parts.append(text)
        cursor += len(text)
    return " ".join(parts), spans


def find_snippet(doc_text: str, snippet: str) -> list[tuple[int, int]]:
    """Every occurrence of ``snippet`` in normalized document text."""
    needle = normalize(snippet)
    if not needle:
        return []
    found: list[tuple[int, int]] = []
    start = doc_text.find(needle)
    while start != -1:
        found.append((start, start + len(needle)))
        start = doc_text.find(needle, start + 1)
    return found


def place_chunks(
    pages: list[str], chunks: list[SourceChunk]
) -> list[PlacedChunk]:
    """Locate each chunk's text inside the normalized document.

    Chunking guarantees a chunk's ``content`` is a verbatim substring of one
    page (see ``app/services/chunking.py``), so each chunk is searched inside
    its own page's span. Consecutive chunks of a page overlap by design
    (``CHUNK_OVERLAP``), but they are ordered by start position — so the search
    cursor only ever moves forward by one character, never past the next chunk.

    A chunk whose text cannot be located is dropped from placement rather than
    guessed at: it can still be matched by plain containment, and inventing an
    offset would silently mark the wrong neighbours as gold.
    """
    doc_text, page_spans = build_document_text(pages)
    placed: list[PlacedChunk] = []
    cursors: dict[int, int] = {}
    for chunk in chunks:
        text = normalize(chunk.content)
        if not text:
            continue
        page_index = (chunk.page_from or 1) - 1
        if 0 <= page_index < len(page_spans):
            lo, hi = page_spans[page_index]
        else:
            lo, hi = 0, len(doc_text)
        start = doc_text.find(text, cursors.get(page_index, lo), hi)
        if start == -1:
            # 페이지 밖일 리 없지만, 라벨이 아니라 추출 차이로 어긋났을 수
            # 있으니 문서 전체에서 한 번 더 찾아본다.
            start = doc_text.find(text)
            if start == -1:
                continue
        cursors[page_index] = start + 1
        placed.append(
            PlacedChunk(chunk.chunk_id, chunk.page_from, start, start + len(text))
        )
    return placed


def resolve_span(
    snippet: str,
    doc_text: str,
    placed: list[PlacedChunk],
    *,
    where: str = "",
) -> set[uuid.UUID]:
    """Chunk ids overlapping ``snippet``. Raises when the snippet is unusable."""
    matches = find_snippet(doc_text, snippet)
    if not matches:
        raise GoldResolutionError(
            f"gold 스니펫이 문서에서 사라졌다{where}: {snippet[:40]!r} — "
            "문서가 바뀌었는데 골든셋이 따라가지 못했다는 뜻이다."
        )
    if len(matches) > MAX_MATCHES:
        raise GoldResolutionError(
            f"gold 스니펫이 {len(matches)}곳에 걸린다{where} (상한 {MAX_MATCHES}): "
            f"{snippet[:40]!r} — 정답을 지목하지 못하므로 스니펫을 좁혀야 한다."
        )

    ids = {
        chunk.chunk_id
        for chunk in placed
        for start, end in matches
        if chunk.start < end and start < chunk.end
    }
    if not ids:
        raise GoldResolutionError(
            f"gold 스니펫이 어떤 청크에도 닿지 않는다{where}: {snippet[:40]!r} — "
            "인덱싱이 그 구간을 빠뜨렸다는 뜻이다."
        )
    return ids


def resolve_question(
    snippets: list[str],
    pages: list[str],
    chunks: list[SourceChunk],
    *,
    where: str = "",
) -> list[set[uuid.UUID]]:
    """One chunk-id set per gold span, in the dataset's span order."""
    doc_text, _ = build_document_text(pages)
    placed = place_chunks(pages, chunks)
    return [
        resolve_span(snippet, doc_text, placed, where=where) for snippet in snippets
    ]


def match_count(snippet: str, pages: list[str]) -> int:
    """How many places a snippet hits in a document — no index needed.

    ``eval.harness validate`` uses this so the uniqueness rule is checked at
    every commit, long before anyone has a database or an embedding server.
    """
    doc_text, _ = build_document_text(pages)
    return len(find_snippet(doc_text, snippet))


def pages_of(snippet: str, pages: list[str]) -> list[int]:
    """1-based pages a snippet falls on (several when it crosses a break)."""
    doc_text, page_spans = build_document_text(pages)
    hits = find_snippet(doc_text, snippet)
    return sorted(
        {
            i + 1
            for i, (lo, hi) in enumerate(page_spans)
            for start, end in hits
            if lo < end and start < hi
        }
    )
