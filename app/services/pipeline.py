"""Shared query pipeline.

``POST /query`` (one-shot) and ``POST /conversations/{id}/query`` (streaming,
persisted) must behave identically on everything that is not presentation:
rate limits, quota, scope ownership, the semantic cache, retrieval, usage
accounting and tracing. Keeping that in one place is what stops the two
endpoints from drifting — a limit enforced on one path but not the other is a
hole, not a difference.

The runner is a single request's worth of state, so the callers pass the
question once instead of threading a dozen arguments through every step.
"""

import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

from fastapi import HTTPException
from fastapi import status as http_status
from pgvector import SparseVector
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import VERIFICATION_REQUIRED
from app.models import User
from app.services import cache, embeddings, ingest, rerank, retrieve, tracing, usage
from app.services.upstream import UpstreamUnavailable
from app.services.ratelimit import query_limiter
from app.services.tracing import (
    STAGE_DENSE,
    STAGE_RERANK,
    STAGE_RRF,
    STAGE_SPARSE,
    Stopwatch,
    TraceDraft,
)


@dataclass
class Retrieval:
    chunks: list[retrieve.RetrievedChunk] = field(default_factory=list)
    stage_ids: dict[str, list[uuid.UUID]] = field(default_factory=dict)
    stage_chunks: dict[str, list[retrieve.RetrievedChunk]] = field(default_factory=dict)


def too_many(detail: str) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": "60"},
    )


NOT_FOUND = HTTPException(
    status_code=http_status.HTTP_404_NOT_FOUND, detail="not found"
)


@contextmanager
def _reachable():
    """Report a model server being down as 503, not as this service crashing.

    Both query paths run through here, so neither can report an outage
    differently from the other. The clients raise a domain error precisely so
    the translation happens once, at the boundary that has a response to put
    it in — background indexing has none, and records the failure instead.

    No ``Retry-After``: nothing here knows when the server comes back, and
    unlike a provider quota it usually takes someone restarting it.
    """
    try:
        yield
    except UpstreamUnavailable as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="search unavailable",
        ) from exc


class QueryRunner:
    """One question, from limits through to tracing."""

    def __init__(
        self,
        session: AsyncSession,
        user: User,
        question: str,
        *,
        document_id: uuid.UUID | None,
        hybrid: bool | None,
    ) -> None:
        self.session = session
        self.user = user
        self.question = question
        self.document_id = document_id
        self.use_hybrid = hybrid if hybrid is not None else settings.hybrid_enabled
        self.watch = Stopwatch()
        self.draft = TraceDraft(
            user_id=user.id,
            question=question,
            document_id=document_id,
            hybrid=self.use_hybrid,
        )
        self.dense: list[float] = []
        self.sparse: SparseVector | None = None
        # "이 요청은 받아들여졌고, 아직 트레이스 행을 쓰지 않았다."
        #
        # 실패를 **어디까지** 기록할지에 대한 판단이 이 플래그 하나에 들어
        # 있다. enforce_limits 가 통과한 뒤부터 record_cache_hit/finalize 가
        # 행을 쓰기 전까지의 구간 — 즉 서버가 실제로 일을 시작한 질의만
        # 기록한다. 자세한 근거는 record_failure 참조.
        self._trace_pending = False
        # enforce_limits 가 예약에 성공하면 채워지고, record_cache_hit 나
        # finalize 가 그 예약을 진짜 기록으로 바꾸는 순간 다시 None 이 된다.
        # 요청이 둘 중 어느 쪽에도 이르지 못하고 끝나면 이 값이 곧
        # release_reservation 이 지워야 할 대상이다.
        self._reservation_id: uuid.UUID | None = None

    async def enforce_limits(self, client_ip: str) -> None:
        if not query_limiter.allow(f"user:{self.user.id}") or not query_limiter.allow(
            f"ip:{client_ip}"
        ):
            raise too_many("query rate limit exceeded")

        # 여기서부터 커밋까지가 "세고 나서 쓴다"를 원자로 만드는 구간이다.
        # 잠금 없이는 동시 요청들이 모두 같은(터지기 전) 집계를 읽고 모두
        # 통과해, 위의 분당 버킷이 막는 "얼마나 빨리"와 다른 문제 —
        # "얼마나 많이"가 새어나간다. 잠금은 트랜잭션 범위라 이 커밋(또는
        # release_reservation 의 롤백)이 곧 해제이므로, 로컬 쿼리 몇 번
        # 뒤에는 곧바로 풀린다 — 뒤이은 embed/retrieve/rerank/generate 는
        # 잠금 없이 실행된다. LLM 호출은 수십 초가 걸릴 수 있어서, 그
        # 동안 커넥션을 붙잡고 이 사용자의 다른 요청까지 세우는 것은
        # 스펙이 명시적으로 금지한 트레이드오프다.
        try:
            await usage.acquire_quota_lock(self.session, self.user.id, "query")
            # 미인증 계정은 다른 한도를 다른 창으로 센다 — 하루가 아니라
            # 계정 수명 전체. 그래야 재가입으로 초기화되지 않는다.
            if not self.user.email_verified:
                if await usage.unverified_query_exceeded(self.session, self.user.id):
                    raise VERIFICATION_REQUIRED
            elif await usage.query_quota_exceeded(self.session, self.user.id):
                raise too_many("daily query quota exceeded")

            self._reservation_id = await usage.reserve(
                self.session, self.user.id, "query"
            )
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise

        # 커밋이 끝난 **뒤에** 세운다. 위에서 튕겨나간 요청(레이트리밋·쿼터·
        # 미인증)은 예약도 없고 트레이스도 없다 — 아래 record_failure 가
        # 설명하는 그 판단이 여기서 한 줄로 강제된다.
        self._trace_pending = True

    async def resolve_scope(self) -> None:
        """A scope the caller does not own is 404, same as the documents API.

        Checked before any embedding: it avoids spending on an unusable scope
        and keeps the cache from being keyed to a document that does not exist.
        """
        if self.document_id is None:
            return
        owned = await ingest.get_document(
            self.session, self.document_id, user_id=self.user.id
        )
        if owned is None:
            raise NOT_FOUND

    async def embed(self) -> None:
        """Embed once; the dense vector serves the cache probe and retrieval."""
        async with self.watch.time("embed"):
            with _reachable():
                if self.use_hybrid:
                    self.dense, self.sparse = await embeddings.embed_query_full(
                        self.question
                    )
                else:
                    self.dense = await embeddings.embed_query(self.question)

    async def cached_answer(self) -> cache.CachedAnswer | None:
        return await cache.lookup(
            self.session,
            user_id=self.user.id,
            document_id=self.document_id,
            question_embedding=self.dense,
        )

    async def record_cache_hit(self, hit: cache.CachedAnswer) -> None:
        # Counts against the quota but costs no LLM call. Turns the
        # reservation enforce_limits already made into the real record rather
        # than inserting a second row for the same request.
        await usage.commit_reservation(self.session, self._reservation_id, cached=True)
        self._reservation_id = None
        self.draft.cached = True
        self.draft.refused = hit.refused
        self.draft.answer = hit.answer
        # record 보다 먼저 내린다. record 는 실패해도 조용히 None 을
        # 돌려주므로, 여기서 안 내리면 뒤이은 실패 처리가 같은 요청에
        # 두 번째 행을 쓰려 든다.
        self._trace_pending = False
        await tracing.record(self.session, self.draft, self.watch)

    def _remember(self, found: Retrieval) -> Retrieval:
        """검색 결과를 초안에 미리 새겨 둔다 — 뒤에서 실패할 때를 위해서다.

        성공하면 finalize 가 같은 값을 다시 넣으므로 이 복사는 성공 경로에
        아무 영향이 없다. 값이 필요한 쪽은 실패 경로다: 생성 단계에서
        LLM 이 터졌을 때 "검색은 정답 청크를 찾아뒀는데 생성이 죽었다"와
        "애초에 못 찾았다"는 완전히 다른 장애이고, 이 모듈이 단계를 쪼개
        기록하는 이유가 바로 그 구분이다. 초안에 안 남기면 실패한 질의의
        트레이스는 단계가 통째로 빈 채로 남아, 있으나 마나 해진다.
        """
        self.draft.stage_ids = found.stage_ids
        self.draft.stage_chunks = found.stage_chunks
        return found

    async def retrieve(self) -> Retrieval:
        if not self.use_hybrid or self.sparse is None:
            async with self.watch.time("retrieve"):
                hits = await retrieve.search(
                    self.session,
                    self.dense,
                    user_id=self.user.id,
                    document_id=self.document_id,
                )
            return self._remember(Retrieval(hits, {}, {STAGE_DENSE: hits}))

        async with self.watch.time("retrieve"):
            fused = await retrieve.hybrid_search(
                self.session,
                self.dense,
                self.sparse,
                user_id=self.user.id,
                document_id=self.document_id,
            )

        stage_ids = {STAGE_DENSE: fused.dense_ids, STAGE_SPARSE: fused.sparse_ids}
        if not fused.candidates:
            return self._remember(Retrieval([], stage_ids, {}))

        async with self.watch.time("rerank"):
            with _reachable():
                ranked = await rerank.rerank(
                    self.question, [c.content for c in fused.candidates]
                )
        top: list[retrieve.RetrievedChunk] = []
        for idx, score in ranked[: settings.rerank_top]:
            chunk = fused.candidates[idx]
            chunk.score = score  # replace RRF score with reranker relevance
            top.append(chunk)

        return self._remember(
            Retrieval(top, stage_ids, {STAGE_RRF: fused.candidates, STAGE_RERANK: top})
        )

    @property
    def grounding_floor(self) -> float | None:
        """The hybrid path scores are reranker sigmoids, not cosine."""
        return settings.rerank_min_score if self.use_hybrid else None

    async def finalize(
        self,
        *,
        found: Retrieval,
        answer: str,
        refused: bool,
        citations: list[dict],
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        """Record usage, populate the cache, and write the trace."""
        # Turns the reservation enforce_limits made into the real record —
        # the row already exists, so this fills in what was not known yet at
        # reservation time instead of inserting a second one.
        await usage.commit_reservation(
            self.session,
            self._reservation_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        self._reservation_id = None
        await cache.store(
            self.session,
            user_id=self.user.id,
            document_id=self.document_id,
            question=self.question,
            question_embedding=self.dense,
            answer=answer,
            refused=refused,
            citations=citations,
        )

        self.draft.refused = refused
        self.draft.answer = answer
        self.draft.llm_model = settings.llm_model
        self.draft.tokens_in = tokens_in
        self.draft.tokens_out = tokens_out
        self.draft.stage_ids = found.stage_ids
        self.draft.stage_chunks = found.stage_chunks
        self._trace_pending = False  # record_cache_hit 와 같은 이유
        await tracing.record(self.session, self.draft, self.watch)

    async def record_failure(self, exc: BaseException) -> None:
        """실패로 끝난 질의를 traces 에 남긴다.

        **무엇을 남기고 무엇을 남기지 않는지가 이 함수의 판단이다.**

        남긴다: enforce_limits 를 통과한 뒤에 깨진 것 전부 — 소유하지 않은
        문서 범위(404), 임베딩/리랭크 서버 다운(503), 생성 중 예기치 못한
        예외(500), 스트림 도중의 실패. 이들은 서버가 실제로 일을 시작한
        요청이고, 장애 중에 운영자가 세고 싶은 것도 정확히 이것이다.
        볼륨은 레이트리밋과 쿼터가 이미 위에서 막아준다.

        남기지 않는다: enforce_limits 자신이 거절한 요청 — 분당 레이트리밋
        429, 일일 쿼터 429, 미인증 403. 두 가지 이유다.
        ① 이것들은 시스템의 고장이 아니라 입장 통제의 정상 동작이다.
        ② 레이트리밋의 존재 이유가 "거절을 싸게 만드는 것"인데, 거절마다
           행을 하나씩 insert 하면 재시도 루프에 빠진 클라이언트 하나가
           무제한의 DB 쓰기로 번역된다 — 관측이 관측 대상을 무너뜨리는
           바로 그 모양이다. 게다가 이미 세어지고 있다: 쿼터 거절은
           usage_events 에, 셋 다 요청 id 가 찍힌 액세스 로그에 남는다.

        한 요청에 두 행을 쓰지 않는다(``_trace_pending``). finalize 뒤에
        터진 실패 — 예컨대 답변을 대화에 저장하다 깨진 경우 — 는 이미
        자기 트레이스를 갖고 있고, 그 행을 실패로 덮어쓰면 실제로 있었던
        답변과 토큰 소비가 지워진다.

        호출자는 예약 정리(``release_reservation``) **뒤에** 부른다. 정리는
        사용자의 쿼터가 걸린 정확성 문제고 이 기록은 최선 노력이라, 순서가
        뒤바뀌면 기록이 정리를 밀어낼 수 있다.
        """
        if not self._trace_pending:
            return
        self._trace_pending = False

        failure = tracing.describe_failure(exc)
        self.draft.status_code = failure.status_code
        self.draft.error = failure.reason
        await tracing.record(self.session, self.draft, self.watch)

    async def release_reservation(self) -> None:
        """Undo the quota reservation if the work it stood for never finished.

        Callers must reach this from every failure path between
        ``enforce_limits`` and whichever of ``record_cache_hit``/``finalize``
        the request was headed for — embedding a 503, retrieval raising,
        the LLM call failing partway through a stream — or the reservation
        commits the request never earned. Safe to call unconditionally from
        an ``except``/``finally``: once ``record_cache_hit`` or ``finalize``
        has run, ``_reservation_id`` is already ``None`` and this is a no-op,
        which is what lets both routers call it blindly on any exception
        without first working out whether one of those two ran.
        """
        if self._reservation_id is None:
            return
        await usage.release_reservation(self.session, self._reservation_id)
        self._reservation_id = None
