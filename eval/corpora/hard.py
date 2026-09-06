"""Discriminating corpus: 25 confusable fee clauses + 15 distinct-topic clauses.

The `simple` corpus saturated — every config scored 1.000 — because its clauses
shared no vocabulary, so dense retrieval was already perfect and there was no
headroom to measure.

Here, pages 1-25 are near-identical fee clauses differing only in article
number, amount, and billing day. Semantically they are almost the same
sentence, so a bi-encoder has little to separate them and exact tokens (digits,
article numbers) carry the signal — which is where lexical/sparse matching
should win. Pages 26-40 stay topically distinct so dense's genuine strength is
still measured by paraphrase queries.
"""

# (article number, monthly amount, billing day)
_FEES = [
    (1, 39000, 1), (2, 45000, 3), (3, 52000, 5), (4, 68000, 7), (5, 74000, 9),
    (6, 88000, 11), (7, 1250000, 13), (8, 96000, 15), (9, 105000, 17),
    (10, 118000, 19), (11, 127000, 21), (12, 134000, 23), (13, 146000, 25),
    (14, 159000, 27), (15, 163000, 2), (16, 178000, 4), (17, 184000, 6),
    (18, 195000, 8), (19, 206000, 10), (20, 213000, 12), (21, 228000, 14),
    (22, 234000, 16), (23, 247000, 18), (24, 256000, 20), (25, 269000, 22),
]

_CONFUSABLE = [
    f"제{n}조 (이용요금) 제{n}호 서비스의 월 이용요금은 금 {amount:,}원으로 하며, "
    f"매월 {day}일에 청구한다. 연체 시 지연이자를 가산한다."
    for n, amount, day in _FEES
]

_DISTINCT = [
    "제26조 (회원가입) 서비스는 만 14세 이상만 가입할 수 있으며, 가입 시 이메일 인증이 필요하다.",
    "제27조 (환불) 결제 후 7일 이내 서비스를 사용하지 않은 경우 전액 환불한다.",
    "제28조 (계약기간) 계약 기간은 2024년 3월 1일부터 2026년 2월 28일까지 총 2년으로 한다.",
    "제29조 (고객센터) 서비스 관련 문의는 고객센터 1588-2024로 연락한다.",
    "제30조 (계약번호) 본 계약의 계약번호는 KR-2024-8891 이다.",
    "제31조 (데이터 보관) 업로드된 문서는 90일간 보관된 후 자동으로 삭제된다.",
    "제32조 (보안) 모든 데이터는 AES-256 방식으로 암호화되어 저장된다.",
    "제33조 (책임의 한계) 회사는 천재지변 등 불가항력으로 인한 서비스 장애에 책임지지 않는다.",
    "제34조 (분쟁해결) 서비스 이용과 관련한 분쟁은 서울중앙지방법원을 관할 법원으로 한다.",
    "제35조 (준거법) 본 약관은 대한민국 법을 준거법으로 한다.",
    "제36조 (지식재산권) 서비스 화면과 소프트웨어에 대한 저작권은 회사에 귀속된다.",
    "제37조 (양도금지) 회원은 회사의 사전 승낙 없이 계약상 지위를 제3자에게 양도할 수 없다.",
    "제38조 (통지) 회사의 통지는 회원이 등록한 이메일 주소로 발송함으로써 효력이 발생한다.",
    "제39조 (서비스 중단) 정기 점검은 매주 일요일 새벽 2시부터 4시까지 진행된다.",
    "제40조 (가입 해지) 회원은 언제든지 마이페이지에서 이용 계약을 해지할 수 있다.",
]

CLAUSES = _CONFUSABLE + _DISTINCT

# "exact": the answer hinges on a literal token (amount / article number) inside
#   the confusable cluster, where dense has almost nothing else to go on.
# "semantic": paraphrase over a distinct-topic clause.
QUERIES = [
    ("월 이용요금이 1,250,000원인 서비스의 청구일은 며칠인가요?", 7, "exact"),
    ("이용요금 269,000원은 몇 일에 청구되나요?", 25, "exact"),
    ("월 요금 88,000원인 조항의 청구일을 알려줘", 6, "exact"),
    ("206,000원 요금은 매월 언제 청구되나요?", 19, "exact"),
    ("이용요금이 134,000원인 서비스는?", 12, "exact"),
    ("월 이용요금 178,000원 조항 내용", 16, "exact"),
    ("제7조의 이용요금은 얼마인가요?", 7, "exact"),
    ("제23조는 무슨 내용인가요?", 23, "exact"),
    ("제14조 이용요금과 청구일을 알려줘", 14, "exact"),
    ("제21조의 월 이용요금은?", 21, "exact"),
    ("제3조 내용을 알려줘", 3, "exact"),
    ("제18조의 청구일은 며칠인가요?", 18, "exact"),
    ("돈을 언제까지 돌려받을 수 있나요?", 27, "semantic"),
    ("몇 살부터 서비스에 가입할 수 있어?", 26, "semantic"),
    ("문서는 얼마 동안 보관되나요?", 31, "semantic"),
    ("분쟁이 생기면 어느 법원에서 다투나요?", 34, "semantic"),
    ("고객센터 전화번호 알려줘", 29, "semantic"),
    ("점검 때문에 서비스가 멈추는 시간은 언제인가요?", 39, "semantic"),
    ("계약을 그만두려면 어떻게 하나요?", 40, "semantic"),
    ("데이터는 어떻게 암호화되나요?", 32, "semantic"),
]
