"""Golden evaluation set: 3 documents, 36 labeled questions.

Hand-authored rather than LLM-generated, per the M4 decision. Three documents
from different domains so retrieval has to discriminate across a corpus, not
just within one file — a question about 보증금 must not pull the insurance
document's 보장한도, which uses the same amount on purpose.

Each question carries a ``reference`` answer, which is what Context Recall
needs: the metric asks whether the retrieved context contains the facts the
reference states. ``gold_pages`` additionally allows plain retrieval metrics
(R@k / MRR) on the same set.

Question types:
- ``factual``      stated directly in one clause
- ``numeric``      hinges on an exact amount, date, or count
- ``multi_hop``    needs two clauses combined
- ``unanswerable`` deliberately absent; the correct answer is a refusal
"""

# --- documents: one clause per page, page = index + 1 ---

LEASE = [
    "제1조 (목적물) 임대차 목적물은 서울특별시 강남구 테헤란로 123, 5층 501호로 한다.",
    "제2조 (보증금) 임대차 보증금은 금 오천만원(50,000,000원)으로 한다.",
    "제3조 (차임) 월 차임은 금 삼백만원(3,000,000원)이며 매월 5일에 선불로 지급한다.",
    "제4조 (계약기간) 계약 기간은 2024년 3월 1일부터 2026년 2월 28일까지 2년으로 한다.",
    "제5조 (관리비) 월 관리비는 금 십오만원(150,000원)이며 매월 25일에 납부한다.",
    "제6조 (중도해지) 중도 해지 시 3개월 전 서면으로 통보하여야 하며, 위약금은 월 차임의 2배로 한다.",
    "제7조 (원상회복) 임차인은 계약 종료 시 목적물을 원상회복한다. 다만 통상적인 마모는 제외한다.",
    "제8조 (수선의무) 보일러 등 주요 설비의 수선은 임대인이, 전구 등 소모품 교체는 임차인이 부담한다.",
    "제9조 (전대금지) 임차인은 임대인의 서면 동의 없이 목적물을 제3자에게 전대할 수 없다.",
    "제10조 (반려동물) 체중 10kg 이하 소형견 1마리에 한하여 사전 신고 후 사육할 수 있다.",
    "제11조 (계약갱신) 만료 2개월 전까지 어느 일방의 통지가 없으면 동일 조건으로 1년 연장된다.",
    "제12조 (관할법원) 본 계약에 관한 분쟁은 서울중앙지방법원을 관할 법원으로 한다.",
]

INSURANCE = [
    "제1조 (보장개시) 보장은 가입일로부터 90일이 경과한 날부터 개시된다.",
    "제2조 (자기부담금) 보험금 지급 시 자기부담금은 금 삼십만원(300,000원)으로 한다.",
    "제3조 (연간보장한도) 연간 보장 한도는 금 오천만원(50,000,000원)으로 한다.",
    "제4조 (보험료) 월 보험료는 금 사만오천원(45,000원)이며 매월 10일에 자동이체된다.",
    "제5조 (면책사유) 피보험자의 고의, 전쟁, 음주운전으로 인한 손해는 보상하지 않는다.",
    "제6조 (청구기한) 보험금 청구권은 사고일로부터 3년간 행사하지 않으면 소멸한다.",
    "제7조 (갱신) 본 계약은 5년 주기로 자동 갱신된다.",
    "제8조 (해지환급금) 중도 해지 시 납입보험료의 70%를 환급한다.",
    "제9조 (치과 대기기간) 치과 치료는 가입 후 180일이 경과해야 보장된다.",
    "제10조 (통원한도) 통원 치료는 1일 20만원, 연 30회를 한도로 보상한다.",
    "제11조 (입원한도) 입원 치료는 1일 50만원, 연 180일을 한도로 보상한다.",
    "제12조 (고지의무) 계약자는 청약서 기재사항을 사실대로 고지하여야 한다.",
]

SAAS = [
    "제1조 (서비스) 회사는 문서 업로드 및 자연어 질의응답 서비스를 제공한다.",
    "제2조 (가입연령) 서비스는 만 14세 이상만 가입할 수 있다.",
    "제3조 (요금제) 베이직 요금제는 월 9,900원, 프로 요금제는 월 29,900원이다.",
    "제4조 (무료체험) 신규 가입자는 14일간 무료로 서비스를 이용할 수 있다.",
    "제5조 (환불) 결제 후 7일 이내 서비스를 사용하지 않은 경우 전액 환불한다.",
    "제6조 (데이터 보관) 업로드된 문서는 90일간 보관된 후 자동으로 삭제된다.",
    "제7조 (보안) 모든 데이터는 AES-256 방식으로 암호화되어 저장된다.",
    "제8조 (정기점검) 정기 점검은 매주 일요일 02시부터 04시까지 진행된다.",
    "제9조 (API 한도) API 호출은 분당 60회로 제한된다.",
    "제10조 (계정 해지) 회원은 마이페이지에서 즉시 이용 계약을 해지할 수 있다.",
    "제11조 (지식재산권) 서비스 화면과 소프트웨어의 저작권은 회사에 귀속된다.",
    "제12조 (준거법) 본 약관은 대한민국 법을 준거법으로 한다.",
]

DOCUMENTS: dict[str, list[str]] = {
    "lease": LEASE,
    "insurance": INSURANCE,
    "saas": SAAS,
}

# (question, document, gold_pages, reference, type)
QUESTIONS: list[dict] = [
    # --- lease ---
    {"q": "임대차 보증금은 얼마인가요?", "doc": "lease", "gold_pages": [2],
     "reference": "임대차 보증금은 오천만원(50,000,000원)이다.", "type": "numeric"},
    {"q": "월세는 얼마이고 언제 내나요?", "doc": "lease", "gold_pages": [3],
     "reference": "월 차임은 삼백만원(3,000,000원)이며 매월 5일에 선불로 지급한다.",
     "type": "numeric"},
    {"q": "계약 기간이 어떻게 되나요?", "doc": "lease", "gold_pages": [4],
     "reference": "2024년 3월 1일부터 2026년 2월 28일까지 2년이다.", "type": "factual"},
    {"q": "관리비 납부일은 며칠인가요?", "doc": "lease", "gold_pages": [5],
     "reference": "월 관리비 십오만원(150,000원)을 매월 25일에 납부한다.", "type": "numeric"},
    {"q": "중도 해지하면 위약금이 얼마인가요?", "doc": "lease", "gold_pages": [6, 3],
     "reference": "위약금은 월 차임의 2배이며, 월 차임이 삼백만원이므로 육백만원이다.",
     "type": "multi_hop"},
    {"q": "보일러가 고장나면 누가 고치나요?", "doc": "lease", "gold_pages": [8],
     "reference": "보일러 등 주요 설비의 수선은 임대인이 부담한다.", "type": "factual"},
    {"q": "강아지를 키울 수 있나요?", "doc": "lease", "gold_pages": [10],
     "reference": "체중 10kg 이하 소형견 1마리에 한해 사전 신고 후 사육할 수 있다.",
     "type": "factual"},
    {"q": "다른 사람에게 세를 놓을 수 있나요?", "doc": "lease", "gold_pages": [9],
     "reference": "임대인의 서면 동의 없이는 제3자에게 전대할 수 없다.", "type": "factual"},
    {"q": "아무 통지도 안 하면 계약은 어떻게 되나요?", "doc": "lease", "gold_pages": [11],
     "reference": "만료 2개월 전까지 통지가 없으면 동일 조건으로 1년 연장된다.",
     "type": "factual"},
    {"q": "분쟁이 생기면 어느 법원으로 가나요?", "doc": "lease", "gold_pages": [12],
     "reference": "서울중앙지방법원을 관할 법원으로 한다.", "type": "factual"},
    {"q": "보증금과 월세를 합치면 첫 달에 얼마가 필요한가요?", "doc": "lease",
     "gold_pages": [2, 3], "reference":
     "보증금 오천만원과 월 차임 삼백만원을 합쳐 오천삼백만원이 필요하다.",
     "type": "multi_hop"},
    {"q": "주차 공간은 몇 대까지 제공되나요?", "doc": "lease", "gold_pages": [],
     "reference": "문서에 주차에 관한 조항이 없다.", "type": "unanswerable"},

    # --- insurance ---
    {"q": "자기부담금은 얼마인가요?", "doc": "insurance", "gold_pages": [2],
     "reference": "자기부담금은 삼십만원(300,000원)이다.", "type": "numeric"},
    {"q": "보장은 언제부터 시작되나요?", "doc": "insurance", "gold_pages": [1],
     "reference": "가입일로부터 90일이 경과한 날부터 보장이 개시된다.", "type": "factual"},
    {"q": "월 보험료와 이체일을 알려주세요.", "doc": "insurance", "gold_pages": [4],
     "reference": "월 보험료는 사만오천원(45,000원)이며 매월 10일에 자동이체된다.",
     "type": "numeric"},
    {"q": "음주운전 사고도 보상되나요?", "doc": "insurance", "gold_pages": [5],
     "reference": "고의, 전쟁, 음주운전으로 인한 손해는 보상하지 않는다.", "type": "factual"},
    {"q": "보험금은 언제까지 청구해야 하나요?", "doc": "insurance", "gold_pages": [6],
     "reference": "사고일로부터 3년 안에 청구해야 하며 그 후에는 소멸한다.", "type": "factual"},
    {"q": "중도 해지하면 얼마를 돌려받나요?", "doc": "insurance", "gold_pages": [8],
     "reference": "납입보험료의 70%를 환급한다.", "type": "numeric"},
    {"q": "치과 치료는 언제부터 보장되나요?", "doc": "insurance", "gold_pages": [9],
     "reference": "가입 후 180일이 경과해야 치과 치료가 보장된다.", "type": "numeric"},
    {"q": "통원 치료 한도가 어떻게 되나요?", "doc": "insurance", "gold_pages": [10],
     "reference": "통원은 1일 20만원, 연 30회 한도로 보상한다.", "type": "numeric"},
    {"q": "입원과 통원 중 하루 한도가 더 큰 쪽은?", "doc": "insurance",
     "gold_pages": [10, 11],
     "reference": "입원이 1일 50만원으로 통원 1일 20만원보다 한도가 크다.",
     "type": "multi_hop"},
    {"q": "1년에 최대 얼마까지 보장받나요?", "doc": "insurance", "gold_pages": [3],
     "reference": "연간 보장 한도는 오천만원(50,000,000원)이다.", "type": "numeric"},
    {"q": "계약은 몇 년마다 갱신되나요?", "doc": "insurance", "gold_pages": [7],
     "reference": "5년 주기로 자동 갱신된다.", "type": "numeric"},
    {"q": "해외에서 치료받아도 보장되나요?", "doc": "insurance", "gold_pages": [],
     "reference": "문서에 해외 치료에 관한 조항이 없다.", "type": "unanswerable"},

    # --- saas ---
    {"q": "프로 요금제는 월 얼마인가요?", "doc": "saas", "gold_pages": [3],
     "reference": "프로 요금제는 월 29,900원이다.", "type": "numeric"},
    {"q": "무료체험 기간은 며칠인가요?", "doc": "saas", "gold_pages": [4],
     "reference": "신규 가입자는 14일간 무료로 이용할 수 있다.", "type": "numeric"},
    {"q": "환불 조건이 어떻게 되나요?", "doc": "saas", "gold_pages": [5],
     "reference": "결제 후 7일 이내 서비스를 사용하지 않은 경우 전액 환불한다.",
     "type": "factual"},
    {"q": "업로드한 문서는 얼마나 보관되나요?", "doc": "saas", "gold_pages": [6],
     "reference": "업로드된 문서는 90일간 보관된 후 자동 삭제된다.", "type": "numeric"},
    {"q": "데이터는 어떻게 암호화되나요?", "doc": "saas", "gold_pages": [7],
     "reference": "모든 데이터는 AES-256 방식으로 암호화되어 저장된다.", "type": "factual"},
    {"q": "점검 때문에 서비스가 멈추는 시간은?", "doc": "saas", "gold_pages": [8],
     "reference": "매주 일요일 02시부터 04시까지 정기 점검이 진행된다.", "type": "factual"},
    {"q": "API를 1분에 몇 번 부를 수 있나요?", "doc": "saas", "gold_pages": [9],
     "reference": "API 호출은 분당 60회로 제한된다.", "type": "numeric"},
    {"q": "몇 살부터 가입할 수 있나요?", "doc": "saas", "gold_pages": [2],
     "reference": "만 14세 이상만 가입할 수 있다.", "type": "factual"},
    {"q": "계약을 그만두려면 어떻게 하나요?", "doc": "saas", "gold_pages": [10],
     "reference": "마이페이지에서 즉시 이용 계약을 해지할 수 있다.", "type": "factual"},
    {"q": "베이직에서 프로로 바꾸면 월에 얼마를 더 내나요?", "doc": "saas",
     "gold_pages": [3],
     "reference": "프로 29,900원과 베이직 9,900원의 차이인 20,000원을 더 낸다.",
     "type": "multi_hop"},
    {"q": "무료체험 기간에도 문서 보관 기간은 동일한가요?", "doc": "saas",
     "gold_pages": [4, 6],
     "reference": "무료체험은 14일이고 문서 보관은 90일로, 보관 기간에 대한 별도 예외는 없다.",
     "type": "multi_hop"},
    {"q": "팀 단위 협업 기능이 있나요?", "doc": "saas", "gold_pages": [],
     "reference": "문서에 팀 협업 기능에 관한 조항이 없다.", "type": "unanswerable"},
]


def stats() -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in QUESTIONS:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    return counts
