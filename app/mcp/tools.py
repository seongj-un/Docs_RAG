"""The tools this server exposes, in whichever variant it was built with.

Registered onto an ``MCPServer`` the way a router is included into the FastAPI
app, so ``server.py`` stays a description of the server and this file stays the
one place the tools' behaviour lives.

The bodies are deliberately thin. Everything that decides whether a search is
allowed and what it is allowed to see — rate limit, quota, scope ownership,
tenant-scoped retrieval, tracing — belongs to ``services.pipeline.QueryRunner``
and is merely driven from here. A check reimplemented in this file would be a
check that can disagree with ``POST /query``, which is exactly the failure the
pipeline module exists to prevent.

**M7 W4 note.** ``register(mcp)`` with no variant registers exactly what W2
shipped, and that is what ``server.py`` calls. The variant argument exists so
the W4 harness can mount a different tool surface against the *same* pipeline;
what each variant is and why is in ``app/mcp/variants.py``. The registration
helpers below are split by input schema rather than parameterised, because the
production signature is what the SDK turns into the JSON Schema an agent reads
— building that signature dynamically would mean production's prompt is
assembled by the experiment's code path.

**M7 W6 note.** ``answer_question`` is the second thing this file registers, and
it is the first tool here that does not stop at retrieval — it drives
``QueryRunner.finalize`` and returns a written answer. It exists for W6's
generation-site comparison (server generation vs the calling agent's), and like
every variant tool it is reachable only when a variant declares it: production
still ships ``search_documents`` alone. The two are told apart forever by
``traces.source`` — ``mcp_search`` against ``mcp_answer`` — which is what lets
the comparison be recomputed from the database after the report is gone.
"""

import logging
import uuid
from typing import Annotated, Literal

from fastapi import HTTPException
from fastapi import status as http_status
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import Field
from sqlalchemy import func, select

from app.db import SessionLocal
from app.mcp import variants
from app.mcp.scopes import SCOPE_SEARCH, scoped_tool
from app.mcp.schemas import (
    AnswerCitation,
    AnswerQuestionResult,
    DocumentSummary,
    ListCollectionsResult,
    Passage,
    SearchDocumentsResult,
    SearchHit,
)
from app.models import Chunk, Document, User
from app.services import generate
from app.services.pipeline import QueryRunner
from app.services.tracing import SOURCE_MCP_ANSWER, SOURCE_MCP_SEARCH

logger = logging.getLogger(__name__)

# 에이전트가 요청할 수 있는 최대 청크 수. 기본값(RERANK_TOP=3)은 우리 프롬프트
# 에 맞춰 M4 골든셋으로 고른 값이지만, 에이전트는 자기 컨텍스트 예산이 다르니
# 더 달라고 할 수 있어야 한다. 상한을 두는 이유는 반대 방향의 사고다 —
# 1,295자짜리 청크(app/config.py 의 실측)를 50개 돌려주면 6만 자가 에이전트
# 컨텍스트로 쏟아진다. 20이면 최악도 2.6만 자 안팎이고, 리랭커가 만드는 후보가
# CAND_K(40)뿐이라 그 위로는 어차피 줄 것도 없다.
MAX_RESULTS_CEILING = 20


def _to_tool_error(exc: HTTPException) -> ToolError:
    """Turn the pipeline's HTTP refusal into something a model can act on.

    ``ToolError`` (not a bare exception) because every one of these is a
    failure the *model* should read and change its behaviour over: a generic
    exception would reach it as "Error executing tool search_documents" with
    the reason stripped, and it would retry the identical call forever.

    The messages say what to do next, since an agent has no status code to
    interpret and no user to ask. The details themselves are already
    user-facing strings from the HTTP API, so nothing new leaks by passing
    them on.
    """
    detail = exc.detail if isinstance(exc.detail, str) else "request refused"
    status = exc.status_code

    if status == http_status.HTTP_404_NOT_FOUND:
        return ToolError(
            "No such document for this user. Either the document_id is wrong "
            "or it belongs to someone else — retry without document_id to "
            "search everything this user has uploaded."
        )
    if status == http_status.HTTP_429_TOO_MANY_REQUESTS:
        return ToolError(
            f"{detail}. Stop searching and tell the user they have hit their "
            "search limit; retrying now will not succeed."
        )
    if status == http_status.HTTP_403_FORBIDDEN:
        return ToolError(
            f"{detail}. This account must confirm its email address before it "
            "can search. Tell the user; there is nothing to retry."
        )
    if status == http_status.HTTP_503_SERVICE_UNAVAILABLE:
        return ToolError(
            f"{detail}. The search backend is down — this is not caused by the "
            "question. Tell the user rather than rephrasing and retrying."
        )
    return ToolError(detail)


def _client_ip(ctx: Context | None) -> str:
    """Best-effort client address for the per-IP rate limit bucket.

    Mirrors what the routers pass: ``request.client.host``, which uvicorn fills
    from ``X-Forwarded-For`` under ``--proxy-headers`` (see the Dockerfile's
    note on why that flag matters behind Caddy). Falls back to ``"unknown"``
    exactly as ``routers/query.py`` does — one shared bucket for callers we
    cannot place is the safe direction, since the per-user bucket still holds.
    """
    request = ctx.request_context.request if ctx is not None else None
    if request is None or request.client is None:
        return "unknown"
    return request.client.host


def _subject_id() -> uuid.UUID:
    """The authenticated user id, from the token and from nowhere else.

    테넌트 격리: 주체는 오직 ``get_access_token()`` 에서 온다. 검증기가 세션 행을
    읽어 넣은 값이고, 헤더나 인자에서 사용자를 읽는 경로는 이 파일에 존재하지
    않는다. 여기 도달했다는 것은 미들웨어가 이미 통과시켰다는 뜻이라 정상적으로
    None 은 나오지 않지만 그래도 막는다 — 주체 없이 아래로 내려가면 격리가
    없는 조회가 된다.
    """
    access = get_access_token()
    if access is None or not access.subject:
        raise ToolError("authentication required")
    return uuid.UUID(access.subject)


def _snippet(text: str, limit: int) -> str:
    """Cut a passage to ``limit`` characters, or leave it whole when 0.

    실험 4(출력 길이)의 유일한 구현 지점. 0 이 프로덕션이고, 0 에서는 이 함수가
    입력을 그대로 돌려준다 — 즉 프로덕션 경로에 분기가 하나 늘 뿐 동작은 같다.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    # 잘렸다는 사실을 모델이 알아야 한다. 조용히 자르면 모델은 문장이 거기서
    # 끝난 줄 알고 잘린 표의 뒷부분을 지어낸다.
    return text[:limit].rstrip() + "…"


async def _run_search(
    *,
    question: str,
    document_id: uuid.UUID | None,
    max_results: int | None,
    client_ip: str,
    snippet_chars: int,
) -> SearchDocumentsResult:
    """Drive the production retrieval pipeline and shape the result.

    Every variant's search tool funnels through here, so a variant can change
    what an agent *reads* but never what the server *does*. If a variant ever
    needs a different pipeline call, the experiment has stopped being about
    tool design.
    """
    # 검증기의 세션은 이미 닫혔다. 여기서 새로 여는 이 세션이 이 요청의
    # 세션이고, async with 가 리트리벌이 몇 초를 쓰든 반납을 보장한다.
    async with SessionLocal() as db:
        user = await db.get(User, _subject_id())
        if user is None:
            # 토큰 검증과 지금 사이에 계정이 지워진 경우. 401 이 아니라
            # 툴 오류인 이유는 이미 인증 미들웨어를 지나왔기 때문이다.
            raise ToolError("authentication required")

        runner = QueryRunner(
            db,
            user,
            question,
            document_id=document_id,
            # hybrid=None → 서버 기본값(HYBRID_ENABLED). 툴 인자로 열지
            # 않는다: dense-only 는 우리가 A/B 하려고 만든 스위치이지
            # 에이전트가 할 선택이 아니고, 노출하면 W4 의 측정에 우리가
            # 통제하지 못하는 변수가 하나 늘어난다.
            hybrid=None,
            # 이 트레이스가 에이전트에서 왔다는 사실을 남기는 곳. W4 가
            # L3 지표를 뽑고 W6 가 "서버 생성 vs 에이전트 생성"을 비교할 때
            # 쓰는 축이다 — W6 가 서버 생성 MCP 툴을 만들면 그 툴은
            # mcp_answer 를 쓰고, 같은 컬럼으로 둘이 비교된다.
            source=SOURCE_MCP_SEARCH,
        )

        try:
            # --- 쿼터 판단 (M7 W2) --------------------------------------
            # 이 툴은 LLM 을 부르지 않는데도 기존 "query" 쿼터를 그대로
            # 깎는다. 판단 근거:
            #
            # 1. 쿼터는 비용을 묶는 장치고, 이 툴이 하는 일이 곧 질의
            #    비용의 비싼 절반이다. 실측(app/config.py, 2026-09-11)으로
            #    CAND_K=40·1,295자 청크에서 리랭킹만 8.8초다. 생성은
            #    flash-lite 로 0.9초였다 — 빠진 쪽이 싼 쪽이다. "LLM 을
            #    안 부르니 공짜"는 이 파이프라인에서 사실이 아니다.
            # 2. 과소 계상이 위험한 방향이다. 에이전트는 사람보다 훨씬
            #    자주 부른다(한 질문을 여러 검색으로 쪼갠다). 공짜로 두면
            #    에이전트 루프 하나가 리랭커를 무제한으로 태우는 동안
            #    /usage 화면은 0 을 보여준다 — 한도를 넘긴 사실조차
            #    사용자에게 보이지 않는 종류의 사고다.
            # 3. "query" 로 세면 미인증 게이트(계정 수명 전체 누적 5회)도
            #    자동으로 적용된다. 갓 만든 미인증 계정이 에이전트용
            #    무제한 검색 API 를 얻는 것은 M3 가 막으려던 재가입
            #    어뷰즈 그대로다.
            #
            # 새 kind("mcp_search")를 만들지 않은 이유: 설정에 한도가 없고
            # usage.query_quota_exceeded 는 kind="query" 로 고정이며
            # /usage 응답(UsageOut)에도 자리가 없다. 즉 새 kind 는 지금
            # 당장은 **아무도 세지 않는** kind 가 된다 — 정확한 칸에 안
            # 세는 것보다 부정확한 칸에 세는 편이 낫다.
            #
            # 치르는 비용은 인정한다: 에이전트의 검색 10번이 사람의 하루
            # 200질문 중 10번을 먹고, 이 툴은 실제로 전체 질의보다 싸다.
            # W2 의 완료 기준이 "실사용 후 불편한 지점 3개"이니 이건 그
            # 후보 1번이다 — 실사용 숫자를 보고 W7 에서 다시 판단한다.
            #
            # 레이트리밋도 같은 이유로 기존 query_limiter 를 쓴다
            # (enforce_limits 안에서 user:/ip: 두 버킷 모두).
            await runner.enforce_limits(client_ip)
            await runner.resolve_scope()
            await runner.embed()
            found = await runner.retrieve(limit=max_results)
            # 트레이싱: 생성이 없을 뿐 기록은 HTTP 경로와 똑같이 남긴다.
            # W4/W5 가 L3 지표(툴 선택 정확도·호출 수)를 이 행들에서
            # 뽑는다 — 지금 안 남기면 그때는 이미 지나간 트래픽이다.
            await runner.finalize_search(found=found)
        except HTTPException as exc:
            # release_reservation 은 무조건 부를 수 있다(마무리됐거나
            # 예약이 없으면 no-op). enforce_limits 가 던진 경우까지 한
            # 자리에서 처리하려고 일부러 같은 블록에 둔다.
            await runner.release_reservation()
            raise _to_tool_error(exc) from exc
        except BaseException:
            # 예약이 선 채로 끝나면 일어나지 않은 검색에 쿼터가 매겨진다.
            # routers/query.py 가 모든 실패 경로에서 이걸 부르는 것과
            # 같은 이유다.
            await runner.release_reservation()
            raise

        return SearchDocumentsResult(
            hits=[
                SearchHit(
                    chunk_id=c.chunk_id,
                    document_id=c.document_id,
                    page_from=c.page_from,
                    page_to=c.page_to,
                    score=c.score,
                    content=_snippet(c.content, snippet_chars),
                )
                for c in found.chunks
            ],
            searched_document_id=document_id,
        )


# 생성 경로에만 존재하는 실패. ``app/services/llm.py`` 가 프로바이더의 429/503 을
# 이 두 문구로 번역해 올려보내는데, ``_to_tool_error`` 의 429/503 안내문은
# 검색용이라 "검색 한도에 걸렸다"·"검색 백엔드가 죽었다"고 말한다. 둘 다 사실이
# 아니고, 에이전트가 그 말을 사용자에게 그대로 옮기면 우리가 거짓말을 시킨 것이
# 된다. 그래서 이 둘만 먼저 가로채고 나머지는 W2 의 번역기가 그대로 처리한다 —
# 검색 툴의 문구는 그 툴의 프롬프트이므로 건드리지 않는다.
_GENERATION_FAILURES = {
    "model quota exceeded": (
        "The answering model is out of quota for now. Retrieval still works — "
        "nothing is wrong with the question. Tell the user the service cannot "
        "write answers at the moment; retrying immediately will not succeed."
    ),
    "model unavailable": (
        "The answering model is unavailable. This is not caused by the "
        "question. Tell the user rather than rephrasing and retrying."
    ),
}


def _to_answer_tool_error(exc: HTTPException) -> ToolError:
    detail = exc.detail if isinstance(exc.detail, str) else ""
    message = _GENERATION_FAILURES.get(detail)
    return ToolError(message) if message else _to_tool_error(exc)


async def _run_answer(
    *,
    question: str,
    document_id: uuid.UUID | None,
    client_ip: str,
) -> AnswerQuestionResult:
    """Drive the production *generation* path and shape the result (M7 W6).

    This is Notion W6's mode A — the server writes the answer, so the citation
    format and the refusal guardrail are ours to enforce. Mode B is
    ``_run_search`` above, which stops at retrieval and lets the calling agent
    write the answer with neither.

    **The steps below are ``routers/query.py``'s steps, in its order, through
    the same objects.** Nothing about answering a question from documents is
    re-decided here: the grounding floor is ``runner.grounding_floor``, the
    refusal sentence and the ``[p.N]`` citation contract are
    ``services/generate.py``'s, and the accounting is ``runner.finalize``. The
    only thing this function owns is the wire shape, because that is the only
    thing that differs between an HTTP client and an agent. A copy of the
    generation logic here would be a second answer path that can drift from
    ``POST /query`` — which is the failure ``services/pipeline.py`` exists to
    prevent, and it would also make W6's comparison meaningless: "server
    generation" has to mean *this server's* generation, not a variant of it
    written for the experiment.

    That includes the semantic cache. ``finalize`` stores into it, so probing
    it is not optional — a path that writes the cache but never reads it makes
    the same user's next question pay for an answer already bought. The eval
    runner turns the cache off for its own reasons (``eval/w6_run.py``), which
    is a measurement decision and not a behaviour difference.
    """
    async with SessionLocal() as db:
        user = await db.get(User, _subject_id())
        if user is None:
            raise ToolError("authentication required")

        runner = QueryRunner(
            db,
            user,
            question,
            document_id=document_id,
            hybrid=None,
            # 0011 이 예고한 값. mcp_search 와 갈라 두는 것이 W6 의 비교축을
            # 트레이스만으로 되살릴 수 있게 하는 유일한 장치다.
            source=SOURCE_MCP_ANSWER,
        )

        try:
            # 쿼터는 search 와 **같은** "query" 칸을 깎는다. 이 툴은 검색이
            # 하는 일을 전부 하고 생성까지 더하므로, 같은 값을 매기는 것이
            # 과소 계상이지 과대 계상이 아니다 — search 쪽 쿼터 주석의
            # 판단(과소 계상이 위험한 방향)이 여기서도 그대로 성립한다.
            # 두 배로 세지 않는 이유는 이 툴이 실험 조건이기도 해서다:
            # 두 모드가 다른 요율로 쿼터를 깎으면 W6 의 스윕 중간에 한쪽만
            # 한도에 닿아, 비교가 아니라 "먼저 죽은 쪽"을 재게 된다.
            await runner.enforce_limits(client_ip)
            await runner.resolve_scope()
            await runner.embed()

            hit = await runner.cached_answer()
            if hit is not None:
                await runner.record_cache_hit(hit)
                return AnswerQuestionResult(
                    answer=hit.answer,
                    refused=hit.refused,
                    # 캐시에 담긴 인용은 저장 시점에 직렬화된 dict 다. 키가
                    # AnswerCitation 과 같도록 finalize 에 넘길 때부터 맞춰
                    # 두므로(아래) 그대로 되살린다.
                    citations=[AnswerCitation(**c) for c in hit.citations],
                    searched_document_id=document_id,
                )

            found = await runner.retrieve()
            # 스톱워치에 단계를 하나 더 여는 것이 라우터와 같은 모양이다 —
            # 그래야 trace 의 generate_ms 가 채워지고, W6 의 지연 비교에서
            # "생성이 몇 ms 였나"가 리포트가 아니라 DB 에서 나온다.
            async with runner.watch.time("generate"):
                result = await generate.answer_question(
                    question, found.chunks, min_score=runner.grounding_floor
                )

            citations = [
                AnswerCitation(
                    chunk_id=c.chunk_id,
                    document_id=c.document_id,
                    page_from=c.page_from,
                    page_to=c.page_to,
                    snippet=c.snippet,
                )
                for c in result.citations
            ]
            await runner.finalize(
                found=found,
                answer=result.answer,
                refused=result.refused,
                citations=[c.model_dump(mode="json") for c in citations],
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
            )
        except HTTPException as exc:
            await runner.release_reservation()
            raise _to_answer_tool_error(exc) from exc
        except BaseException:
            await runner.release_reservation()
            raise

        return AnswerQuestionResult(
            answer=result.answer,
            refused=result.refused,
            citations=citations,
            searched_document_id=document_id,
        )


# --- 등록 헬퍼 -------------------------------------------------------------
#
# 하나에 스키마 하나. 왜 매개변수화하지 않았는지는 모듈 docstring 에 있다.


def _register_search_full(mcp: MCPServer, spec: variants.ToolSpec,
                          variant: variants.ToolVariant) -> None:
    """The production input schema: question + document_id + max_results."""

    # mcp.tool 이 아니라 scoped_tool 이다. 스코프를 선언하는 것과 강제하는
    # 것이 한 줄이어야 둘이 어긋나지 않는다 — 근거는 app/mcp/scopes.py.
    @scoped_tool(
        mcp,
        scope=SCOPE_SEARCH,
        name=spec.name,
        title=spec.title,
        description=spec.description,
    )
    async def search_documents(
        question: Annotated[
            str,
            Field(
                min_length=1,
                max_length=2000,
                description="The user's question, in their own words. Not "
                "keywords — the retriever is built for natural questions.",
            ),
        ],
        ctx: Context,
        document_id: Annotated[
            uuid.UUID | None,
            Field(
                default=None,
                description="Restrict the search to one document, using a "
                "`document_id` from an earlier result. Omit to search every "
                "document this user has uploaded.",
            ),
        ] = None,
        max_results: Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                le=MAX_RESULTS_CEILING,
                description="How many passages to return. Omit for the "
                "server's default, which is tuned for answering one question.",
            ),
        ] = None,
    ) -> SearchDocumentsResult:
        # ⚠️ description= 이 위에 있으므로 이 docstring 은 모델에게 가지 않는다
        # (SDK 우선순위: description 인자 > docstring). 여기 적는 것은 우리를
        # 위한 것이다.
        #
        # document_id 는 사용자가 **이미 소유한** 것 안에서 좁히기만 하며
        # (resolve_scope + retrieve.py 의 조인), 소유하지 않은 id 는 404 —
        # 기존 라우터와 같은 의미로 "없는 것"이다.
        return await _run_search(
            question=question,
            document_id=document_id,
            max_results=max_results,
            client_ip=_client_ip(ctx),
            snippet_chars=variant.snippet_chars,
        )


def _register_search_query_only(mcp: MCPServer, spec: variants.ToolSpec,
                                variant: variants.ToolVariant) -> None:
    """실험 3 조건 A: 범위를 좁힐 손잡이가 아예 없는 스키마."""

    @scoped_tool(
        mcp,
        scope=SCOPE_SEARCH,
        name=spec.name,
        title=spec.title,
        description=spec.description,
    )
    async def search_query_only(
        question: Annotated[
            str,
            Field(
                min_length=1,
                max_length=2000,
                description="The user's question, in their own words. Not "
                "keywords — the retriever is built for natural questions.",
            ),
        ],
        ctx: Context,
    ) -> SearchDocumentsResult:
        return await _run_search(
            question=question,
            document_id=None,
            max_results=None,
            client_ip=_client_ip(ctx),
            snippet_chars=variant.snippet_chars,
        )


def _register_search_collection_enum(mcp: MCPServer, spec: variants.ToolSpec,
                                     variant: variants.ToolVariant) -> None:
    """실험 3 조건 B: question + collection(문서 이름 열거형).

    열거형 값이 **문서 이름**이지 UUID 가 아닌 것이 이 조건의 전부다. UUID 를
    열거해 봤자 모델은 어느 것이 무엇인지 모르므로, 그렇게 만든 "열거형"은
    Notion 이 말한 "범위 좁히기 성공률"을 올릴 수가 없다.

    ⚠️ 값은 실행 시점에 이 계정의 문서 이름으로 채워진다. 즉 이 툴의 스키마는
    **사용자마다 다르다** — 그것이 tools/list 의 cacheScope 가 private 여야 하는
    또 하나의 이유이고, 다행히 이미 private 다(server.py 의 _cache_hints).
    """
    names = variant.collections
    if not names:
        raise ValueError(
            "collection_enum 변형은 문서 이름 목록 없이는 등록할 수 없다. "
            "ToolVariant.with_collections() 로 채운 뒤 넘길 것."
        )
    # Literal 을 실행 시점에 만든다. SDK 가 이것을 JSON Schema 의 enum 으로
    # 그대로 옮긴다 — 우리가 손으로 enum 을 써 넣으면 검증과 스키마가 갈린다.
    collection_type = Literal[names]  # type: ignore[valid-type]

    @scoped_tool(
        mcp,
        scope=SCOPE_SEARCH,
        name=spec.name,
        title=spec.title,
        description=spec.description,
    )
    async def search_with_collection(
        question: Annotated[
            str,
            Field(
                min_length=1,
                max_length=2000,
                description="The user's question, in their own words. Not "
                "keywords — the retriever is built for natural questions.",
            ),
        ],
        ctx: Context,
        collection: Annotated[
            collection_type | None,
            Field(
                default=None,
                description="Search only this document. Omit to search every "
                "document this user has uploaded.",
            ),
        ] = None,
    ) -> SearchDocumentsResult:
        document_id = None
        if collection is not None:
            document_id = await _document_id_for(_subject_id(), str(collection))
            if document_id is None:
                # 열거형에 있는데 없는 문서 = 목록을 만든 뒤 지워진 것.
                raise ToolError(
                    f"No document named {collection!r} for this user any more. "
                    "Retry without `collection` to search everything."
                )
        return await _run_search(
            question=question,
            document_id=document_id,
            max_results=None,
            client_ip=_client_ip(ctx),
            snippet_chars=variant.snippet_chars,
        )


async def _document_id_for(user_id: uuid.UUID, name: str) -> uuid.UUID | None:
    """Resolve a document name to its id, within this user's own documents."""
    async with SessionLocal() as db:
        row = await db.execute(
            select(Document.id).where(
                Document.user_id == user_id, Document.filename == name
            )
        )
        return row.scalars().first()


def _register_fetch(mcp: MCPServer, spec: variants.ToolSpec,
                    variant: variants.ToolVariant) -> None:
    """Read one passage back by id. Mirrors ``GET /chunks/{id}``.

    Owner-scoped through the chunk's document, and a chunk belonging to someone
    else answers "not found" rather than "forbidden" — the same judgement, and
    the same SQL join, as ``routers/chunks.py``. Different wording would mean
    two answers to "does this id exist", and the cheaper one to get wrong is
    the one an agent can call in a loop.

    쿼터를 깎지 않는다. 검색 쿼터는 리랭킹 비용을 묶는 장치인데(tools 의 쿼터
    주석) 이 툴은 인덱스 조회 하나이고 리랭커를 태우지 않는다. 검색과 같은 값을
    매기면 분해 조건이 "툴이 셋"이 아니라 "쿼터가 두 배로 빨리 닳음"이 되어
    실험이 측정하려던 것과 다른 것을 잰다.
    """

    @scoped_tool(
        mcp,
        scope=SCOPE_SEARCH,
        name=spec.name,
        title=spec.title,
        description=spec.description,
    )
    async def fetch(
        chunk_id: Annotated[
            uuid.UUID,
            Field(
                description="The `chunk_id` of a passage you have already "
                "seen, from an earlier search result."
            ),
        ],
    ) -> Passage:
        user_id = _subject_id()
        async with SessionLocal() as db:
            row = await db.execute(
                select(Chunk)
                .join(Document, Chunk.document_id == Document.id)
                .where(Chunk.id == chunk_id, Document.user_id == user_id)
            )
            chunk = row.scalars().first()
            if chunk is None:
                raise ToolError(
                    "No such passage for this user. The id is wrong, or it "
                    "belongs to someone else — search again to get a current "
                    "`chunk_id` rather than retrying this one."
                )
            return Passage(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                page_from=chunk.page_from,
                page_to=chunk.page_to,
                content=_snippet(chunk.content, variant.snippet_chars),
            )


def _register_list_collections(mcp: MCPServer, spec: variants.ToolSpec,
                               variant: variants.ToolVariant) -> None:
    """Say which documents exist, without reading inside any of them.

    ``status == "ready"`` 만 센다. 인덱싱 중이거나 실패한 문서를 목록에 넣으면
    에이전트는 그 document_id 로 검색을 걸고 빈 결과를 받는다 — 그리고 "문서에
    없다"는 결론을 낸다. 아직 없는 것과 정말 없는 것은 다르다.
    """

    @scoped_tool(
        mcp,
        scope=SCOPE_SEARCH,
        name=spec.name,
        title=spec.title,
        description=spec.description,
    )
    async def list_collections() -> ListCollectionsResult:
        user_id = _subject_id()
        async with SessionLocal() as db:
            rows = await db.execute(
                select(
                    Document.id,
                    Document.filename,
                    Document.num_pages,
                    func.count(Chunk.id),
                )
                .join(Chunk, Chunk.document_id == Document.id, isouter=True)
                .where(Document.user_id == user_id, Document.status == "ready")
                .group_by(Document.id)
                .order_by(Document.created_at.desc())
            )
            return ListCollectionsResult(
                documents=[
                    DocumentSummary(
                        document_id=doc_id,
                        filename=filename,
                        pages=pages,
                        passages=passages,
                    )
                    for doc_id, filename, pages, passages in rows.all()
                ]
            )


def _register_answer(mcp: MCPServer, spec: variants.ToolSpec,
                     variant: variants.ToolVariant) -> None:
    """M7 W6 모드 A: 검색이 아니라 **답**을 돌려주는 툴.

    입력 스키마가 ``search_documents`` 에서 ``max_results`` 만 뺀 모양인 것은
    의도다. 이 툴은 컨텍스트 크기를 에이전트가 고를 자리가 아니다 —
    ``RERANK_TOP`` 은 **우리 프롬프트에 맞춰** M4 골든셋으로 고른 값이고
    (``retrieve`` 의 docstring), 그 프롬프트로 답을 쓰는 것이 지금 이 툴이다.
    에이전트가 20을 달라고 하면 우리 생성 프롬프트가 튜닝된 적 없는 컨텍스트로
    답을 쓰게 되고, 그 비용은 사용자가 낸다. 모드 B 에서 그 손잡이가 열려 있는
    이유는 정반대다: 거기서는 컨텍스트를 **에이전트의** 프롬프트가 먹는다.

    ``snippet_chars`` 를 쓰지 않는다. 출력 길이 실험(실험 4)은 에이전트가 읽는
    청크를 자르는 것인데, 이 툴의 응답에서 모델이 읽는 것은 답변 텍스트이고
    청크는 인용 스니펫(240자 고정, ``generate._snippet``)으로만 나간다. 여기에
    같은 손잡이를 다는 것은 다른 것에 같은 이름을 붙이는 일이다.
    """

    @scoped_tool(
        mcp,
        # ``docs:search`` 다. 새 스코프를 만들지 않은 이유: 스코프는 **접근
        # 권한**을 말하는 것이고, 이 툴은 ``docs:search`` 가 이미 허락한 것
        # (이 사용자 문서의 본문을 읽는 것) 위에 산문 한 편을 더 얹을 뿐이다.
        # 검색 스코프를 가진 에이전트는 같은 청크를 받아 같은 답을 스스로 쓸 수
        # 있으므로, 별도 스코프는 권한을 더 좁히지 못하면서 재인가만 요구한다.
        # 이 툴이 더 비싸다는 것은 사실이지만 그것은 **쿼터**가 다루는 문제이고,
        # 쿼터는 이미 같은 칸을 깎는다(``_run_answer``).
        scope=SCOPE_SEARCH,
        name=spec.name,
        title=spec.title,
        description=spec.description,
    )
    async def answer_question(
        question: Annotated[
            str,
            Field(
                min_length=1,
                max_length=2000,
                description="The user's question, in their own words. Not "
                "keywords — the retriever is built for natural questions.",
            ),
        ],
        ctx: Context,
        document_id: Annotated[
            uuid.UUID | None,
            Field(
                default=None,
                description="Restrict the answer to one document, using a "
                "`document_id` from an earlier citation. Omit to use every "
                "document this user has uploaded.",
            ),
        ] = None,
    ) -> AnswerQuestionResult:
        return await _run_answer(
            question=question,
            document_id=document_id,
            client_ip=_client_ip(ctx),
        )


_SEARCH_REGISTRARS = {
    variants.SCHEMA_FULL: _register_search_full,
    variants.SCHEMA_QUERY_ONLY: _register_search_query_only,
    variants.SCHEMA_COLLECTION_ENUM: _register_search_collection_enum,
}


def register(mcp: MCPServer, variant: variants.ToolVariant | None = None) -> None:
    """Register the tools of ``variant`` (default: what production ships)."""
    variant = variant or variants.PRODUCTION
    for spec in variant.tools:
        if spec.role == variants.ROLE_SEARCH:
            _SEARCH_REGISTRARS[variant.schema](mcp, spec, variant)
        elif spec.role == variants.ROLE_FETCH:
            _register_fetch(mcp, spec, variant)
        elif spec.role == variants.ROLE_LIST:
            _register_list_collections(mcp, spec, variant)
        elif spec.role == variants.ROLE_ANSWER:
            _register_answer(mcp, spec, variant)
        else:  # pragma: no cover - 방어용
            raise ValueError(f"unknown tool role {spec.role!r}")
