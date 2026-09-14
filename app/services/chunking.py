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

M7 W5 adds two *optional* levers on top of that, both off by default:

``strategy="section"``
    Cut at heading boundaries instead of at a fixed token count. A spec
    document's meaning is carried by its sections, and a fixed window happily
    slices a parameter table in half — after which neither half describes a
    whole thing and the field name in the header row is gone from the rows
    that need it.

``heading_prefix=True``
    Attach the enclosing heading path (``명세 > 학생 > 목록 조회``) to what the
    embedder sees. A chunk that starts mid-section carries no heading at all,
    so a short query ("학생 목록 조회 응답 필드") has nothing lexical to match.

**헤딩을 어떻게 알아보는가.** 마크다운식 ``#`` 접두 표기다(``HEADING_RE``).
PDF 추출 텍스트에는 구조가 없다 — ``page.get_text("text")`` 는 글자 크기도
굵기도 버리고 줄만 남긴다. 그래서 실제 PDF 에서 헤딩을 뽑으려면 ``dict``
추출로 폰트 크기·굵기·들여쓰기를 보고 추정하는 별도 단계가 필요하고, 그건
그 자체로 오차가 있는 문제다. 그 오차를 청킹 실험에 섞으면 "섹션 청킹이
좋았다/나빴다"가 청킹 얘기인지 헤딩 추출 얘기인지 구분할 수 없게 된다.
그래서 합성 코퍼스가 표기를 **명시적으로** 준다(``eval/corpora/spec.py``).
한계는 한계대로 적어 둔다: 실제 문서에 이 전략을 켜려면 헤딩 추출기가 먼저
있어야 하고, 이 모듈은 그 추출기가 내놓은 표기를 받는 쪽이다.

``heading_path`` 는 ``content`` 에 섞지 않는다. ``content`` 는 원문의 축자
부분문자열이라는 계약이 있고(인용 스니펫·``eval/gold.py`` 의 스팬 역매칭이
전부 여기에 기댄다), 접두사를 본문에 넣는 순간 그 계약이 깨져 골든셋 전체가
해석 불가가 된다. 그래서 접두사는 **임베딩 입력**(``embed_text``)에만 붙는다.
"""

import bisect
import re
from dataclasses import dataclass
from functools import lru_cache

from app.config import settings

# Fallback token proxy when the real tokenizer is unavailable: ~0.75 words/token.
_WORDS_PER_TOKEN = 0.75
_WORD_RE = re.compile(r"\S+")

# 헤딩 표기. 줄 맨 앞의 ``#``~``######`` + 공백 + 제목.
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*$", re.MULTILINE)

# 헤딩 경로 구분자. Notion W5 표가 적은 표기 그대로다.
HEADING_SEP = " > "

STRATEGY_FIXED = "fixed"
STRATEGY_SECTION = "section"
STRATEGIES = (STRATEGY_FIXED, STRATEGY_SECTION)


@dataclass
class Chunk:
    chunk_index: int
    page_from: int
    page_to: int
    content: str
    token_count: int
    # 이 청크를 감싸는 헤딩 경로. 비어 있는 것이 기본값이라, 접두사를 켜지
    # 않은 실행은 M1 때와 바이트 단위로 같은 청크를 만든다.
    heading_path: tuple[str, ...] = ()

    @property
    def embed_text(self) -> str:
        """What the embedder sees — ``content`` unless a heading path is attached."""
        if not self.heading_path:
            return self.content
        return f"{HEADING_SEP.join(self.heading_path)}\n{self.content}"


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


def _page_paths(
    text: str, base: tuple[str, ...]
) -> tuple[list[tuple[int, tuple[str, ...]]], tuple[str, ...]]:
    """Heading-path transitions inside one page, plus the path it ends on.

    ``base`` is the path the previous page ended on: a page that begins in the
    middle of a section inherits it. 쪽마다 경로를 0에서 다시 시작하면, 헤딩이
    앞 쪽에 있는 이어지는 쪽은 영영 경로를 못 갖는다 — 접두사가 가장 필요한
    쪽이 정확히 그 쪽이다.
    """
    transitions: list[tuple[int, tuple[str, ...]]] = [(0, base)]
    stack = list(base)
    for match in HEADING_RE.finditer(text):
        level = len(match.group(1))
        title = match.group(2).strip()
        del stack[level - 1 :]
        while len(stack) < level - 1:
            # 건너뛴 단계(예: # 다음에 바로 ###)는 빈 칸으로 채우지 않는다.
            # 경로에 빈 문자열이 끼면 "A >  > C" 같은 접두사가 나온다.
            stack.append(stack[-1] if stack else title)
        stack.append(title)
        transitions.append((match.start(), tuple(stack)))
    return transitions, tuple(stack)


def _path_at(transitions: list[tuple[int, tuple[str, ...]]], offset: int) -> tuple[str, ...]:
    """The heading path in effect at a character offset."""
    index = bisect.bisect_right([start for start, _ in transitions], offset) - 1
    return transitions[max(index, 0)][1]


def _sections(text: str, base: tuple[str, ...]) -> list[tuple[int, int, tuple[str, ...]]]:
    """(start, end, path) for each heading-delimited section of one page.

    A heading whose body is empty — ``## 학생`` immediately followed by
    ``### 목록 조회`` — does not become a section of its own. 그런 줄만 담긴
    청크는 검색에 아무 정보도 주지 않으면서 후보 한 자리를 차지한다. 대신 그
    줄을 다음 섹션의 시작으로 끌어와, 청크 본문이 여전히 원문의 연속된
    부분문자열이 되게 한다.
    """
    heads = list(HEADING_RE.finditer(text))
    if not heads:
        return [(0, len(text), base)]

    bounds = [match.start() for match in heads] + [len(text)]
    sections: list[tuple[int, int, tuple[str, ...]]] = []

    if text[: bounds[0]].strip():
        sections.append((0, bounds[0], base))

    stack = list(base)
    start = bounds[0]
    for i, match in enumerate(heads):
        level = len(match.group(1))
        title = match.group(2).strip()
        del stack[level - 1 :]
        while len(stack) < level - 1:
            stack.append(stack[-1] if stack else title)
        stack.append(title)

        end = bounds[i + 1]
        body = text[match.end() : end]
        if not body.strip():
            continue  # 제목만 있는 헤딩 — 다음 섹션에 붙인다(start 를 그대로 둔다)
        sections.append((start, end, tuple(stack)))
        start = end
    return sections


def _common_prefix(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    """The deepest heading path all of ``paths`` share."""
    if not paths:
        return ()
    out: list[str] = []
    for parts in zip(*paths):
        if len(set(parts)) != 1:
            break
        out.append(parts[0])
    return tuple(out)


def _pack(
    raw: str,
    sections: list[tuple[int, int, tuple[str, ...]]],
    budget: int,
) -> list[tuple[int, int, tuple[str, ...]]]:
    """Merge consecutive sections up to ``budget`` tokens, never across one.

    왜 합치나. 명세 문서의 ``#### 오류 코드`` 한 절은 150자짜리다. 그걸 그대로
    청크로 내보내면 섹션 청킹은 고정 청킹보다 **훨씬 작은** 청크를 만들게 되고,
    그러면 precision 이 올라가도 그게 "섹션 경계가 옳았다" 때문인지 "청크가
    작았다" 때문인지 구분할 수 없다. 같은 토큰 예산 안에서 자르되 경계만
    헤딩에 맞추는 것이 이 실험이 실제로 묻는 것이다.

    붙인 청크의 경로는 합쳐진 섹션들의 **공통 조상**이다 — 그것이 그 청크가
    실제로 속한 범위다.
    """
    packed: list[tuple[int, int, tuple[str, ...]]] = []
    lo = hi = None
    paths: list[tuple[str, ...]] = []
    tokens = 0
    for start, end, path in sections:
        count = len(_offsets(raw[start:end]))
        if lo is not None and start == hi and tokens + count <= budget:
            hi, tokens = end, tokens + count
            paths.append(path)
            continue
        if lo is not None:
            packed.append((lo, hi, _common_prefix(paths)))
        lo, hi, tokens, paths = start, end, count, [path]
    if lo is not None:
        packed.append((lo, hi, _common_prefix(paths)))
    return packed


def _window(
    raw: str,
    lo: int,
    hi: int,
    budget: int,
    step: int,
) -> list[tuple[str, int, int]]:
    """Token windows over ``raw[lo:hi]`` as (content, token_count, abs_start).

    ``abs_start`` is an offset into ``raw``, not into the slice: the heading
    path a window inherits depends on where it *starts*, and re-finding the
    text would land on the wrong copy whenever a document repeats a sentence —
    which a spec document does constantly.
    """
    piece = raw[lo:hi]
    spans = _offsets(piece)
    if not spans:
        return []
    out: list[tuple[str, int, int]] = []
    start = 0
    while start < len(spans):
        window = spans[start : start + budget]
        text = piece[window[0][0] : window[-1][1]]
        content = text.strip()
        if content:
            out.append((content, len(window), lo + window[0][0] + (len(text) - len(text.lstrip()))))
        if start + budget >= len(spans):
            break
        start += step
    return out


def chunk_pages(
    pages: list[str],
    chunk_size: int,
    chunk_overlap: int,
    *,
    strategy: str = STRATEGY_FIXED,
    heading_prefix: bool = False,
) -> list[Chunk]:
    """Split per-page text into chunks.

    Args:
        pages: page texts, index 0 == page 1.
        chunk_size: target chunk size in tokens.
        chunk_overlap: overlap between consecutive windows in tokens.
        strategy: ``"fixed"`` (M1 default — overlapping token windows) or
            ``"section"`` (cut at heading boundaries; a section longer than
            ``chunk_size`` is still windowed *inside* itself).
        heading_prefix: attach the enclosing heading path to each chunk, which
            ``Chunk.embed_text`` prepends for the embedder. ``content`` is
            untouched either way.

    Returns chunks in document order with a global ``chunk_index``. Each chunk
    stays within a single page, so ``page_from == page_to``. When the tokenizer
    is unavailable, ``chunk_size``/``chunk_overlap`` are interpreted against a
    word proxy instead of true tokens.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown chunk strategy {strategy!r}; choose from {STRATEGIES}")

    real_tokens = _tokenizer() is not None
    budget = chunk_size if real_tokens else _word_budget(chunk_size)
    ov = chunk_overlap if real_tokens else _word_budget(chunk_overlap)
    overlap = min(ov, budget - 1) if budget > 1 else 0
    step = max(1, budget - overlap)

    chunks: list[Chunk] = []
    index = 0
    # 헤딩 문맥은 쪽을 넘어 이어진다 — 섹션이 쪽 경계에서 끊겨도 경로는 남는다.
    carried: tuple[str, ...] = ()
    for page_no, raw in enumerate(pages, start=1):
        if not raw.strip():
            continue

        if strategy == STRATEGY_SECTION:
            spans = _pack(raw, _sections(raw, carried), budget)
        else:
            spans = [(0, len(raw), carried)]

        if heading_prefix or strategy == STRATEGY_SECTION:
            transitions, carried = _page_paths(raw, carried)
        else:
            transitions = [(0, ())]

        for lo, hi, path in spans:
            for content, tokens, offset in _window(raw, lo, hi, budget, step):
                if not heading_prefix:
                    attached = ()
                elif strategy == STRATEGY_SECTION:
                    attached = path
                else:
                    # fixed 전략의 창은 섹션 한가운데에서 시작할 수 있다. 그
                    # 창이 **실제로 시작하는 위치**의 경로를 붙여야, 헤딩을 한
                    # 글자도 담지 못한 청크가 자기가 어디 속하는지 말할 수 있다.
                    attached = _path_at(transitions, offset)
                chunks.append(
                    Chunk(
                        chunk_index=index,
                        page_from=page_no,
                        page_to=page_no,
                        content=content,
                        token_count=tokens,
                        heading_path=attached,
                    )
                )
                index += 1
    return chunks
