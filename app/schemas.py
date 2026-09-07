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


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
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
    """

    queries_today: int
    queries_per_day: int
    pages_this_month: int
    pages_per_month: int


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
