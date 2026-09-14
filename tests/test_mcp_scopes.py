"""Scope gating and the W7 tenant-isolation checklist. Requires Postgres.

The Notion W7 page lists four things to prove, and all four are here:

1. B 가 A 의 문서 참조를 명시해 질의 → 거부
2. B 의 일반 질의 결과에 A 문서 청크 미포함
3. ``tools/list`` 가 스코프에 따라 다르게 나옴
4. 리스트 응답 ``cacheScope`` 가 private

(1) and (2) also have W2-era witnesses in ``test_mcp_server.py``. They are here
too and deliberately stricter: (1) additionally pins that a refused search
spends no quota and names nothing that exists, and (2) checks the **recorded
retrieval stages**, not only the returned hits — a leak would reach the trace
rows before it reached a response, and that is where it would be visible first
if the cut-off ever moved from the SQL join to the response shaping.

(3) is the interesting one, because this server has exactly one tool and it is
read-only: there is no write tool to hide. W7 asks for the mechanism, not for a
destructive tool to demonstrate it on, so the probe tool below exists only
inside this file. Shipping a real ``delete_documents`` to have something to gate
would be trading a genuine risk for a demonstration.
"""



import pytest
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal
from app.mcp import auth as mcp_auth
from app.mcp.scopes import SCOPE_SEARCH, SCOPE_WRITE, scoped_tool
from tests.mcp_fixture import (
    call,
    call_tool,
    db_available,
    drop_users,
    make_chunk,
    make_session,
    make_user,
    mcp_client,
    run_async,
    search,
    stub_models,  # noqa: F401 - 픽스처를 이 모듈 이름공간으로 끌어온다
)

pytestmark = pytest.mark.skipif(not db_available(), reason="Postgres not reachable")

PROBE_TOOL = "delete_documents"


def _with_probe_write_tool(server):
    """Register a write-scoped tool that exists only for this test module.

    ``scoped_tool`` 은 프로덕션 툴이 쓰는 것과 **같은** 함수다. 다른 경로로
    등록했다면 이 파일은 실제로 쓰이지 않는 메커니즘을 시험하게 된다.
    """

    @scoped_tool(
        server,
        scope=SCOPE_WRITE,
        name=PROBE_TOOL,
        title="Probe",
        description="Test-only probe tool. Deletes nothing.",
    )
    async def _probe() -> str:
        # 정말로 아무것도 하지 않는다. 이 함수에 도달했다는 사실만이 신호다.
        return "reached"


@pytest.fixture
def read_only_sessions(monkeypatch):
    """Make session bearers read-only, so a scope difference is observable.

    세션 토큰은 정의상 계정 전체다(app/mcp/auth.py 의 SESSION_SCOPES). 즉 세션
    으로는 "읽기만 가진 클라이언트"를 만들 수 없다 — 그건 OAuth 가 주는 것이고
    tests/test_mcp_oauth.py 가 그 경로를 다룬다. 여기서는 게이트 자체가 목적이라
    세션의 스코프 집합만 좁혀서 같은 판정 경로를 태운다.
    """
    monkeypatch.setattr(mcp_auth, "SESSION_SCOPES", [SCOPE_SEARCH])


# --- (3) tools/list 가 스코프에 따라 다르다 --------------------------------


def test_read_scope_does_not_see_the_write_tool(read_only_sessions):
    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "mcp-readonly")
            session_id = await make_session(db, user)

        async with mcp_client(extra_tools=_with_probe_write_tool) as client:
            listed = await call(client, "tools/list", str(session_id))
            called = await call_tool(client, str(session_id), PROBE_TOOL)
            missing = await call_tool(client, str(session_id), "no_such_tool_at_all")

        await drop_users(user)
        return listed, called, missing

    listed, called, missing = run_async(scenario)

    result = listed.json()["result"]
    assert [t["name"] for t in result["tools"]] == ["search_documents"]

    # (4) 같은 응답에서 확인한다. public 이면 공유 캐시가 한 사용자의 목록을
    # 다른 사용자에게 줄 수 있고, 그 목록이 **이제 스코프에 따라 달라지므로**
    # 이 단언은 W2 때보다 더 무거워졌다.
    assert result["cacheScope"] == "private"
    assert result["ttlMs"] == settings.mcp_tools_cache_ttl_ms

    # 목록에서 숨기는 것은 접근 통제가 아니다 — 이름은 클라이언트가 지어낼 수
    # 있다. 호출도 막혀야 한다.
    refused = called.json()["result"]
    assert refused["isError"] is True

    # 그리고 그 거절은 **무엇이 모자란지 말해야** 한다. 모르면 모델은 같은
    # 호출을 영원히 재시도하고, 사용자는 재인가하면 된다는 것을 영영 모른다.
    # 남의 document_id 를 404 로 덮는 것과 다른 판단인 근거는
    # app/mcp/scopes.py 의 scoped_tool docstring 에 있다.
    message = refused["content"][0]["text"]
    assert SCOPE_WRITE in message
    assert "re-authorize" in message

    # 진짜로 없는 툴은 SDK 가 다른 문구로 답한다. 둘이 섞이지 않는지 확인한다 —
    # 게이트가 꺼지면 위 호출이 이 문구로 바뀌면서 테스트가 먼저 빨개진다.
    assert (
        missing.json()["result"]["content"][0]["text"]
        == "Unknown tool: no_such_tool_at_all"
    )


def test_write_scope_sees_and_reaches_the_write_tool():
    """세션은 계정 전체이므로 기본 스코프 집합에 쓰기가 들어 있다."""

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "mcp-write")
            session_id = await make_session(db, user)

        async with mcp_client(extra_tools=_with_probe_write_tool) as client:
            listed = await call(client, "tools/list", str(session_id))
            called = await call_tool(client, str(session_id), PROBE_TOOL)

        await drop_users(user)
        return listed, called

    listed, called = run_async(scenario)
    assert sorted(t["name"] for t in listed.json()["result"]["tools"]) == [
        PROBE_TOOL,
        "search_documents",
    ]
    result = called.json()["result"]
    assert result["isError"] is False
    assert result["content"][0]["text"] == "reached"


def test_an_undeclared_tool_is_invisible_to_everyone():
    """선언을 잊으면 새는 쪽이 아니라 사라지는 쪽으로 실패한다.

    ``mcp.tool`` 을 직접 써서 스코프 선언 없이 등록한 툴은 아무에게도 보이지
    않는다. 조용히 모두에게 노출되는 것보다, 첫 실행에서 툴이 없어져 눈에 띄는
    편이 낫다 — session_cookie_secure 와 같은 방향의 판단이다.
    """

    def _undeclared(server):
        @server.tool(name="forgot_to_declare", description="x")
        async def _f() -> str:  # pragma: no cover - 도달하면 테스트가 실패한다
            return "leaked"

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "mcp-undeclared")
            session_id = await make_session(db, user)

        async with mcp_client(extra_tools=_undeclared) as client:
            listed = await call(client, "tools/list", str(session_id))

        await drop_users(user)
        return listed

    names = [t["name"] for t in run_async(scenario).json()["result"]["tools"]]
    assert names == ["search_documents"]


def test_anonymous_caller_holds_no_scope():
    """토큰이 없으면 스코프도 없다 — 전송 계층에서 이미 401 이어야 한다."""

    async def scenario():
        async with mcp_client(extra_tools=_with_probe_write_tool) as client:
            return await call(client, "tools/list", None)

    assert run_async(scenario).status_code == 401


# --- (1) 남의 문서를 명시하면 거부 ------------------------------------------


def test_naming_another_tenants_document_is_refused_and_costs_nothing(stub_models):
    async def scenario():
        async with SessionLocal() as db:
            alice = await make_user(db, "w7-alice")
            bob = await make_user(db, "w7-bob")
            bob_session = await make_session(db, bob)
            alice_chunk = await make_chunk(db, alice, "앨리스만 볼 수 있는 내용")

        async with mcp_client() as client:
            response = await search(
                client,
                str(bob_session),
                question="앨리스만 볼 수 있는 내용",
                document_id=str(alice_chunk.document_id),
            )

        async with SessionLocal() as db:
            spent = (
                await db.execute(
                    text(
                        "SELECT count(*) FROM usage_events WHERE user_id = :uid"
                    ).bindparams(uid=bob.id)
                )
            ).scalar_one()

        await drop_users(alice, bob)
        return response, spent, alice_chunk

    response, spent, alice_chunk = run_async(scenario)
    result = response.json()["result"]
    assert result["isError"] is True

    message = result["content"][0]["text"]
    # 기존 라우터와 같은 의미의 "없는 것". 403 이었다면 그 응답 자체가 앨리스의
    # 문서가 존재한다는 확인이 된다.
    assert "no such document" in message.lower()
    assert str(alice_chunk.document_id) not in message
    assert "앨리스" not in message

    # 일어나지 않은 검색에 쿼터가 매겨지면, 남의 id 를 훑는 것만으로 남의
    # 한도가 아니라 자기 한도가 타긴 하지만 — 그보다 중요한 건 실패한 요청이
    # 어떤 자원도 잡지 않는다는 계약이다(routers/query.py 와 같은 약속).
    assert spent == 0


# --- (2) 일반 질의에 남의 청크가 하나도 섞이지 않는다 ------------------------


def test_a_general_question_never_returns_a_foreign_chunk(stub_models):
    """반환 결과뿐 아니라 **기록된 리트리벌 단계**까지 본다.

    격리는 SQL 조인이 지킨다(retrieve.py). 그 조인이 느슨해지고 응답 모양이
    마침 가려 주는 상태가 되면, 응답만 보는 테스트는 초록으로 남는다 —
    trace_chunks 는 파이프라인이 실제로 **본** 것을 적으므로 거기서 먼저 보인다.
    """

    async def scenario():
        async with SessionLocal() as db:
            alice = await make_user(db, "w7-leak-a")
            bob = await make_user(db, "w7-leak-b")
            bob_session = await make_session(db, bob)
            alice_chunks = [
                await make_chunk(db, alice, f"앨리스 문서 {i}: 계약 해지 조건", page=i + 1)
                for i in range(5)
            ]
            bob_chunk = await make_chunk(db, bob, "밥 문서: 계약 해지 조건")

        async with mcp_client() as client:
            # 상한까지 달라고 한다. 좁게 물으면 남의 청크가 있어도 우연히 안
            # 나올 수 있다.
            response = await search(
                client, str(bob_session), question="계약 해지 조건", max_results=20
            )

        async with SessionLocal() as db:
            staged = [
                str(row[0])
                for row in (
                    await db.execute(
                        text(
                            "SELECT chunk_id FROM trace_chunks tc "
                            "JOIN traces t ON t.id = tc.trace_id "
                            "WHERE t.user_id = :uid"
                        ).bindparams(uid=bob.id)
                    )
                ).all()
            ]

        await drop_users(alice, bob)
        return response, staged, alice_chunks, bob_chunk

    response, staged, alice_chunks, bob_chunk = run_async(scenario)

    hits = response.json()["result"]["structuredContent"]["hits"]
    assert [h["chunk_id"] for h in hits] == [str(bob_chunk.id)]

    forbidden = {str(c.id) for c in alice_chunks}
    assert forbidden.isdisjoint({h["chunk_id"] for h in hits})
    # 파이프라인이 본 것 전체에도 없다.
    assert forbidden.isdisjoint(set(staged))
    assert staged == [str(bob_chunk.id)]


# --- (4) cacheScope --------------------------------------------------------


def test_tools_list_cache_scope_is_private(stub_models):
    """W2 부터 private 이었다. W7 은 그것을 **고정**한다.

    목록이 스코프에 따라 달라진 지금은 더 이상 "정적이라 새어 나갈 것이 없다"가
    아니다 — public 으로 바뀌는 순간, 쓰기 스코프를 가진 사용자의 목록이 중간
    캐시를 거쳐 읽기 전용 사용자에게 그대로 간다.
    """

    async def scenario():
        async with SessionLocal() as db:
            user = await make_user(db, "w7-cache")
            session_id = await make_session(db, user)

        async with mcp_client(extra_tools=_with_probe_write_tool) as client:
            return_value = await call(client, "tools/list", str(session_id))

        await drop_users(user)
        return return_value

    result = run_async(scenario).json()["result"]
    assert result["cacheScope"] == "private"
