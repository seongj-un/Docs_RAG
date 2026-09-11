"""ORM models for M1: documents and their embedded chunks.

Schema is intentionally shaped for later milestones:
- ``content`` keeps the raw chunk text so M2 can add tsvector/GIN for BM25.
- ``embed_model`` is recorded so a model swap can identify what to re-index.
- ``user_id`` already exists so M3 tenant isolation is a filter, not a migration.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import SPARSEVEC, Vector
from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import settings


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL 이면 미인증. boolean 이 아니라 시각인 이유는 "인증했나"보다
    # "가입 후 얼마 만에 인증했나"가 어뷰즈 조사에서 실제로 쓰이기 때문이다.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    @property
    def email_verified(self) -> bool:
        """UserOut(from_attributes=True) 이 그대로 읽어간다."""
        return self.email_verified_at is not None


class Session(Base):
    """Server-side session. The cookie carries this row's id."""

    __tablename__ = "sessions"
    __table_args__ = (Index("sessions_user_id_idx", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class EmailVerificationToken(Base):
    """1회용 이메일 인증 토큰. 저장하는 것은 sha256 해시이고 원문은 링크에만.

    argon2 를 쓰지 않는 이유는 토큰이 사용자가 고른 비밀번호가 아니라 256비트
    난수라서다. 사전 공격 대상이 아니므로 느린 해시가 방어하는 것이 없고,
    반대로 솔트 때문에 인덱스 조회가 불가능해져 검증마다 전체를 훑게 된다.

    소비된 행은 지우지 않는다. 남겨야 두 번째 클릭에 "만료됐다"가 아니라
    "이미 인증하셨다"고 말할 수 있다.
    """

    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        Index("email_verification_tokens_user_id_idx", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (Index("documents_user_id_idx", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # M3: owner. Every read path filters on this — see services/retrieve.py.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    num_pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # pending | processing | ready | failed
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_doc_index"),
        Index("chunks_document_id_idx", "document_id"),
        Index(
            "chunks_embedding_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embed_model: Mapped[str] = mapped_column(String, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.embed_dim), nullable=False
    )
    # M2: BGE-M3 lexical weights for hybrid (sparse) retrieval. Nullable so M1
    # rows survive until backfilled; new rows are written with both vectors.
    sparse_embedding: Mapped[object | None] = mapped_column(
        SPARSEVEC(settings.embed_sparse_dim), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped["Document"] = relationship(back_populates="chunks")


class UsageEvent(Base):
    """One billable action. Quotas are aggregated from these rows, so they hold
    across processes and restarts (unlike the in-process rate limiter)."""

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("usage_events_user_created_idx", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # query | ingest
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Pages added by an ingest event (drives the monthly upload-page quota) or
    # an upload event (drives the unverified taster page quota) — the two
    # kinds are summed separately, each filtering on its own `kind`.
    pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # True when a query was served from the semantic cache (no LLM spend).
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # NULL means this row is still a reservation: a slot claimed by
    # services/usage.py's reserve() before the slow work behind it
    # (embedding/rerank/LLM) has run, so it is not yet known whether that
    # work will finish, fail, or never get the chance to (the process dies).
    # record() and commit_reservation() both set this the moment a row's
    # values are final. A NULL row still counts toward quotas — that is what
    # makes a reservation visible to a concurrent request before the work
    # behind it completes — but one older than
    # settings.reservation_ttl_seconds is what acquire_quota_lock's sweep
    # treats as abandoned and deletes.
    settled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class Trace(Base):
    """One query request, recorded for diagnosis.

    Traces are a diagnostic log, so they deliberately outlive what they point
    at: ``document_id`` and ``TraceChunk.chunk_id`` carry no foreign key, and a
    deleted document leaves its history intact. They do cascade from the user,
    since a deleted account should take its query history with it.
    """

    __tablename__ = "traces"
    __table_args__ = (Index("traces_user_created_idx", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    hybrid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    refused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Per-stage latency, so a slow request says *which* stage was slow.
    embed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retrieve_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rerank_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generate_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    chunks: Mapped[list["TraceChunk"]] = relationship(
        back_populates="trace", cascade="all, delete-orphan"
    )


class TraceChunk(Base):
    """A chunk as it appeared at one retrieval stage of one trace.

    Keeping every stage (not just the final context) is what makes the RAG
    Triad diagnosable: if the gold chunk is in ``dense`` but not ``rerank``,
    the reranker dropped it; if it is in no stage at all, retrieval never found
    it and the LLM was never the problem.
    """

    __tablename__ = "trace_chunks"
    __table_args__ = (
        Index("trace_chunks_trace_stage_idx", "trace_id", "stage", "rank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("traces.id", ondelete="CASCADE"), nullable=False
    )
    # No FK: the chunk may be deleted later, but the trace must stay readable.
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)  # dense|sparse|rrf|rerank
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    page_from: Mapped[int | None] = mapped_column(Integer, nullable=True)

    trace: Mapped["Trace"] = relationship(back_populates="chunks")


class Conversation(Base):
    """A chat thread. Groups messages so the sidebar can list past sessions.

    ``scope_document_id`` remembers which document the thread was scoped to.
    It is SET NULL rather than CASCADE on document deletion: the conversation
    is the user's record of what they asked, and losing the whole thread
    because its document was removed would destroy that history.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        Index("conversations_user_created_idx", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    """One turn in a conversation.

    ``citations`` stores the footnote mapping (number -> chunk id, page) as
    written at answer time, so reopening an old thread shows the same evidence
    even after the underlying chunks change or the document is deleted.
    """

    __tablename__ = "messages"
    __table_args__ = (
        Index("messages_conversation_idx", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    refused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The scope this turn was asked under. NULL means every document. No FK:
    # a record of what was asked must stay readable after the document is
    # deleted — same reasoning as ``Trace.document_id``. Without it, a thread
    # reopened later cannot say whether a refusal was scoped or corpus-wide.
    scope_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class QueryCache(Base):
    """Semantic cache: a past answer keyed by the question's embedding.

    Scoped to (user, document) so a hit can never cross tenants and never
    answers from a different document than the caller asked about.
    """

    __tablename__ = "query_cache"
    __table_args__ = (
        Index("query_cache_user_doc_idx", "user_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # NULL means the question was asked across all of the user's documents.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=True,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    question_embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.embed_dim), nullable=False
    )
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    refused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    citations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
