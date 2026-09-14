"""Request/response models for the M1 API surface."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SignupRequest(BaseModel):
    email: EmailStr
    # argon2 handles long passwords; the floor is the only real requirement.
    password: str = Field(..., min_length=8, max_length=256)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=256)


class VerifyRequest(BaseModel):
    # 링크에서 온 토큰. GET 이 아니라 본문으로 받는 이유는 메일 스캐너가
    # 링크를 미리 밟아 1회용 토큰을 태우기 때문이다.
    token: str = Field(..., min_length=1, max_length=512)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    # User.email_verified 프로퍼티에서 읽어온다.
    email_verified: bool = False
    created_at: datetime | None = None


class DocumentCreated(BaseModel):
    """202 response after an upload is accepted for background indexing."""

    id: uuid.UUID
    filename: str
    status: str


class DocumentStatus(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    num_pages: int | None = None
    status: str
    error: str | None = None
    created_at: datetime | None = None


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1)
    document_id: uuid.UUID | None = None
    # Per-request override of HYBRID_ENABLED (None -> use the server default).
    # Lets callers A/B hybrid+rerank vs M1 dense-only on the same corpus.
    hybrid: bool | None = None


class Citation(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    page_from: int | None = None
    page_to: int | None = None
    snippet: str


class QueryResponse(BaseModel):
    answer: str
    refused: bool
    citations: list[Citation]


class ChunkOut(BaseModel):
    """One chunk's full text, for the evidence modal.

    ``Citation.snippet`` is truncated at 240 characters for display inside an
    answer; the modal shows the chunk the answer was actually grounded in, so
    it needs the untruncated text. The document's name is not included — the
    caller already holds the document list and joins on ``document_id``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    chunk_index: int
    page_from: int | None = None
    page_to: int | None = None
    content: str


class UsageOut(BaseModel):
    """Current consumption against the configured quotas.

    Both halves are returned so the caller can phrase the remainder itself
    ("82쪽 남았습니다") instead of receiving a percentage it cannot explain.

    미인증 계정은 다른 한도를 **다른 창**으로 센다(수명 전체 누적). 숫자만
    바꿔서는 안 되는 이유가 여기 있다 — 화면의 "내일 다시 채워집니다"가
    거짓이 된다. ``email_verified`` 를 함께 주어 문구까지 바꾸게 한다.
    """

    queries_today: int
    queries_per_day: int
    pages_this_month: int
    pages_per_month: int

    email_verified: bool
    queries_total: int
    documents_total: int
    unverified_query_limit: int
    unverified_document_limit: int
    # 미인증 계정은 문서 개수와 쪽수 **양쪽**에 막힌다. 쪽수를 내려보내지
    # 않으면 "문서 0/1"인 화면에서 60쪽짜리가 거절당하고, 사용자는 남았다고
    # 적힌 한도에 왜 막혔는지 알 길이 없다.
    pages_uploaded_total: int
    unverified_page_limit: int


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    scope_document_id: uuid.UUID | None = None


class ConversationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None = None
    scope_document_id: uuid.UUID | None = None
    created_at: datetime | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    citations: list[Citation] | None = None
    refused: bool
    # Which scope the turn was asked under; NULL means every document.
    scope_document_id: uuid.UUID | None = None
    created_at: datetime | None = None


class ConversationDetail(ConversationSummary):
    messages: list[MessageOut] = []


class ConversationQuery(BaseModel):
    question: str = Field(..., min_length=1)
    # Overrides the conversation's stored scope for this turn only.
    document_id: uuid.UUID | None = None
    hybrid: bool | None = None


class TraceSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question: str
    document_id: uuid.UUID | None = None
    hybrid: bool
    cached: bool
    refused: bool
    llm_model: str | None = None
    tokens_in: int
    tokens_out: int
    embed_ms: int | None = None
    retrieve_ms: int | None = None
    rerank_ms: int | None = None
    generate_ms: int | None = None
    total_ms: int | None = None
    # 실패로 끝난 질의에만 채워진다(NULL = 실패하지 않음). 실패한 질의도
    # 이제 traces 에 남으므로, 이 둘이 없으면 목록에서 실패한 행이 "답이
    # 빈 성공"과 구분되지 않는다 — 없느니만 못한 기록이 된다. 소유자에게만
    # 보이고, 내용도 이미 그 사람이 응답으로 받아 본 detail 이거나 예외
    # 클래스 이름이다(메시지는 저장하지 않는다). 실패 사유를 주인에게
    # 그대로 보여주는 것은 documents.error 가 이미 하고 있는 일이다.
    status_code: int | None = None
    error: str | None = None
    created_at: datetime | None = None


class TraceChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chunk_id: uuid.UUID
    stage: str
    rank: int
    score: float | None = None
    page_from: int | None = None


class TraceDetail(TraceSummary):
    answer: str | None = None
    chunks: list[TraceChunkOut] = []


class Percentiles(BaseModel):
    """Median and tail for one stage. Absent when nothing was recorded."""

    p50: int | None = None
    p95: int | None = None


class QueryStats(BaseModel):
    """Queries in the window. ``total`` counts every admitted request.

    That includes the ones that ended in an error, and it has to: while only
    successes were recorded, an embedding outage made this number *fall*, so
    the hour the operator was paging through looked like a quiet one.
    ``failed`` is the subset that did not answer — ``total - failed`` is what
    used to be reported as ``total``.
    """

    total: int
    cached: int
    refused: int
    failed: int


class DocumentStats(BaseModel):
    """How the corpus stands **right now** — deliberately not windowed.

    This is a gauge, not a count of events: narrowing it to ``window_hours``
    would answer "documents uploaded in the last hour", so a healthy corpus
    queried with ``hours=1`` would report ``ready: 0``.
    """

    ready: int = 0
    processing: int = 0
    pending: int = 0
    failed: int = 0


class FailureReason(BaseModel):
    """A failure class and how often it happened.

    Grouped by the error's leading token rather than the whole message, since
    the tail carries file names and ids that would split one cause into many.
    """

    reason: str
    count: int


class AdminStats(BaseModel):
    window_hours: int
    queries: QueryStats
    # Latency of what users actually waited for, cache hits included.
    total_ms: Percentiles
    # Where the time went, cache hits excluded — a hit skips embed/rerank and
    # generation entirely, so mixing the two makes every stage look fast.
    stages: dict[str, Percentiles]
    tokens_in: int
    tokens_out: int
    documents: DocumentStats
    # Indexing failures, in the window. The name stays as it was — nothing
    # consumes it but the README — while the query-side list gets a qualified
    # one, because the unqualified word is the older field's.
    failures: list[FailureReason]
    # Query failures, grouped as "<status> <reason>". Separate from the list
    # above rather than merged into it: an indexing failure is a document a
    # user has to re-upload, a query failure is a request that is already
    # gone, and an operator reading one number for both cannot tell which of
    # the two is happening.
    query_failures: list[FailureReason]
