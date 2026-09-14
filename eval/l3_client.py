"""A minimal MCP client for the L3 harness — modern era (2026-07-28) only.

The agent under test has to reach our tools the way a real client would, so the
harness speaks the wire protocol rather than calling ``tools.py`` directly. A
direct call would skip the auth middleware, the scope gate, the schema
validation and the error envelope, which are four of the things a tool-design
experiment is actually measuring the effect of.

**Why this is not imported from ``tests/mcp_fixture.py``.** That module builds
the same requests, and W7 wrote it first. But it is a pytest fixture module: it
imports ``pytest``, and an eval runner that imports the test package acquires a
test-only dependency for a production-ish job. So the request building lives
here, and ``tests/test_l3_client.py`` asserts byte-for-byte that this module and
``tests/mcp_fixture`` produce the **same** headers and body. The duplication is
paid for by a test rather than by an import, and drift is loud.

## What "modern era" demands

Server-side validation is strict, and getting it wrong answers ``-32020`` or
400 rather than doing something useful:

* ``POST`` to the endpoint path; there is no ``initialize`` handshake.
* ``MCP-Protocol-Version: 2026-07-28``.
* ``Mcp-Method`` must equal the body's ``method``.
* ``Mcp-Name`` must equal ``params.name`` on ``tools/call`` (and is absent
  otherwise).
* ``params._meta`` must carry both ``io.modelcontextprotocol/protocolVersion``
  and ``io.modelcontextprotocol/clientCapabilities``.

Transport is whatever ``httpx.AsyncClient`` was built with, so the same client
works against an ASGI app in-process and against a real URL. The harness uses
the in-process form: it is the same request, and it removes a port and a second
process from a measurement that already has enough moving parts.
"""

import json
from dataclasses import dataclass
from typing import Any

import httpx

PROTOCOL_VERSION = "2026-07-28"

META_PROTOCOL = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"


def body(method: str, params: dict | None = None) -> dict:
    """The JSON-RPC envelope, with the ``_meta`` the modern era requires."""
    merged = dict(params or {})
    merged["_meta"] = {
        META_PROTOCOL: PROTOCOL_VERSION,
        META_CAPABILITIES: {},
    }
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": merged}


def headers(method: str, token: str | None, name: str | None = None) -> dict:
    out = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        out["Mcp-Name"] = name
    if token is not None:
        out["Authorization"] = f"Bearer {token}"
    return out


class McpError(RuntimeError):
    """A transport- or protocol-level failure. Not a tool refusing its work.

    툴이 거절한 것(``isError``)은 여기서 예외가 되지 않는다. 그것은 모델이 읽고
    행동을 바꿔야 하는 **결과**이고, 예외로 만들면 에이전트 루프가 그 턴을
    보지 못한 채 죽는다 — 측정하려던 행동이 바로 그 턴에 있는데도.
    """


@dataclass
class ToolResult:
    is_error: bool
    text: str
    structured: dict | None

    def for_model(self) -> dict:
        """The payload handed back to the model as a function response.

        구조화 출력이 있으면 그것을, 없으면 텍스트를 넘긴다. 오류일 때 ``error``
        키로 감싸는 이유는 Gemini 의 function response 가 임의의 객체라서, 그
        객체 안에 "이건 실패다"라고 적어 주지 않으면 모델이 거절 메시지를 결과
        본문으로 읽기 때문이다.
        """
        if self.is_error:
            return {"error": self.text}
        if self.structured is not None:
            return self.structured
        return {"result": self.text}


def _parse(response: httpx.Response) -> dict:
    """Read the JSON-RPC response, accepting either JSON or an SSE frame.

    같은 엔드포인트가 구성에 따라 둘 중 하나로 답한다. 하나만 읽게 짜 두면
    서버 설정이 바뀌는 날 원인을 알 수 없는 파싱 오류가 난다.
    """
    if response.status_code >= 400:
        raise McpError(f"HTTP {response.status_code}: {response.text[:400]}")
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise McpError("SSE 응답에 data 프레임이 없다")
    return response.json()


class McpClient:
    """Talks to one MCP endpoint as one authenticated user."""

    def __init__(self, http: httpx.AsyncClient, token: str, path: str = "/mcp"):
        self._http = http
        self._token = token
        self._path = path

    async def _rpc(self, method: str, params: dict | None = None,
                   name: str | None = None) -> dict:
        response = await self._http.post(
            self._path,
            json=body(method, params),
            headers=headers(method, self._token, name),
        )
        payload = _parse(response)
        if "error" in payload:
            error = payload["error"]
            raise McpError(f"{error.get('code')}: {error.get('message')}")
        return payload.get("result", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        """The tools this token can see, as wire dicts (name/description/schema).

        ⚠️ ``server/discover`` 는 부르지 않는다 — 서버 안내문을 프롬프트에 넣지
        않는 것이 이 실험의 통제 조건이다(근거는 eval/l3.py 의 SYSTEM_PROMPT).
        """
        result = await self._rpc("tools/list")
        return list(result.get("tools") or [])

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        result = await self._rpc(
            "tools/call", {"name": name, "arguments": arguments}, name=name
        )
        blocks = result.get("content") or []
        text = "\n".join(
            b.get("text", "") for b in blocks if isinstance(b, dict)
        ).strip()
        return ToolResult(
            is_error=bool(result.get("isError")),
            text=text,
            structured=result.get("structuredContent"),
        )
