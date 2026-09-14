"""M7 W6: comparing where the answer is written — server, or calling agent.

**No model is called from this file.** Everything here is a pure function over
values, exactly as ``eval/l2.py`` is, so the citation rule, the refusal
detector, the cost accounting and the aggregation can be tested without a GPU,
a network or an API key. The runner that spends money is ``eval/w6_run.py``.

Notion W6's table, restated as the two conditions this module scores:

    A 서버 생성   ``answer_question``     서버 안의 Gemini 가 답을 쓴다.
                                          인용 형식·거부 가드레일을 서버가 통제.
    B 컨텍스트만  ``search_documents``    청크만 건네고 호출한 에이전트가 쓴다.
                                          대화 맥락을 쓰고 서버 생성 비용 0.

and the five axes it asks for: **L2 품질 · 인용 정확도 · 거부 정확도 · 질의당
비용 · 지연**.

--------------------------------------------------------------------------
인용 정확도를 두 모드에서 공정하게 비교하는 법
--------------------------------------------------------------------------

이것이 이 파일의 가장 어려운 결정이다. 모드 A 는 구조화된 ``citations``
(chunk_id · document_id · 페이지 범위)를 **기계적으로** 만든다 — 답을 쓴 청크가
곧 인용이므로 틀릴 수가 없다. 모드 B 에는 그런 것이 아예 없다. 에이전트가
산문 안에 자기 말로 적을 뿐이다. 그래서 "구조화된 인용이 있는가"로 재면 A 가
100%, B 가 0% 이고, 그 표는 측정이 아니라 **정의를 다시 쓴 것**이다.

그래서 세 갈래로 나눈다.

1. **구조적 가용성은 점수가 아니라 성질로 보고한다.** A=있음, B=없음.
   측정한 것이 아니라 설계에서 따라 나오는 사실이고(``STRUCTURED_CITATIONS``),
   Notion 이 "인용을 통제 못 함"이라고 적은 것의 실체가 정확히 이 줄이다.
   이것을 점수 칸에 넣으면 나머지 네 축이 이 한 칸에 가려진다.

2. **점수는 두 모드에서 똑같이 — 답변 텍스트만 보고 — 잰다.** 사용자에게
   실제로 닿는 것은 산문이고, 산문에 적힌 페이지 표기는 두 모드 모두 만든다
   (A 는 ``[p.N]`` 를 쓰라는 생성 프롬프트가, B 는 페이지 범위를 인용하라는
   툴 description 이 시킨다). 같은 추출기를 같은 규칙으로 양쪽에 돌린다.

3. **각 모드는 자기 생성기가 실제로 본 증거에 대해 채점한다.** A 의 생성기가
   본 것은 접지선을 넘은 청크들(=그대로 citations)이고, B 의 생성기가 본 것은
   ``search_documents`` 가 돌려준 히트들이다. 보지도 않은 페이지를 인용했다면
   그것은 양쪽 모두에서 **지어낸 인용**이다. 이 기준이라야 "더 많은 청크를
   받은 쪽이 불리해지는" 편향이 생기지 않는다.

그래서 점수 칸은 셋이다 — ``cited`` (인용을 하기는 했는가),
``attributable`` (인용한 페이지 중 실제로 본 것의 비율), ``gold_hit``
(인용한 페이지에 정답 페이지가 들어 있는가). 앞의 둘은 golden 라벨 없이도
나오고, 셋째는 ``gold_spans`` 를 코퍼스 본문에서 페이지로 되짚어 얻는다.

**A 에는 칸이 하나 더 있다.** 에이전트가 A 의 답을 그대로 전하지 않고 자기
말로 다시 쓰면 서버가 건 가드레일은 거기서 끝난다. 그래서 A 는 서버가 쓴
원문과 에이전트가 내보낸 최종 텍스트를 **둘 다** 채점한다. 둘의 차이가 곧
"서버 생성으로 인용을 통제할 수 있다"는 주장이 실제로 사용자에게 닿는지의
답이다. B 에는 대응하는 칸이 없다 — 거기서는 원문이 곧 최종 텍스트다.

--------------------------------------------------------------------------
거부 정확도를 judge 없이 재는 법
--------------------------------------------------------------------------

W6 노트가 결정 기준으로 지목한 축이고, 다행히 이 축은 **judge 가 필요 없다**.
라벨이 ``no_answer`` 인지(데이터셋이 아는 사실)와 답변이 거부인지(텍스트에서
읽히는 사실)만 있으면 된다. L2 품질이 미검증 judge 의 의견인 것과 달리 이
수치는 그렇지 않으므로, 결론은 이쪽을 더 무겁게 읽어야 한다.

다만 "답변이 거부인지"는 B 에서 구조화 플래그로 주어지지 않는다(A 에만
``refused`` 가 있다). 그래서 텍스트 탐지기를 쓰되, **그 탐지기 자체를 A 의
구조화 플래그로 검증한다** — 같은 44문항에서 탐지기와 서버 플래그가 얼마나
일치하는지가 ``detector_agreement()`` 이고, 그 수치 없이 B 의 거부율을
인용하면 안 된다.
"""

import re
import statistics
from dataclasses import asdict, dataclass, field

# --- 두 모드 --------------------------------------------------------------
#
# 값은 ``app/mcp/variants.py`` 의 변형 이름과 **같아야 한다**. 같은 문자열이
# L2Record.variant 와 traces 조회의 조건과 이 리포트의 열 이름에 동시에 쓰이고,
# 셋 중 하나만 다르면 조인이 조용히 비어 버린다.
MODE_SERVER = "server_answer"
MODE_CLIENT = "client_answer"
MODES = (MODE_SERVER, MODE_CLIENT)

# 어느 모드가 어느 trace source 를 남기는지. 리포트가 사라져도 이 매핑과
# ``traces.source`` 만 있으면 같은 비교를 SQL 로 되살릴 수 있다.
TRACE_SOURCE_OF = {MODE_SERVER: "mcp_answer", MODE_CLIENT: "mcp_search"}

# 구조화된 인용의 유무. **측정값이 아니라 설계에서 따라 나오는 사실**이므로
# 점수 칸이 아니라 성질로 보고한다 — 모듈 docstring 의 1번.
STRUCTURED_CITATIONS = {MODE_SERVER: True, MODE_CLIENT: False}
STRUCTURED_CITATIONS_WHY = (
    "모드 A 는 답을 쓴 청크가 곧 citations 이라 구조적으로 틀릴 수 없다. "
    "모드 B 에는 그런 필드가 존재하지 않는다 — 에이전트가 산문에 자기 말로 "
    "적을 뿐이다. 이 차이는 재서 나온 것이 아니라 두 아키텍처의 정의이고, "
    "Notion 이 '인용·거부를 통제 못 함'이라고 적은 것의 실체다."
)


# --- 페이지 인용 추출 ------------------------------------------------------
#
# 두 모드에 **같은 추출기**를 돌린다. 한쪽에만 맞춘 추출기는 그 한쪽의 표기
# 습관을 점수로 바꾼다. 그래서 우리 생성 프롬프트가 시키는 `[p.N]` 만 보지
# 않고, 사람이 쓰는 한국어 표기와 영어 표기를 함께 받는다.
#
# 숫자만으로는 절대 잡지 않는 것이 중요하다. 이 코퍼스는 오류 코드(E4012)와
# HTTP 상태(403)와 분당 한도가 본문에 가득해서, 맨 숫자를 페이지로 읽으면
# "인용을 아주 많이 한" 답변이 대량으로 생긴다.
_PAGE_TOKEN = re.compile(
    r"""
    (?:
        p\.?\s*                     # p.12 / p 12 / [p.12]
      | pages?\s+                   # page 12 / pages 12-13
      | 페이지\s*                    # 페이지 12
    )
    (?P<num>\d{1,4})
    (?:\s*[-~–]\s*(?P<to>\d{1,4}))?  # 범위
    """,
    re.IGNORECASE | re.VERBOSE,
)
# 한국어는 접미사 쪽이 흔하다: "12쪽", "12-13쪽", "12페이지".
_PAGE_SUFFIX = re.compile(
    r"(?P<num>\d{1,4})(?:\s*[-~–]\s*(?P<to>\d{1,4}))?\s*(?:쪽|페이지|면)"
)

# 한 인용이 펼칠 수 있는 페이지 수의 상한. "p.1-400" 같은 표기 하나가 문서
# 전체를 인용한 것으로 집계되면 attributable 이 의미를 잃는다.
MAX_RANGE = 20


def extract_pages(text: str) -> set[int]:
    """Page numbers the answer text claims as sources.

    범위(``p.3-4``)는 펼친다. 0 이나 음수는 페이지가 아니므로 버린다 — 우리
    페이지 번호는 1-based 다(``eval/pdf.py``).
    """
    pages: set[int] = set()
    for pattern in (_PAGE_TOKEN, _PAGE_SUFFIX):
        for match in pattern.finditer(text or ""):
            start = int(match.group("num"))
            raw_end = match.group("to")
            end = int(raw_end) if raw_end else start
            if start < 1:
                continue  # 1-based 다. 0 은 페이지가 아니다.
            if end < start or end - start >= MAX_RANGE:
                # 뒤집혔거나 지나치게 넓은 범위는 범위로 읽지 않고 시작 쪽만
                # 인정한다. "p.1-400" 하나를 문서 전체 인용으로 집계하면
                # attributable 이 의미를 잃는다.
                pages.add(start)
                continue
            pages.update(range(start, end + 1))
    return pages


def pages_of(spans: list[dict]) -> set[int]:
    """Expand ``page_from``/``page_to`` records into the page numbers they cover.

    검색 히트와 인용이 같은 두 필드를 쓰므로 양쪽에 그대로 쓰인다.
    """
    out: set[int] = set()
    for span in spans or []:
        start = span.get("page_from")
        end = span.get("page_to") if span.get("page_to") is not None else start
        if start is None:
            continue
        if end is None or end < start or end - start >= MAX_RANGE:
            out.add(int(start))
            continue
        out.update(range(int(start), int(end) + 1))
    return out


def resolve_gold_pages(gold_spans: list[dict], documents: dict[str, list[str]]) -> set[int]:
    """Turn the dataset's ``{doc, snippet}`` gold labels into page numbers.

    골든셋은 페이지가 아니라 **스니펫**을 적는다(청크 id 가 인덱싱마다 바뀌는
    것과 같은 이유). 코퍼스 본문에서 그 스니펫이 실린 쪽을 되짚으면 페이지가
    나오고, 그것이 인용 정확도의 정답 집합이다.

    한 스니펫이 여러 쪽에 걸리면 **전부** 넣는다. 그 경우 어느 쪽을 인용해도
    맞는 것이 사실이기 때문이다 — 하나를 임의로 고르면 맞은 답을 틀렸다고
    세게 된다. (코퍼스가 18곳에 똑같이 실린 문장을 gold 로 쓰지 못하게 한
    이유도 같다 — ``eval/corpora/spec.py``.)
    """
    pages: set[int] = set()
    for span in gold_spans or []:
        doc = span.get("doc")
        snippet = (span.get("snippet") or "").strip()
        if not snippet or doc not in documents:
            continue
        for index, page_text in enumerate(documents[doc], start=1):
            if snippet in page_text:
                pages.add(index)
    return pages


# --- 거부 탐지 -------------------------------------------------------------
#
# 두 모드에 **같은 탐지기**를 돌린다. A 의 구조화 ``refused`` 를 A 에만 쓰면
# 두 모드가 다른 자로 재게 되고, 그 표에서 나온 차이는 아키텍처가 아니라 자의
# 차이다. A 의 플래그는 대신 **이 탐지기를 검증하는 데** 쓴다
# (``detector_agreement``).

# 출처를 가리키는 말. 거부는 "무언가가 없다"가 아니라 "**이 문서에** 없다"이다.
_SOURCE_WORDS = (
    "문서", "자료", "컨텍스트", "context", "제공된", "검색", "passage", "document",
)
# 없다는 말. 전부 **여러 어절짜리 구**다. 맨 "없다"/"없습니다" 를 넣지 않은
# 것이 결정적이다 — 이 코퍼스는 "문서에 따르면 권한이 없습니다" 같은 정상
# 답변으로 가득해서, 짧은 부정어 하나면 같은 문장 안의 출처 단어와 짝지어져
# 정답을 전부 거부로 집계한다.
_NEGATIONS = (
    "찾을 수 없", "찾지 못", "확인할 수 없", "알 수 없",
    "답변할 수 없", "답할 수 없", "나와 있지 않", "나와있지 않",
    "포함되어 있지 않", "포함돼 있지 않", "포함하고 있지 않",
    "언급되어 있지 않", "언급되지 않", "언급이 없",
    "명시되어 있지 않", "명시되지 않",
    "정보가 없", "내용이 없", "설명이 없",
    "기재되어 있지 않", "기재되지 않", "다루고 있지 않",
    "do not contain", "does not contain", "not found", "no information",
)
# 문장 경계. 마침표·물음표·느낌표·줄바꿈 — 한국어 답변은 개조식 줄바꿈이 잦다.
# ``p`` 나 숫자 뒤의 마침표는 경계가 아니다. 이것을 빼먹으면 우리 인용 표기
# ``[p.2, p.5]`` 하나가 한 문장을 셋으로 쪼개고, "선두 문장" 규칙이 엉뚱한
# 조각을 선두로 읽는다. 실측에서 실제로 그렇게 깨졌다.
_SENTENCE = re.compile(r"(?<![p0-9])[.!?]+\s*|\n+")

# 선두로 볼 문장 수. 1 이다 — 아래 docstring 참조.
LEADING_SENTENCES = 1
# 이보다 짧은 조각은 문장으로 세지 않는다(공백 제거 후). 개조식 답변의
# 제목 줄("### 결론", "- 요약") 때문이다 — 그런 조각이 선두를 차지하면
# 선두 문장 규칙이 아무것도 판정하지 못한다.
MIN_SENTENCE_CHARS = 8


def _is_refusal_sentence(sentence: str) -> bool:
    """출처를 가리키는 말과 없다는 말이 **한 문장 안에** 함께 있는가."""
    folded = _squash(sentence)
    if not folded:
        return False
    return any(_squash(w) in folded for w in _SOURCE_WORDS) and any(
        _squash(n) in folded for n in _NEGATIONS
    )


def _squash(text: str) -> str:
    """Fold spacing so '찾을 수 없' 와 '찾을수없' 이 같은 것으로 읽힌다."""
    return re.sub(r"\s+", "", text or "").lower()


def looks_refused(text: str) -> bool:
    """Whether this answer declines to answer from the documents.

    규칙은 두 줄이다. **선두 문장이 거부이면 거부다.** 아니면, **실질적인
    문장이 전부 거부이면** 거부다.

    왜 "어딘가에 거부 문장이 있으면 거부"가 아닌가 — 실측으로 그 규칙이
    깨졌기 때문이다. 이 데이터셋의 질문은 "권한이나 중복 때문에 실패하면"처럼
    **둘을 묻는데 문서에는 하나만 있는** 모양이 많고, 그러면 두 모드 모두
    이렇게 답한다:

        강의 단건 조회 시 권한 문제로 실패하면 E4052 가 내려갑니다 [p.5].
        제공된 문서에는 '중복'으로 인한 오류 코드는 명시되어 있지 않습니다.

    이것은 **답을 한 것**이다. 둘째 문장만 보고 거부로 세면 답을 잘한 문항이
    거부로 집계되고, 두 모드의 거부율이 나란히 부풀어 축 자체가 무의미해진다.
    스모크 실행 2문항에서 실제로 50%가 그렇게 잘못 잡혔다.

    선두 문장이 결정권을 갖는 이유는 두 모드가 모두 결론을 먼저 쓰기
    때문이다 — 우리 생성 프롬프트는 거부를 한 문장으로만 쓰게 하고
    (``services/generate.py`` 의 REFUSAL_TEXT), 에이전트 쪽도 한국어 QA 관례
    대로 판정을 앞에 놓는다. 둘째 규칙("전부 거부")이 있는 이유는 개조식
    답변의 선두가 제목 한 줄("### 결론")일 수 있어서다. 길이로 가르지 않는
    것이 중요하다 — 길이 문턱은 이 데이터셋의 답변 길이에 맞춰 고른 숫자가
    되고, 그런 숫자는 다른 코퍼스에서 조용히 틀린다.

    남는 한계: 결론을 뒤에 놓는 긴 거부는 놓친다. 그 오차는 거부율을 **낮게**
    잡는 방향이고, 두 모드에 똑같이 작용한다. 실제로 얼마나 틀리는지는
    ``detector_agreement`` 가 모드 A 의 구조화 플래그로 재고, 그 수치 없이
    이 축을 인용하면 안 된다.
    """
    sentences = [
        s for s in _SENTENCE.split(text or "")
        if len(_squash(s)) >= MIN_SENTENCE_CHARS
    ]
    if not sentences:
        return False
    if any(_is_refusal_sentence(s) for s in sentences[:LEADING_SENTENCES]):
        return True
    return all(_is_refusal_sentence(s) for s in sentences)


def detector_agreement(rows: list[dict]) -> dict:
    """How often ``looks_refused`` matches mode A's structured ``refused``.

    탐지기를 쓰기 전에 탐지기를 재는 자리다. 서버가 찍은 플래그가 참값이고,
    같은 텍스트에 탐지기를 돌려 맞춰 본다. 이 수치를 함께 싣지 않은 거부율은
    인용하면 안 된다 — B 의 거부율은 전적으로 이 탐지기의 값이기 때문이다.
    """
    pairs = [
        (bool(r["server_refused"]), looks_refused(r.get("server_answer") or ""))
        for r in rows
        if r.get("mode") == MODE_SERVER and r.get("server_refused") is not None
    ]
    if not pairs:
        return {"n": 0, "agreement": None, "false_positive": 0, "false_negative": 0}
    agree = sum(1 for truth, guess in pairs if truth == guess)
    return {
        "n": len(pairs),
        "agreement": agree / len(pairs),
        # 참값은 답했는데 탐지기가 거부라고 읽은 건수 (위 docstring 의 편향 방향).
        "false_positive": sum(1 for truth, guess in pairs if guess and not truth),
        "false_negative": sum(1 for truth, guess in pairs if truth and not guess),
    }


# --- 한 문항 · 한 모드의 채점 ---------------------------------------------

# 이 데이터셋에서 "거부가 정답"인 유형. 데이터셋이 아는 사실이므로 judge 가
# 필요 없다 — W6 가 결정 기준으로 지목한 축이 judge 없이 서는 이유다.
NO_ANSWER_TYPE = "no_answer"


@dataclass
class CitationScore:
    """One answer's citations, scored against what its generator actually saw.

    ``None`` 은 0 이 아니라 **잴 것이 없음**이다(``eval/l2.py`` 의 NOT_APPLICABLE
    과 같은 판단). 거부한 답변은 인용할 것이 없으므로 인용 정확도의 분모에
    들어가면 안 된다 — 거부를 잘한 문항이 인용 점수를 깎는 것은 틀린 집계다.
    """

    cited: set[int] = field(default_factory=set)
    seen: set[int] = field(default_factory=set)
    gold: set[int] = field(default_factory=set)
    scored: bool = True
    has_citation: bool | None = None
    attributable: float | None = None
    gold_hit: bool | None = None

    def to_dict(self) -> dict:
        return {
            "cited": sorted(self.cited),
            "seen": sorted(self.seen),
            "gold": sorted(self.gold),
            "scored": self.scored,
            "has_citation": self.has_citation,
            "attributable": self.attributable,
            "gold_hit": self.gold_hit,
        }


def score_citations(
    answer: str,
    *,
    seen_pages: set[int],
    gold_pages: set[int],
    refused: bool,
) -> CitationScore:
    """Score one answer's page citations. 규칙은 모듈 docstring 에 있다."""
    if refused:
        # 거부는 인용하지 않는 것이 정답이다. 점수가 아니라 해당 없음.
        return CitationScore(seen=set(seen_pages), gold=set(gold_pages), scored=False)

    cited = extract_pages(answer)
    score = CitationScore(
        cited=cited, seen=set(seen_pages), gold=set(gold_pages), scored=True
    )
    score.has_citation = bool(cited)
    if cited:
        # 본 적 없는 페이지를 인용했으면 지어낸 것이다. 양쪽 모드에 같은 규칙.
        score.attributable = len(cited & seen_pages) / len(cited)
    # gold 가 없는 문항(no_answer 등)에는 맞출 페이지가 없다 — 0 이 아니라 없음.
    if gold_pages:
        score.gold_hit = bool(cited & gold_pages)
    return score


@dataclass
class Cost:
    """Tokens spent on one question, split by who paid.

    Notion 의 축은 "질의당 비용"이고, 모드 B 의 함정은 **서버 비용이 0 이라고
    해서 비용이 0 이 아니라는 것**이다. 청크가 에이전트의 컨텍스트로 들어가고
    그 토큰은 호출한 쪽이 문다. 그래서 양쪽을 따로 세고 합계도 함께 낸다 —
    한쪽만 보면 어느 모드든 원하는 결론을 만들 수 있다.
    """

    server_in: int = 0
    server_out: int = 0
    client_in: int = 0
    client_out: int = 0

    @property
    def server_total(self) -> int:
        return self.server_in + self.server_out

    @property
    def client_total(self) -> int:
        return self.client_in + self.client_out

    @property
    def total(self) -> int:
        return self.server_total + self.client_total

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "server_total": self.server_total,
            "client_total": self.client_total,
            "total": self.total,
        }


# --- 집계 ------------------------------------------------------------------


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def refusal_metrics(rows: list[dict]) -> dict:
    """거부 정확도. judge 가 필요 없는 유일한 품질 축이다.

    두 수치가 방향이 반대라 둘 다 내야 한다:

    ``refusal_recall``      no_answer 문항에서 실제로 거부한 비율. 높을수록 좋다.
                            W6 가 "여기가 무너지면 A 를 병행 제공"이라고 지목한
                            바로 그 칸이다.
    ``false_refusal_rate``  답할 수 있는 문항에서 거부해 버린 비율. 낮을수록
                            좋다. 이 칸이 없으면 "무조건 거부"가 만점이 된다.
    """
    no_answer = [r for r in rows if r["type"] == NO_ANSWER_TYPE]
    answerable = [r for r in rows if r["type"] != NO_ANSWER_TYPE]
    refused_no_answer = sum(1 for r in no_answer if r["refused"])
    refused_answerable = sum(1 for r in answerable if r["refused"])
    return {
        "no_answer_n": len(no_answer),
        "refused_on_no_answer": refused_no_answer,
        "refusal_recall": (
            refused_no_answer / len(no_answer) if no_answer else None
        ),
        "answerable_n": len(answerable),
        "false_refusals": refused_answerable,
        "false_refusal_rate": (
            refused_answerable / len(answerable) if answerable else None
        ),
        "wrongly_answered_ids": [
            r["question_id"] for r in no_answer if not r["refused"]
        ],
        "wrongly_refused_ids": [
            r["question_id"] for r in answerable if r["refused"]
        ],
    }


def citation_metrics(rows: list[dict], key: str = "citation") -> dict:
    """Aggregate the three citation columns, each over its own denominator.

    분모가 칸마다 다른 것이 의도다. 거부한 문항은 인용 채점에서 빠지고
    (``scored=False``), gold 가 없는 문항은 gold_hit 에서 빠진다. 하나의 분모로
    접으면 "거부를 잘해서 인용 점수가 낮은" 모드가 생긴다.
    """
    scored = [r[key] for r in rows if r.get(key) and r[key]["scored"]]
    cited = [c for c in scored if c["has_citation"]]
    gold_scored = [c for c in scored if c["gold_hit"] is not None]
    return {
        "scored_n": len(scored),
        "cited_n": len(cited),
        "citation_rate": len(cited) / len(scored) if scored else None,
        "attributable": _mean([c["attributable"] for c in cited]),
        "gold_n": len(gold_scored),
        "gold_hit_rate": (
            sum(1 for c in gold_scored if c["gold_hit"]) / len(gold_scored)
            if gold_scored
            else None
        ),
    }


def cost_metrics(rows: list[dict]) -> dict:
    server = [r["cost"]["server_total"] for r in rows]
    client = [r["cost"]["client_total"] for r in rows]
    total = [r["cost"]["total"] for r in rows]
    return {
        "n": len(rows),
        "server_tokens_mean": _mean(server),
        "client_tokens_mean": _mean(client),
        "total_tokens_mean": _mean(total),
        "server_tokens_sum": sum(server),
        "client_tokens_sum": sum(client),
        "total_tokens_sum": sum(total),
    }


def latency_metrics(rows: list[dict]) -> dict:
    """Wall-clock, with the pacing sleep taken out.

    ``throttle_ms`` 는 우리가 무료 티어를 넘지 않으려고 **일부러 잔 시간**이다.
    그것까지 지연이라고 부르면 두 모드의 지연 차이는 요청 수의 차이가 되고,
    설계의 차이가 아니게 된다. 그래서 러너가 잰 잠든 시간을 빼고 센다.

    ``server_ms`` 는 MCP 툴 호출에 걸린 시간 — 모드 A 에서는 검색+생성,
    모드 B 에서는 검색만이다. ``agent_ms`` 는 그 밖의 시간, 즉 호출자 쪽 모델이
    쓴 시간이다. 이 맥에서 다른 작업이 돌지 않았고 모델 서버(8099)는 두 모드가
    똑같이 쓰므로, 두 모드의 차이는 대조가 성립한다.
    """
    server = [r["latency"]["server_ms"] for r in rows]
    agent = [r["latency"]["agent_ms"] for r in rows]
    total = [r["latency"]["total_ms"] for r in rows]
    return {
        "n": len(rows),
        "server_ms_median": _median(server),
        "server_ms_mean": _mean(server),
        "agent_ms_median": _median(agent),
        "agent_ms_mean": _mean(agent),
        "total_ms_median": _median(total),
        "total_ms_mean": _mean(total),
    }


def rescore(rows: list[dict]) -> list[dict]:
    """Recompute every derived score from the raw evidence each row still carries.

    한 행에는 **증거**(답변 텍스트·본 페이지·gold 페이지)와 **판정**(거부 여부·
    인용 점수)이 함께 저장된다. 증거는 돈을 주고 산 것이라 다시 만들 수 없지만
    판정은 순수 함수라 언제든 다시 낼 수 있다. 그래서 리포트는 저장된 판정을
    믿지 않고 여기서 다시 계산한다.

    이유는 실제로 겪은 일이다 — 첫 스모크 실행 뒤 거부 탐지기의 규칙을 고쳤는데,
    저장된 ``refused`` 는 옛 규칙의 값이라 표의 거부 칸과 탐지기 검증 칸이 서로
    다른 규칙을 말하고 있었다. 탐지기를 고칠 때마다 44문항 × 2모드를 다시 사야
    한다면 아무도 고치지 않을 것이고, 고치지 않은 탐지기가 결론을 쓰게 된다.

    행을 제자리에서 바꾸지 않고 복사본을 돌려준다. 원본 jsonl 은 지불의 기록이고,
    그것을 재계산 결과로 덮어쓰면 다음 재계산의 출발점이 증거가 아니라 판정이 된다.
    """
    out: list[dict] = []
    for row in rows:
        fresh = dict(row)
        seen = set(row.get("seen_pages") or [])
        gold = set(row.get("gold_pages") or [])
        fresh["refused"] = looks_refused(row.get("final_text") or "")
        fresh["citation"] = score_citations(
            row.get("final_text") or "",
            seen_pages=seen,
            gold_pages=gold,
            refused=fresh["refused"],
        ).to_dict()
        if row.get("mode") == MODE_SERVER and row.get("server_answer") is not None:
            # 서버 원문의 거부는 탐지기가 아니라 **서버가 찍은 플래그**다.
            # 그 칸의 존재 이유가 "구조화된 판정이 있으면 이렇게 된다"이므로,
            # 여기까지 탐지기로 덮으면 비교 대상이 사라진다.
            fresh["citation_server_text"] = score_citations(
                row["server_answer"],
                seen_pages=seen,
                gold_pages=gold,
                refused=bool(row.get("server_refused")),
            ).to_dict()
        out.append(fresh)
    return out


def by_mode(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {mode: [] for mode in MODES}
    for row in rows:
        out.setdefault(row["mode"], []).append(row)
    return out


def summarize(rows: list[dict]) -> dict:
    """Everything the report table needs, per mode. L2 는 여기 없다.

    L2 는 judge 를 부른 결과이고 이 파일은 모델을 부르지 않는다. 리포트가 두
    출처를 합칠 뿐, 미검증 judge 의 수치가 이 순수 함수들 사이에 섞여 앉아
    같은 신뢰도를 가진 것처럼 보이면 안 된다.
    """
    grouped = by_mode(rows)
    return {
        mode: {
            "n": len(items),
            "refusal": refusal_metrics(items),
            "citation": citation_metrics(items, "citation"),
            # A 에만 있는 칸. B 에서는 서버 원문이라는 것이 존재하지 않는다.
            "citation_server_text": (
                citation_metrics(items, "citation_server_text")
                if mode == MODE_SERVER
                else None
            ),
            "cost": cost_metrics(items),
            "latency": latency_metrics(items),
            "structured_citations": STRUCTURED_CITATIONS.get(mode),
            "trace_source": TRACE_SOURCE_OF.get(mode),
        }
        for mode, items in grouped.items()
        if items
    }
