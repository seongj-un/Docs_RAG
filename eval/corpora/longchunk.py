"""Long clauses where the answering fact sits at a controlled depth.

Every other corpus here puts one short clause on a page — 42 to 77 characters,
all of them shorter than the smallest truncation setting worth testing. They
therefore cannot measure what truncating reranker input costs: the numbers
would be identical at every setting and the "no regression" would be an
artifact of the fixture, not a finding.

Real chunks are up to CHUNK_SIZE (700) tokens. This corpus builds pages near
that size and varies one thing only — **where in the chunk the fact lives**:

    front   within the first 128 characters
    mid     around 400 characters in
    back    past 800 characters in

Boilerplate filler sits in front of the fact, so a truncated candidate keeps
its topic but loses its distinguishing detail. That is the failure mode
``RERANK_MAX_CHARS`` risks, and query type in the report reads directly as
depth: if truncation is safe, all three rows hold; if it is not, ``back``
falls first and ``front`` never moves.

Amounts and periods are deliberately confusable across pages, so a reranker
that cannot see the fact has no other way to pick the right page.
"""

# Generic terms-of-service sentences. None of them answers any query — they
# exist to push the fact deeper into the chunk while keeping pages plausible.
_FILLER = [
    "회사는 서비스의 안정적인 제공을 위하여 최선을 다하며 관련 법령을 준수한다.",
    "이용자는 본 약관 및 관계 법령을 준수하여야 하며 회사의 업무를 방해하여서는 아니 된다.",
    "본 조에서 정하지 아니한 사항은 관계 법령 및 일반적인 상관례에 따른다.",
    "회사는 서비스의 내용을 변경할 경우 그 사유와 내용을 사전에 공지한다.",
    "이용자가 등록한 정보에 변경이 있는 경우 지체 없이 이를 갱신하여야 한다.",
    "회사는 이용자의 귀책사유로 발생한 손해에 대하여 책임을 지지 아니한다.",
    "본 약관의 해석에 관하여 다툼이 있는 경우 당사자는 성실히 협의한다.",
    "회사는 천재지변 등 불가항력으로 인한 서비스 중단에 책임을 지지 아니한다.",
    "이용자는 자신의 계정 정보를 제3자에게 양도하거나 대여할 수 없다.",
    "회사는 관계 법령이 정하는 바에 따라 이용자의 개인정보를 보호한다.",
    "본 약관에 동의함으로써 이용자는 서비스 이용 자격을 취득한다.",
    "회사는 서비스 개선을 위하여 이용 현황에 관한 통계를 작성할 수 있다.",
    "이용자는 서비스를 통하여 얻은 정보를 회사의 동의 없이 영리 목적으로 이용할 수 없다.",
    "회사는 이용자가 제기한 의견이 정당하다고 인정할 경우 이를 신속히 처리한다.",
    "본 약관은 공지한 날로부터 효력이 발생하며 이용자에게 개별 통지하지 아니한다.",
]

# (fact sentence, question, depth) — one page and one query each.
_FACTS = [
    ("제1조 (보증금) 서비스 보증금은 금 이백사십만원(2,400,000원)으로 한다.",
     "서비스 보증금은 얼마인가요?", "front"),
    ("제2조 (위약금) 중도 해지 시 위약금은 월 이용료의 3배로 한다.",
     "중도 해지하면 위약금이 얼마인가요?", "front"),
    ("제3조 (무료체험) 무료 이용 기간은 가입일로부터 21일로 한다.",
     "무료로 써볼 수 있는 기간은 며칠인가요?", "front"),
    ("제4조 (보관기간) 업로드된 문서는 180일간 보관된 후 자동 삭제된다.",
     "올린 문서는 며칠이나 보관되나요?", "front"),
    ("제5조 (환불) 환불은 결제일로부터 14일 이내에 한하여 신청할 수 있다.",
     "환불은 며칠 안에 신청해야 하나요?", "mid"),
    ("제6조 (호출한도) API 호출은 분당 90회로 제한된다.",
     "API는 1분에 몇 번까지 부를 수 있나요?", "mid"),
    ("제7조 (정기점검) 정기 점검은 매주 화요일 03시부터 05시까지 진행된다.",
     "정기 점검은 무슨 요일 몇 시에 하나요?", "mid"),
    ("제8조 (가입연령) 만 16세 이상인 자에 한하여 가입할 수 있다.",
     "몇 살부터 가입할 수 있나요?", "mid"),
    ("제9조 (해지통보) 해지를 원하는 경우 2개월 전까지 서면으로 통보하여야 한다.",
     "해지하려면 얼마나 전에 알려야 하나요?", "back"),
    ("제10조 (관할법원) 본 계약에 관한 분쟁은 부산지방법원을 관할 법원으로 한다.",
     "분쟁이 생기면 어느 법원으로 가나요?", "back"),
    ("제11조 (암호화) 모든 데이터는 AES-256 방식으로 암호화되어 저장된다.",
     "데이터는 어떤 방식으로 암호화되나요?", "back"),
    ("제12조 (갱신주기) 본 계약은 18개월마다 자동으로 갱신된다.",
     "계약은 몇 개월마다 갱신되나요?", "back"),
]

_DEPTH_TARGET = {"front": 0, "mid": 400, "back": 800}
_PAGE_CHARS = 1000


def _filler_run(start: int, min_chars: int) -> str:
    """Filler sentences totalling at least ``min_chars``, varied per page."""
    out: list[str] = []
    total = 0
    i = start
    while total < min_chars:
        sentence = _FILLER[i % len(_FILLER)]
        out.append(sentence)
        total += len(sentence) + 1
        i += 1
    return " ".join(out)


def _build() -> tuple[list[str], list[tuple[str, int, str]], list[int]]:
    clauses: list[str] = []
    queries: list[tuple[str, int, str]] = []
    offsets: list[int] = []

    for index, (fact, question, depth) in enumerate(_FACTS):
        lead = _filler_run(index, _DEPTH_TARGET[depth]) if _DEPTH_TARGET[depth] else ""
        head = f"{lead} " if lead else ""
        offset = len(head)
        tail_needed = max(0, _PAGE_CHARS - offset - len(fact))
        tail = _filler_run(index + 7, tail_needed) if tail_needed else ""
        clause = f"{head}{fact} {tail}".strip()

        # The fixture's whole point is where the fact sits; assert it rather
        # than trust it, so an edit to the filler cannot silently flatten the
        # experiment into "every fact is near the front".
        assert clause.index(fact) == offset, f"page {index + 1} offset drift"
        if depth == "front":
            assert offset < 128, f"page {index + 1} front fact at {offset}"
        else:
            assert offset >= _DEPTH_TARGET[depth], (
                f"page {index + 1} {depth} fact at {offset}"
            )

        clauses.append(clause)
        queries.append((question, index + 1, depth))
        offsets.append(offset)

    return clauses, queries, offsets


CLAUSES, QUERIES, FACT_OFFSETS = _build()
