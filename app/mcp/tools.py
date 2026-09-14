"""The ``search_documents`` tool.

Registered onto an ``MCPServer`` the way a router is included into the FastAPI
app, so ``server.py`` stays a description of the server and this file stays the
one place the tool's behaviour lives.

The body is deliberately thin. Everything that decides whether a search is
allowed and what it is allowed to see — rate limit, quota, scope ownership,
tenant-scoped retrieval, tracing — belongs to ``services.pipeline.QueryRunner``
and is merely driven from here. A check reimplemented in this file would be a
check that can disagree with ``POST /query``, which is exactly the failure the
pipeline module exists to prevent.
"""

import logging
import uuid
from typing import Annotated

from fastapi import HTTPException
from fastapi import status as http_status
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import Field

from app.db import SessionLocal
from app.mcp.descriptions import SEARCH_DOCUMENTS_DESCRIPTION
from app.mcp.schemas import SearchDocumentsResult, SearchHit
from app.models import User
from app.services.pipeline import QueryRunner
from app.services.tracing import SOURCE_MCP_SEARCH

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


def register(mcp: MCPServer) -> None:
    """Register every tool this server exposes (M7 W2: exactly one)."""

    @mcp.tool(
        name="search_documents",
        title="Search the user's documents",
        description=SEARCH_DOCUMENTS_DESCRIPTION,
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
        # 테넌트 격리: 주체는 오직 get_access_token() 에서 온다. 검증기가
        # 세션 행을 읽어 넣은 값이고, 헤더나 인자에서 사용자를 읽는 경로는
        # 이 함수에 존재하지 않는다. document_id 는 사용자가 **이미 소유한**
        # 것 안에서 좁히기만 하며(resolve_scope + retrieve.py 의 조인),
        # 소유하지 않은 id 는 404 — 기존 라우터와 같은 의미로 "없는 것"이다.
        access = get_access_token()
        if access is None or not access.subject:
            # 여기 도달하면 미들웨어가 이미 통과시켰다는 뜻이라 정상적으로는
            # 일어나지 않는다. 그래도 막는다 — 주체 없이 아래로 내려가면
            # 격리가 없는 검색이 된다.
            raise ToolError("authentication required")

        client_ip = _client_ip(ctx)

        # 검증기의 세션은 이미 닫혔다. 여기서 새로 여는 이 세션이 이 요청의
        # 세션이고, async with 가 리트리벌이 몇 초를 쓰든 반납을 보장한다.
        async with SessionLocal() as db:
            user = await db.get(User, uuid.UUID(access.subject))
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
                        content=c.content,
                    )
                    for c in found.chunks
                ],
                searched_document_id=document_id,
            )
