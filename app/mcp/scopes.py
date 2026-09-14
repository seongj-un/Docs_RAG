"""Scopes: what a token may do, and which tools that makes exist.

W7 asks for "스코프별 툴 노출 — 읽기 스코프만 있으면 쓰기 툴은 목록에서 숨긴다".
Today this server has exactly one tool and it is read-only, so there is no write
tool to hide. **The mechanism is built anyway and proved with a fake write tool
in the tests** rather than inventing a real one: a destructive tool that exists
only to demonstrate an access check is a worse thing to ship than a check with
no tool behind it yet. ``tests/test_mcp_scopes.py`` registers one and watches it
disappear.

Two rules hold this together.

**One declaration, both effects.** ``scoped_tool`` is the only way a tool gets
registered here, and it does two things at once: it writes the required scope
into the tool's ``_meta`` (which is what ``ScopeGate`` filters ``tools/list``
on) and it wraps the function so a caller without that scope cannot reach the
body. Hiding a tool from the list is not access control — a client can call any
name it likes — so the two must never drift, and the way to guarantee that is to
make them the same line of code. What the two say differs deliberately, and
``scoped_tool``'s docstring says why.

**A tool that declares nothing is invisible to everyone.** ``ScopeGate`` treats a
missing declaration as "requires a scope nobody holds". Forgetting is therefore
loud in the most convenient way possible: the tool vanishes the first time you
run the suite, instead of quietly being listed to callers who should not see it.
Same judgement as ``session_cookie_secure`` — fail toward the safe side.
"""

import functools
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

logger = logging.getLogger(__name__)

# 읽기/쓰기 두 개로 시작한다. 이름이 `docs:` 접두사인 것은 한 IdP 가 여러
# 리소스 서버의 스코프를 한 토큰에 담는 일이 흔해서다 — "read" 같은 이름은
# 남의 read 와 구별되지 않는다.
SCOPE_SEARCH = "docs:search"
SCOPE_WRITE = "docs:write"

# 이 서버가 아는 스코프 전부. 세션 베어러가 받는 집합이기도 하다(아래 참조).
ALL_SCOPES: frozenset[str] = frozenset({SCOPE_SEARCH, SCOPE_WRITE})

# 툴의 _meta 에 필요한 스코프를 적어 두는 키. MCP 스펙은 _meta 키에 역DNS 형태의
# 접두사를 요구하고 `io.modelcontextprotocol/` 은 스펙 예약분이라, 우리 것에는
# 우리 이름을 쓴다.
REQUIRED_SCOPE_META = "io.docs-rag/requiredScope"


def held_scopes() -> frozenset[str]:
    """The scopes on this request's token. Empty when there is no token.

    유일한 출처가 ``get_access_token()`` 인 것이 중요하다. 이 값은 검증기가
    토큰을 해석해 넣은 것이고, 요청 헤더에서 스코프를 읽는 경로는 이 파일에
    존재하지 않는다 — SDK 주석대로 "Headers are client-supplied input — never
    treat one as an identity assertion".
    """
    access = get_access_token()
    if access is None:
        return frozenset()
    return frozenset(access.scopes or ())


def scoped_tool(
    mcp: MCPServer,
    *,
    scope: str,
    name: str,
    **tool_kwargs: Any,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Register a tool, declaring and enforcing the scope it needs.

    **The refusal says what is missing, and that is on purpose** — unlike an
    unowned ``document_id``, which answers 404 as if it did not exist
    (``pipeline.resolve_scope``). The two cases look similar and are not. A
    document id is another tenant's data, and confirming it exists is the leak.
    A tool name is this server's public capability surface, already written down
    in the README; what the caller actually needs to learn is *which scope to go
    ask for*, because they cannot fix a bare "unknown tool" and will retry it
    forever. That is also what OAuth itself answers in this situation — RFC 6750
    ``insufficient_scope``, which the SDK's own ``RequireAuthMiddleware`` sends
    with the required scope named.

    So the split is: ``tools/list`` hides the tool (a model should not be
    tempted by something it cannot use), and a direct call explains itself. An
    agent that ignores the list and guesses a name learns a tool name it could
    have read in our docs, and nothing about any user.
    """

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def guarded(*args: Any, **kwargs: Any) -> Any:
            if scope not in held_scopes():
                # 사용자 id 도 토큰도 남기지 않는다 — 어느 툴이 어떤 스코프를
                # 요구해서 걸렸는지만으로 운영자는 충분히 진단할 수 있다.
                logger.info("tool %r refused: caller lacks %r", name, scope)
                # ToolError 라야 메시지가 모델에게 도달한다. 일반 예외는
                # "Error executing tool ..." 로 이유가 지워진 채 가고, 모델은
                # 같은 호출을 영원히 재시도한다(tools.py 의 _to_tool_error 와
                # 같은 이유). SDK 가 앞에 "Error executing tool <name>: " 를
                # 붙이는데, 그것까지 지우려면 미들웨어에서 응답 봉투를 손으로
                # 조립해야 해서 사지 않았다 — 이 메시지는 접두사가 붙어도 읽힌다.
                raise ToolError(
                    f"This tool requires the {scope!r} scope and this token does "
                    "not have it. Tell the user they need to re-authorize with "
                    "that scope; retrying now will not succeed."
                )
            return await fn(*args, **kwargs)

        # functools.wraps 가 시그니처·애노테이션을 넘겨주므로 SDK 의 스키마
        # 생성(입력 JSON Schema, ctx 파라미터 제외, 구조화 출력)은 원본 함수를
        # 그대로 본다. 확인함: mcp 2.2.0 에서 감싼 함수와 안 감싼 함수의
        # inputSchema/outputSchema 가 같다.
        return mcp.tool(name=name, meta={REQUIRED_SCOPE_META: scope}, **tool_kwargs)(
            guarded
        )

    return decorator


def _visible(tool: Mapping[str, Any], held: frozenset[str]) -> bool:
    """Whether ``tool`` (a serialized wire dict) may be listed to this caller."""
    meta = tool.get("_meta") or {}
    required = meta.get(REQUIRED_SCOPE_META) if isinstance(meta, Mapping) else None
    # 선언이 없으면 아무도 못 본다. 이 파일의 모듈 docstring 참조 — 잊었을 때
    # 새는 쪽이 아니라 사라지는 쪽으로 실패한다.
    return isinstance(required, str) and required in held


class ScopeGate(ServerMiddleware[Any]):
    """Filter ``tools/list`` down to what this token's scopes allow.

    A context-tier middleware rather than a ``list_tools`` override because the
    SDK's ``MCPServer.list_tools()`` takes no request context — it cannot know
    who is asking. The middleware does, via the auth contextvar the SDK's ASGI
    layer set before the dispatcher ran.

    ⚠️ ``call_next`` hands back the **already-serialized wire dict**, not
    ``ListToolsResult``: ``ServerRunner._inner`` calls ``_serialize`` inside the
    middleware chain (its docstring says so explicitly, so the OTel span can
    observe a bad return shape). That is why this reads ``_meta`` and not
    ``.meta``, and why spreading ``**result`` matters — ``ttlMs`` and
    ``cacheScope`` are already in that dict and rebuilding it from ``tools``
    alone would drop them.

    ``tools/call`` is **not** gated here. It is gated inside the tool by
    ``scoped_tool``, where a refusal can take the same shape as an unknown tool
    without this middleware having to hand-assemble a response envelope (the
    runner does not patch up a short-circuited result: "including its response
    envelope. The pipeline never patches it up after the fact").
    """

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        result = await call_next(ctx)
        if ctx.method != "tools/list" or not isinstance(result, Mapping):
            return result
        tools = result.get("tools")
        if not isinstance(tools, list):  # pragma: no cover - 방어용
            return result

        held = held_scopes()
        return {
            **result,
            "tools": [t for t in tools if isinstance(t, Mapping) and _visible(t, held)],
        }
