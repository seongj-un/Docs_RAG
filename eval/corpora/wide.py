"""A corpus larger than CAND_K, so the candidate cut-off actually binds.

``CAND_K`` limits how many chunks each retriever hands to fusion, and fusion
hands to the reranker. Every other corpus here is *smaller* than the default
50 — 12, 36, 40 chunks — so all of them are always retrieved and the setting
does nothing. (The D19 note already flagged this: "코퍼스(40)가 CAND_K(50)보다
작아 융합 단계 영향은 미측정".) Sweeping CAND_K on those measures nothing.

This corpus is 260 pages: five families of near-identical clauses that differ
only in a number, plus distinct-topic clauses. Within a family a bi-encoder has
almost nothing to separate the pages, so the gold chunk has to *earn* a place in
the top-K of at least one retriever — which is exactly what CAND_K gates.

Amount ranges are disjoint per family and every exact query's answer token is
asserted unique across the corpus, so a question has exactly one right page.
"""

# --- families: near-identical within, disjoint amount ranges across ---

_FEES = [(n, 31000 + (n - 1) * 971, (n % 28) + 1) for n in range(1, 71)]
_FEE_CLAUSES = [
    f"제{n}조 (이용요금) 제{n}호 서비스의 월 이용요금은 금 {amount:,}원으로 하며, "
    f"매월 {day}일에 청구한다. 연체 시 지연이자를 가산한다."
    for n, amount, day in _FEES
]

_DEPOSITS = [(70 + i, 1100000 + (i - 1) * 37000) for i in range(1, 51)]
_DEPOSIT_CLAUSES = [
    f"제{n}조 (보증금) 제{n}호 계약의 보증금은 금 {amount:,}원으로 하며, "
    f"계약 종료 시 반환한다."
    for n, amount in _DEPOSITS
]

_PENALTIES = [(120 + i, i + 1) for i in range(1, 31)]
_PENALTY_CLAUSES = [
    f"제{n}조 (위약금) 제{n}호 계약을 중도 해지하는 경우 위약금은 "
    f"월 이용료의 {mult}배로 한다."
    for n, mult in _PENALTIES
]

_RETENTIONS = [(150 + i, 100 + i) for i in range(1, 41)]
_RETENTION_CLAUSES = [
    f"제{n}조 (보관기간) 제{n}호 자료는 {days}일간 보관된 후 자동으로 파기된다."
    for n, days in _RETENTIONS
]

_LIMITS = [(190 + i, 10000000 + (i - 1) * 730000) for i in range(1, 36)]
_LIMIT_CLAUSES = [
    f"제{n}조 (보장한도) 제{n}호 담보의 연간 보장 한도는 금 {amount:,}원으로 한다."
    for n, amount in _LIMITS
]

_DISTINCT = [
    "제226조 (회원가입) 서비스는 만 17세 이상만 가입할 수 있으며, 가입 시 휴대전화 인증이 필요하다.",
    "제227조 (환불) 결제 후 11일 이내 서비스를 사용하지 않은 경우 전액 환불한다.",
    "제228조 (고객센터) 서비스 관련 문의는 고객센터 1577-3092로 연락한다.",
    "제229조 (계약번호) 본 계약의 계약번호는 KR-2026-4417 이다.",
    "제230조 (보안) 모든 데이터는 ChaCha20-Poly1305 방식으로 암호화되어 저장된다.",
    "제231조 (분쟁해결) 서비스 이용과 관련한 분쟁은 대전지방법원을 관할 법원으로 한다.",
    "제232조 (준거법) 본 약관은 대한민국 법을 준거법으로 한다.",
    "제233조 (지식재산권) 서비스 화면과 소프트웨어에 대한 저작권은 회사에 귀속된다.",
    "제234조 (양도금지) 회원은 회사의 사전 승낙 없이 계약상 지위를 제3자에게 양도할 수 없다.",
    "제235조 (통지) 회사의 통지는 회원이 등록한 이메일 주소로 발송함으로써 효력이 발생한다.",
    "제236조 (서비스 중단) 정기 점검은 매주 목요일 새벽 1시부터 3시까지 진행된다.",
    "제237조 (가입 해지) 회원은 언제든지 마이페이지에서 이용 계약을 해지할 수 있다.",
    "제238조 (호출제한) API 호출은 분당 175회로 제한된다.",
    "제239조 (무료체험) 무료 이용 기간은 가입일로부터 23일로 한다.",
    "제240조 (반려동물) 체중 8kg 이하 소형견 1마리에 한하여 사전 신고 후 사육할 수 있다.",
    "제241조 (수선의무) 보일러 등 주요 설비의 수선은 임대인이, 전구 교체는 임차인이 부담한다.",
    "제242조 (전대금지) 임차인은 임대인의 서면 동의 없이 목적물을 제3자에게 전대할 수 없다.",
    "제243조 (계약갱신) 만료 3개월 전까지 통지가 없으면 동일 조건으로 1년 연장된다.",
    "제244조 (보장개시) 보장은 가입일로부터 45일이 경과한 날부터 개시된다.",
    "제245조 (면책사유) 음주 상태에서 발생한 사고는 보상 대상에서 제외된다.",
    "제246조 (청구기한) 보험금은 사고 발생일로부터 4년 이내에 청구하여야 한다.",
    "제247조 (통원한도) 통원 치료는 1회당 30만원, 연간 60회까지 보장한다.",
    "제248조 (해외치료) 해외에서 발생한 치료비는 국내 기준 금액의 60퍼센트를 지급한다.",
    "제249조 (원상회복) 임차인은 계약 종료 시 목적물을 원상회복한다. 통상적인 마모는 제외한다.",
    "제250조 (관리비) 월 관리비는 금 246,000원이며 매월 27일에 납부한다.",
    "제251조 (주차) 세대당 주차는 1대까지 무상으로 제공된다.",
    "제252조 (흡연) 건물 내 전 구역에서 흡연이 금지된다.",
    "제253조 (소음) 22시부터 06시까지 층간 소음을 유발하는 행위를 금지한다.",
    "제254조 (열쇠) 출입 카드 분실 시 재발급 수수료는 3만원이다.",
    "제255조 (하자보수) 인도일로부터 2년간 하자 보수를 보증한다.",
    "제256조 (양도세) 본 계약에 따른 제세공과금은 각자 부담한다.",
    "제257조 (비밀유지) 당사자는 계약 과정에서 알게 된 상대방의 영업비밀을 누설하지 않는다.",
    "제258조 (불가항력) 천재지변으로 인한 이행 지연은 채무불이행으로 보지 아니한다.",
    "제259조 (통합조항) 본 계약은 당사자 간 합의의 전부이며 이전 합의를 대체한다.",
    "제260조 (효력발생) 본 계약은 양 당사자가 서명한 날부터 효력이 발생한다.",
]

CLAUSES = (
    _FEE_CLAUSES + _DEPOSIT_CLAUSES + _PENALTY_CLAUSES
    + _RETENTION_CLAUSES + _LIMIT_CLAUSES + _DISTINCT
)

# (question, gold_page, type, unique answer token or None for semantic)
_SPEC = [
    ("월 이용요금이 61,101원인 서비스의 청구일은 며칠인가요?", 32, "exact", "61,101"),
    ("이용요금 96,057원은 며칠에 청구되나요?", 68, "exact", "96,057"),
    ("월 요금 41,681원인 조항의 청구일을 알려줘", 12, "exact", "41,681"),
    ("이용요금이 79,550원인 서비스는?", 51, "exact", "79,550"),
    ("제7조의 이용요금은 얼마인가요?", 7, "exact", "제7조"),
    ("제44조 이용요금과 청구일을 알려줘", 44, "exact", "제44조"),
    ("보증금이 2,136,000원인 계약은 몇 조인가요?", 99, "exact", "2,136,000"),
    ("보증금 2,358,000원 조항의 내용은?", 105, "exact", "2,358,000"),
    ("제88조의 보증금은 얼마인가요?", 88, "exact", "제88조"),
    ("제112조 보증금 액수를 알려줘", 112, "exact", "제112조"),
    ("위약금이 월 이용료의 18배인 조항은?", 137, "exact", "18배"),
    ("제129조의 위약금은 몇 배인가요?", 129, "exact", "제129조"),
    ("자료를 123일간 보관하는 조항은 몇 조인가요?", 173, "exact", "123일"),
    ("제160조의 보관 기간은 며칠인가요?", 160, "exact", "제160조"),
    ("보장 한도가 24,600,000원인 담보는?", 211, "exact", "24,600,000"),
    ("제199조의 연간 보장 한도를 알려줘", 199, "exact", "제199조"),
    ("돈을 언제까지 돌려받을 수 있나요?", 227, "semantic", None),
    ("몇 살부터 서비스에 가입할 수 있어?", 226, "semantic", None),
    ("분쟁이 생기면 어느 법원에서 다투나요?", 231, "semantic", None),
    ("고객센터 전화번호 알려줘", 228, "semantic", None),
    ("점검 때문에 서비스가 멈추는 시간은 언제인가요?", 236, "semantic", None),
    ("계약을 그만두려면 어떻게 하나요?", 237, "semantic", None),
    ("데이터는 어떻게 암호화되나요?", 230, "semantic", None),
    ("API는 1분에 몇 번까지 부를 수 있나요?", 238, "semantic", None),
    ("강아지를 키울 수 있나요?", 240, "semantic", None),
    ("보일러가 고장나면 누가 고치나요?", 241, "semantic", None),
    ("무료로 써볼 수 있는 기간은 며칠인가요?", 239, "semantic", None),
    ("보장은 언제부터 시작되나요?", 244, "semantic", None),
    ("음주운전 사고도 보상되나요?", 245, "semantic", None),
    ("밤에 시끄럽게 하면 안 되는 시간은?", 253, "semantic", None),
    # "vague": 구어체 의역이라 어휘가 거의 겹치지 않는다. sparse는 걸 것이 없고
    # dense는 260개 중에서 골라야 하므로, 두 채널 모두 정답을 상위로 못 올릴
    # 가능성이 있는 유일한 유형 — CAND_K의 한계선을 찌르는 것이 목적이다.
    ("카드를 잃어버렸는데 돈을 얼마나 내야 하나요?", 254, "vague", None),
    ("이사 나갈 때 벽지 낡은 것도 물어줘야 하나요?", 249, "vague", None),
    ("일하면서 알게 된 회사 정보를 밖에 말해도 되나요?", 257, "vague", None),
    ("태풍 때문에 늦어진 것도 잘못인가요?", 258, "vague", None),
    ("차를 몇 대나 댈 수 있나요?", 251, "vague", None),
    ("집에 문제가 생기면 언제까지 고쳐주나요?", 255, "vague", None),
    ("병원에 다니면서 치료받을 때 얼마까지 되나요?", 247, "vague", None),
    ("외국에서 아프면 어떻게 되나요?", 248, "vague", None),
]


def _check() -> list[tuple[str, int, str]]:
    """Assert each exact answer is unique and lands on the labeled page."""
    queries = []
    for question, gold, qtype, token in _SPEC:
        assert 1 <= gold <= len(CLAUSES), f"gold page {gold} out of range"
        if token is not None:
            hits = [i + 1 for i, c in enumerate(CLAUSES) if token in c]
            assert hits == [gold], (
                f"{token!r} appears on {hits}, expected only page {gold}"
            )
        queries.append((question, gold, qtype))
    return queries


QUERIES = _check()
