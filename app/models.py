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
    # 어느 소비자가 남긴 행인가. 값과 판단 근거는 services/tracing.py 의
    # TRACE_SOURCES, 백필·CHECK 제약은 마이그레이션 0011 참조. 파이썬 기본값을
    # 주지 않는 것이 요점이다 — 새 소비자가 빠뜨리면 조용히 잘못된 소스로
    # 기록되는 대신 터져야 한다(QueryRunner 가 필수 인자로 받는 것과 같은 이유).
    source: Mapped[str] = mapped_column(Text, nullable=False)
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


class EvalRun(Base):
    """One execution of the M7 L1 harness over one dataset and one config.

    이 표의 목적은 "지금 점수가 몇 점인가"가 아니라 **"지난번보다 나빠졌는가"**
    다. 그래서 지표만 남기지 않고 그 지표를 만든 조건 — git 커밋, 데이터셋
    이름과 해시, 구성 이름, cutoff — 을 같이 남긴다. 조건이 다른 두 실행을
    비교하면 검색이 좋아졌는지 질문이 쉬워졌는지 구분할 수 없고, 회귀 경보는
    그 순간부터 거짓말이 된다.

    ``user_id`` 가 없다. 평가는 사용자의 데이터가 아니라 저장소의 기록이고,
    픽스처는 seed 계정 소유로 인덱싱된다(``app/constants.py``). 계정이 지워져도
    지표 이력은 남아야 한다 — 이건 ``traces`` 와 반대 방향의 결정이다.
    """

    __tablename__ = "eval_runs"
    __table_args__ = (
        Index("eval_runs_dataset_config_idx", "dataset_name", "config", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dataset_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 데이터셋 파일 바이트의 sha256. diff 가 이 값이 다르면 비교를 거부한다.
    dataset_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[str] = mapped_column(Text, nullable=False)  # dense|hybrid|hybrid+rerank
    # 이 실행이 어느 커밋의 코드였는지. dirty 작업 트리면 "<sha>-dirty".
    git_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    k: Mapped[int] = mapped_column(Integer, nullable=False)
    num_questions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # L1 에서 실제로 채점된 문항 수. no_answer 문항은 정답 청크가 없어
    # 여기서 빠진다 — 전체 문항 수와 갈라지는 것이 정상이고, 두 수를 같이
    # 남겨야 "왜 36문항인데 33개만 쟀나"에 답할 수 있다.
    num_scored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 유형별·split별 집계. 전체 평균만 보면 multi_doc 실패가 single_fact
    # 성공에 가려진다(W1).
    metrics_by_type: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    metrics_by_split: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    results: Mapped[list["EvalResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class EvalResult(Base):
    """One question's outcome inside one run.

    집계만 저장하면 "recall@5 가 0.02 떨어졌다"까지만 말할 수 있다. 어떤
    질문이 뒤집혔는지를 말하려면 문항 단위가 남아야 하고, 그게 diff 리포트가
    실제로 쓸모 있어지는 지점이다.

    ``gold_chunk_ids`` 는 실행 시점에 스니펫을 해석한 결과다. 외래키를 걸지
    않는다 — 다음 인덱싱이 그 청크를 지우고 새로 만들어도 이 기록은 그대로
    읽혀야 한다(``trace_chunks.chunk_id`` 와 같은 이유).
    """

    __tablename__ = "eval_results"
    __table_args__ = (
        Index("eval_results_run_question_idx", "run_id", "question_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[str] = mapped_column(Text, nullable=False)
    split: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL 이면 상위 순위 어디에도 정답 청크가 없었다는 뜻. 0 이 아니라 NULL
    # 인 이유는 "1위"와 "못 찾음"이 같은 정수로 뭉개지면 안 되기 때문이다.
    first_gold_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    retrieved_chunk_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    gold_chunk_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    run: Mapped["EvalRun"] = relationship(back_populates="results")


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
