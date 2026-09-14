"""Run the M7 W6 generation-site comparison: server generation vs the agent's.

    python -m eval.w6_run measure  --out /tmp/w6_records.jsonl
    python -m eval.w6_run judge    --records /tmp/w6_records.jsonl --out /tmp/w6_l2.jsonl
    python -m eval.w6_run pairwise --records /tmp/w6_records.jsonl --out /tmp/w6_pairwise.json
    python -m eval.w6_run report   --records /tmp/w6_records.jsonl --l2 /tmp/w6_l2.jsonl \\
                                   --pairwise /tmp/w6_pairwise.json

The scoring rules live in ``eval/w6.py`` and call no model. This file is the
half that spends money and time: it indexes the corpus, drives a real agent
against a real MCP endpoint over the wire, and afterwards asks the local judge.

## Why the phases are separate commands

**Latency.** The judge is ``qwen3:4b`` on the same single MPS device the
embedding server and the reranker use (``eval/judge_local.py``'s CONCURRENCY
note). Judging while measuring would put the judge in contention with the very
retrieval whose latency is one of W6's five axes, and this repository has
already decided once that a contaminated time number is worth less than no
number (``eval/l3.py``: "Latency and cost … would describe the contention").
So ``measure`` runs alone, and ``judge`` runs after it has finished.

**Money.** ``measure`` is the paid half. Splitting it off means a judging bug
costs a re-run of the free half, not of the Gemini half.

## What is held fixed across the two modes

Same dataset, same corpus, same account, same system prompt (``eval/l3.py``'s,
which says nothing about tools — the control W4 established), same agent loop,
same model, same throttle. The tool surface is the only thing that differs:
``answer_question`` in mode A, ``search_documents`` in mode B. Mode B's variant
**is the deployed one** (``variants.CLIENT_ANSWER`` is ``PRODUCTION`` renamed),
so the comparison answers "should we add A" rather than "which of two things we
do not ship is better".

Two settings are deliberately *not* production values while this runs, and both
go into every row's provenance:

``SEMANTIC_CACHE_ENABLED=false``
    The dataset asks 44 questions that are near-duplicates by construction
    (``spec-001``..``spec-005`` differ only in the resource name), and the cache
    hits at cosine 0.95. A hit would serve question 1's answer as question 2's
    and both modes would be scored on prose written for a different question.
    The tool itself keeps the cache — this is a measurement condition, not a
    behaviour change.

``LLM_MODEL=$EVAL_LLM_MODEL``
    Mode A generates *inside the server*, through ``settings.llm_model``, which
    is the flagship flash (20 requests/day on the free tier). The whole sweep is
    44 server generations plus ~90 agent turns, so it runs on flash-lite — the
    same model the agent uses, which is also what makes the two modes'
    generation comparable at all. Scoring A on a stronger model than B would
    measure the model, not the site.

## Quota

Both tools spend the user's daily query quota, on purpose. Hitting it mid-sweep
would leave half the conditions running against a tool that answers "you have
hit your search limit", which is a different experiment. Raise the limits for
this account, exactly as W4 did:

    QUOTA_QUERIES_PER_DAY=2000 RATE_LIMIT_QUERY_PER_MIN=120 python -m eval.w6_run measure

## Scale, and the reduction to n=1

Notion asks for numbers, not for a statistical claim. The free tier pays for one
replicate: **44 questions × 2 modes × n=1**. That is enough to say which way a
number moved and to catch a mode that is broken; it is not enough to call a
small gap real. The refusal axis, the one W6 names as the decision criterion,
rests on the six ``no_answer`` items — six. Every table this prints says so.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from google import genai
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.routing import Route

from eval import l2, l3, w6
from eval.indexing import index_pages
from eval.judge_local import JudgeUnavailable, LocalJudge, judge_one, judge_pairwise
from eval.l3_client import McpClient
from eval.scenarios import Scenario
from eval.throttle import Throttle

from app.config import settings
from app.constants import UNUSABLE_PASSWORD_HASH
from app.db import SessionLocal, engine
from app.mcp import variants
from app.mcp.server import MCP_HTTP_METHODS, build_mcp_app, build_mcp_server
from app.mcp.variants import ROLE_SEARCH
from app.models import Chunk, Document, Session, User
from app.services import llm

# W6 전용 계정. L3_USER_ID·EVAL_USER_ID·SEED_USER_ID 와 따로 두는 이유는 W4 와
# 같다 — 공유하면 "어느 문서를 검색했나"가 지난주에 누가 무엇을 인덱싱했는지에
# 좌우된다. 로그인은 영원히 불가능하다.
W6_USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000e716")
W6_USER_EMAIL = "m7-w6-eval@local.invalid"

# 기본 허용 Host 가 로컬호스트뿐이라(app/mcp/server.py) 클라이언트도 그렇게
# 말해야 한다. http://test 로 보내면 421 이다.
BASE_URL = "http://localhost:8000"

DATASET = Path(__file__).resolve().parent / "datasets" / "spec_golden.jsonl"

from eval.corpora import spec  # noqa: E402 - 코퍼스는 설정 읽은 뒤에 부른다

CORPUS = {f"{name}.pdf": pages for name, pages in spec.DOCUMENTS.items()}

# 한 문항이 쓸 수 있는 툴 호출 상한. l3 의 HARD_CALL_CAP 과 같은 성격의 비용
# 차단기이고, 여기서는 채점 기준이 아니다 — W6 은 "몇 번 불렀나"가 아니라
# "누가 답을 썼나"를 잰다.
MAX_CALLS = 4


# --- 환경 ------------------------------------------------------------------


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - 저장소 밖에서도 돌 수 있어야 한다
        return "unknown"


def load_dataset(limit: int = 0) -> list[dict]:
    items = [
        json.loads(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not limit or limit >= len(items):
        return items
    return _stratified(items, limit)


def _stratified(items: list[dict], limit: int) -> list[dict]:
    """유형별 라운드로빈으로 ``limit`` 개를 고른다.

    앞에서 N 개를 자르지 않는다. 이 데이터셋은 유형별로 뭉쳐 있어서 머리를
    자르면 ``no_answer`` 가 통째로 빠지는데, **W6 의 결정 기준이 바로 거부
    정확도**다 — 서버 생성(A)을 병행 제공할 것인가를 그 축으로 정한다. 그리고
    그 축은 ``no_answer`` 문항에서만 나온다. 머리를 자르는 축소는 비용을 줄이는
    대신 이번 주의 결론 자체를 없앤다.

    무작위 표본이 아니라 라운드로빈인 것은 재현 때문이다. 시드를 관리할 필요가
    없고 같은 데이터셋이면 언제 돌려도 같은 문항이 나온다 — 두 모드가 **정확히
    같은 문항**을 봐야 비교가 성립하므로 이 성질이 꼭 필요하다.

    고른 뒤에는 원래 파일 순서로 되돌려 돌려준다. 실행 순서가 유형별로 뭉치면
    레이트 리밋 재시도가 한 유형에만 몰려, 그 유형만 다른 조건에서 돈다.
    """
    buckets: dict[str, list[dict]] = {}
    for item in items:
        buckets.setdefault(item["type"], []).append(item)

    picked: list[dict] = []
    deepest = max(len(bucket) for bucket in buckets.values())
    for depth in range(deepest):
        for name in sorted(buckets):
            bucket = buckets[name]
            if depth < len(bucket):
                picked.append(bucket[depth])
                if len(picked) == limit:
                    chosen = {id(x) for x in picked}
                    return [x for x in items if id(x) in chosen]
    return items


def dataset_sha256() -> str:
    return hashlib.sha256(DATASET.read_bytes()).hexdigest()


def _check_budget(calls: int) -> None:
    """Refuse to start a sweep the quota cannot finish. 근거는 모듈 docstring."""
    needed = int(calls * 1.5) + 10
    if settings.quota_queries_per_day < needed:
        raise SystemExit(
            f"일일 쿼터가 {settings.quota_queries_per_day} 인데 이 스윕은 최대 "
            f"{needed} 회를 쓴다. QUOTA_QUERIES_PER_DAY 를 올려서 다시 부를 것 — "
            "중간에 쿼터로 죽으면 절반의 조건이 다른 실험이 된다."
        )
    if settings.rate_limit_query_per_min < 60:
        raise SystemExit(
            f"분당 한도가 {settings.rate_limit_query_per_min} 이다. "
            "RATE_LIMIT_QUERY_PER_MIN=120 이상으로 올려서 다시 부를 것."
        )
    if not settings.gemini_api_key:
        raise SystemExit(
            "GEMINI_API_KEY 가 비어 있다. 키 없이 낼 수 있는 숫자는 없다 — "
            "지어낸 표보다 '못 쟀다'가 낫다."
        )


async def _ensure_user() -> uuid.UUID:
    """The dedicated account and a fresh session bearer for it."""
    async with SessionLocal() as db:
        user = await db.get(User, W6_USER_ID)
        if user is None:
            user = User(
                id=W6_USER_ID,
                email=W6_USER_EMAIL,
                password_hash=UNUSABLE_PASSWORD_HASH,
                email_verified_at=datetime.now(timezone.utc),
            )
            db.add(user)
        elif user.email_verified_at is None:
            # 미인증이면 계정 수명 전체에 검색 5회가 상한이다(M3).
            user.email_verified_at = datetime.now(timezone.utc)
        await db.commit()

        row = Session(
            user_id=W6_USER_ID,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def _ensure_corpus(reindex: bool) -> None:
    """Index the spec corpus under the W6 account. 이미 있으면 건너뛴다."""
    for name, pages in CORPUS.items():
        async with SessionLocal() as db:
            existing = (
                await db.execute(
                    select(Document).where(
                        Document.user_id == W6_USER_ID, Document.filename == name
                    )
                )
            ).scalars().first()
            ready = existing is not None and existing.status == "ready"
        if reindex or not ready:
            await index_pages(
                list(pages), f"/tmp/w6_{name}", name, owner_id=W6_USER_ID
            )
        async with SessionLocal() as db:
            doc = (
                await db.execute(
                    select(Document).where(
                        Document.user_id == W6_USER_ID, Document.filename == name
                    )
                )
            ).scalars().first()
            count = (
                await db.execute(
                    select(Chunk.id).where(Chunk.document_id == doc.id)
                )
            ).scalars().all()
        print(f"[corpus] {name}: document_id={doc.id} chunks={len(count)}")


async def _chunk_texts(chunk_ids: list[str]) -> dict[str, str]:
    """Full passage text for ids, so the judge sees the same evidence in both modes.

    ⚠️ 이것이 없으면 judge 입력이 비대칭이 된다. 모드 B 의 에이전트는 청크
    **전문**을 받지만, 모드 A 의 툴 응답에는 240자 인용 스니펫만 실린다
    (``generate._snippet``). 스니펫만 넘기면 judge 는 A 의 근거가 잘린 것을
    "근거가 약하다"로 읽고, 우리는 아키텍처가 아니라 응답 모양을 채점하게 된다.
    """
    if not chunk_ids:
        return {}
    ids = [uuid.UUID(c) for c in chunk_ids]
    async with SessionLocal() as db:
        rows = (
            await db.execute(select(Chunk.id, Chunk.content).where(Chunk.id.in_(ids)))
        ).all()
    return {str(cid): content for cid, content in rows}


# --- 계측 ------------------------------------------------------------------


class MeteredThrottle(Throttle):
    """A throttle that remembers how long it made us wait.

    페이싱 수면은 **우리가 무료 티어를 넘지 않으려고 일부러 잔 시간**이다.
    지연 축에 그것까지 넣으면 두 모드의 차이는 설계가 아니라 요청 수의 차이가
    된다. 그래서 잰다 — 빼려면 먼저 재야 한다.
    """

    def __init__(self, rpm: int) -> None:
        super().__init__(rpm)
        self.slept_ms = 0.0

    async def wait(self) -> None:
        started = time.perf_counter()
        await super().wait()
        self.slept_ms += (time.perf_counter() - started) * 1000


class RecordingClient:
    """Wraps ``McpClient`` to keep every tool result and time every call.

    ``l3.run_episode`` 는 호출의 **이름과 인자**만 남긴다(W4 가 재려던 것이
    그것이었다). W6 은 응답 본문이 필요하다 — 모드 A 의 서버 원문·거부 플래그·
    구조화 인용이 거기 있고, 모드 B 의 청크 본문도 거기 있다. 루프를 고쳐
    W4 의 측정을 건드리는 대신 클라이언트를 감싼다: ``run_episode`` 는
    ``call_tool`` 하나만 부르므로 이 래퍼로 충분하다.

    시간도 여기서 잰다. 이 지점이 **서버가 쓴 시간**의 정확한 경계다 — 안쪽은
    검색(+모드 A 의 생성)이고 바깥쪽은 호출자 쪽 모델이다.
    """

    def __init__(self, inner: McpClient) -> None:
        self._inner = inner
        self.results: list[dict] = []
        self.elapsed_ms = 0.0

    @property
    def errors(self) -> list[str]:
        return [r["text"] for r in self.results if r["is_error"]]

    @property
    def all_failed(self) -> bool:
        """Every tool call this episode made came back an error.

        ``run_episode`` 는 툴 오류를 예외로 만들지 않는다(그게 옳다 — 모델이
        읽고 행동을 바꿔야 할 결과다). 그래서 하네스가 보지 않으면 실패한
        에피소드가 "답을 못 쓴 에피소드"와 구별되지 않은 채 표로 들어간다.
        """
        return bool(self.results) and all(r["is_error"] for r in self.results)

    async def list_tools(self):
        return await self._inner.list_tools()

    async def call_tool(self, name: str, arguments: dict):
        started = time.perf_counter()
        result = await self._inner.call_tool(name, arguments)
        elapsed = (time.perf_counter() - started) * 1000
        self.elapsed_ms += elapsed
        self.results.append(
            {
                "name": name,
                "arguments": arguments,
                "is_error": result.is_error,
                "text": result.text,
                "structured": result.structured,
                "ms": round(elapsed, 1),
            }
        )
        return result


def _install_throttled_generation(throttle: Throttle) -> None:
    """Pace the *server's* generation through the same budget as the agent's.

    모드 A 의 생성은 MCP 툴 **안에서** 일어나므로 러너의 throttle 을 지나가지
    않는다. 그대로 두면 에이전트 호출과 서버 생성이 같은 무료 티어 버킷을
    각자 모르는 채 태우고, 스윕은 429 로 죽는다 — 그리고 반쯤 죽은 스윕은
    절반의 조건이 다른 실험이다. 그래서 한 개의 페이서로 묶는다.

    ``app.services.llm.generate`` 를 갈아 끼우는 것이지 ``generate.py`` 를
    고치는 것이 아니다. 프로덕션 코드에 평가용 분기를 넣지 않겠다는 뜻이고,
    ``generate.py`` 가 호출 시점에 속성을 찾으므로 이것으로 충분하다.
    """
    from eval.throttle import call_with_retry

    original = llm.generate

    async def throttled(system_prompt: str, user_prompt: str, *, model=None):
        return await call_with_retry(
            lambda: original(system_prompt, user_prompt, model=model), throttle
        )

    llm.generate = throttled


# --- 측정 ------------------------------------------------------------------


def _scenario(item: dict) -> Scenario:
    """One dataset question as the L3 loop's input.

    ``expected_tool`` 은 채우되 쓰이지 않는다. W6 은 툴 선택을 채점하지 않고
    (모드 A 는 툴이 하나라 언제나 100%다), 이 필드는 ``Scenario`` 가 요구하는
    값이라 넣는 것뿐이다. 그 사실을 ``why`` 에 적어 둔다 — 라벨이 아니라
    자리 채움이라는 것이 나중에 이 파일을 읽는 사람에게 보여야 한다.
    """
    return Scenario(
        id=item["id"],
        utterance=item["question"],
        expected_tool=ROLE_SEARCH,
        kind=item.get("type", "unknown"),
        why="W6 은 툴 선택을 채점하지 않는다 — 이 필드는 자리 채움이다.",
        max_calls=MAX_CALLS,
        min_calls=1,
    )


def _mcp_app(variant: variants.ToolVariant):
    """A live MCP endpoint wired the way ``main.py`` wires the real one."""
    server = build_mcp_server(variant)
    host = FastAPI()
    app = build_mcp_app(server)
    host.router.routes.append(
        Route(settings.mcp_path, endpoint=app, methods=MCP_HTTP_METHODS)
    )
    return server, host


def _server_side(mode: str, results: list[dict]) -> dict:
    """Pull mode-specific facts out of the recorded tool responses.

    모드 A: 서버가 쓴 원문·거부 플래그·구조화 인용. 여러 번 불렀으면
    **마지막** 성공 호출을 쓴다 — 에이전트가 최종적으로 근거로 삼은 것이 그것이다.
    모드 B: 모든 검색 히트의 합집합. 에이전트가 답을 쓸 때 눈앞에 있던 것이
    한 번의 응답이 아니라 그 전부이기 때문이다.
    """
    ok = [r for r in results if not r["is_error"] and isinstance(r["structured"], dict)]
    if mode == w6.MODE_SERVER:
        if not ok:
            return {"server_answer": None, "server_refused": None,
                    "citations": [], "chunk_ids": []}
        last = ok[-1]["structured"]
        citations = list(last.get("citations") or [])
        return {
            "server_answer": last.get("answer"),
            "server_refused": bool(last.get("refused")),
            "citations": citations,
            "chunk_ids": [c["chunk_id"] for c in citations if c.get("chunk_id")],
        }

    hits: list[dict] = []
    seen_ids: set[str] = set()
    for call in ok:
        for hit in call["structured"].get("hits") or []:
            cid = hit.get("chunk_id")
            if cid and cid in seen_ids:
                continue
            if cid:
                seen_ids.add(cid)
            hits.append(hit)
    return {
        "server_answer": None,
        "server_refused": None,
        "citations": hits,
        "chunk_ids": [h["chunk_id"] for h in hits if h.get("chunk_id")],
    }


async def _run_mode(
    *,
    mode: str,
    variant: variants.ToolVariant,
    items: list[dict],
    token: str,
    client: genai.Client,
    model: str,
    throttle: MeteredThrottle,
    provenance: dict,
    out: Path,
    done: set,
    gold_pages: dict[str, set[int]],
) -> None:
    server, host = _mcp_app(variant)
    async with server.session_manager.run():
        transport = ASGITransport(app=host)
        async with AsyncClient(transport=transport, base_url=BASE_URL,
                               timeout=300.0) as http:
            mcp = McpClient(http, token, path=settings.mcp_path)
            listed = await mcp.list_tools()
            names = [t["name"] for t in listed]
            if names != list(variant.tool_names()):
                raise SystemExit(
                    f"{variant.name}: tools/list 가 {names} 를 냈는데 변형은 "
                    f"{list(variant.tool_names())} 를 선언한다. 스코프 선언을 "
                    "빠뜨리면 툴이 조용히 사라진다(app/mcp/scopes.py)."
                )
            declarations = l3.to_declarations(listed)
            base = {
                **provenance,
                "mode": mode,
                "variant": variant.name,
                "tools": names,
                "tool_description_sha256": {
                    t["name"]: hashlib.sha256(
                        (t.get("description") or "").encode("utf-8")
                    ).hexdigest()
                    for t in listed
                },
            }

            consecutive_failures = 0
            for item in items:
                if (mode, item["id"]) in done:
                    continue
                recorder = RecordingClient(mcp)
                slept_before = throttle.slept_ms
                # 벽시계 두 개를 잰다. perf_counter 는 지연 계산용(단조),
                # UTC 시각은 이 에피소드가 남긴 트레이스를 고르는 창의 시작점.
                since = datetime.now(timezone.utc)
                started = time.perf_counter()
                episode = await l3.run_episode(
                    _scenario(item), recorder, declarations,
                    client=client, model=model, throttle=throttle,
                )
                wall_ms = (time.perf_counter() - started) * 1000
                throttle_ms = throttle.slept_ms - slept_before

                row = await _build_row(
                    base=base,
                    item=item,
                    mode=mode,
                    episode=episode,
                    recorder=recorder,
                    gold=gold_pages.get(item["id"], set()),
                    wall_ms=wall_ms,
                    throttle_ms=throttle_ms,
                    since=since,
                )
                # 매 실행마다 저장한다. 한 줄이 곧 지불이 끝난 하나이고,
                # 429 로 죽어도 앞의 것들이 버려지면 안 된다.
                with out.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                mark = "거부" if row["refused"] else "답변"
                print(
                    f"  [{mode}] {item['id']} {item['type']:14} {mark} "
                    f"calls={row['calls']} cite={sorted(row['citation']['cited'])} "
                    f"tok={row['cost']['total']} "
                    f"srv={row['latency']['server_ms']:.0f}ms "
                    f"agt={row['latency']['agent_ms']:.0f}ms"
                    + (f"  ⚠️ 툴 오류: {recorder.errors[0][:70]}"
                       if recorder.errors else "")
                )

                # 툴이 계속 거절하면 멈춘다. **실제로 겪은 사고다**: 스윕
                # 도중에 8099 모델 서버가 죽었고, 44문항 중 42문항이 "답을
                # 못 쓴 에피소드"로 조용히 기록됐다. 툴 오류를 예외로 만들지
                # 않는 것은 에이전트 루프에서는 옳은 판단이지만(모델이 읽고
                # 행동을 바꿔야 할 결과다), 하네스에서는 그 판단이 쓰레기
                # 데이터를 표로 만든다.
                #
                # 한 건에서 멈추지 않는 이유는 정당한 툴 오류가 실제로 있기
                # 때문이다(없는 document_id 등). 연속 3건이면 그것은 문항의
                # 문제가 아니라 환경의 문제다.
                consecutive_failures = (
                    consecutive_failures + 1 if recorder.all_failed else 0
                )
                if consecutive_failures >= 3:
                    raise SystemExit(
                        f"{mode}: 툴 호출이 연속 {consecutive_failures}회 전부 "
                        f"실패했다. 마지막 오류: {recorder.errors[0][:200]}\n"
                        "환경을 고치고 --resume 으로 이어서 돌릴 것 — 이대로 두면 "
                        "나머지 문항이 '답을 못 쓴 문항'으로 표에 들어간다. "
                        f"모델 서버({settings.tei_url})와 Postgres 를 먼저 확인할 것."
                    )


async def _build_row(
    *,
    base: dict,
    item: dict,
    mode: str,
    episode: l3.Episode,
    recorder: RecordingClient,
    gold: set[int],
    wall_ms: float,
    throttle_ms: float,
    since: datetime,
) -> dict:
    side = _server_side(mode, recorder.results)
    seen_pages = w6.pages_of(side["citations"])
    texts = await _chunk_texts(side["chunk_ids"])
    contexts = [texts[c] for c in side["chunk_ids"] if c in texts]

    final_text = episode.final_text or ""
    refused = w6.looks_refused(final_text)
    citation = w6.score_citations(
        final_text, seen_pages=seen_pages, gold_pages=gold, refused=refused
    )

    # 모드 A 에만 있는 칸: 에이전트가 중계하기 **전**의 서버 원문. 두 칸의
    # 차이가 "서버 생성으로 인용을 통제한다"는 주장이 사용자에게 닿는지의 답이다.
    citation_server_text = None
    if mode == w6.MODE_SERVER and side["server_answer"] is not None:
        citation_server_text = w6.score_citations(
            side["server_answer"],
            seen_pages=seen_pages,
            gold_pages=gold,
            refused=bool(side["server_refused"]),
        ).to_dict()

    # 비용. 클라이언트 토큰은 에이전트 루프가 이미 셌다.
    cost = w6.Cost(client_in=episode.tokens_in, client_out=episode.tokens_out)
    # 서버 생성 토큰은 툴 응답 본문에 싣지 않는다 — 그 필드는 에이전트가 읽는
    # 프롬프트가 되고(app/mcp/schemas.py 의 규칙), 우리 회계를 모델에게 읽힐
    # 이유가 없다. 대신 트레이스에서 읽는다. 모드 B 에서 이 값이 0 인 것은
    # **진짜 0** 이지 미측정이 아니다: 서버가 모델을 부르지 않았다.
    if mode == w6.MODE_SERVER:
        cost.server_in, cost.server_out = await _server_tokens(since)

    return {
        **base,
        "question_id": item["id"],
        "type": item.get("type", "unknown"),
        "question": item["question"],
        "reference": item.get("reference_answer"),
        "final_text": final_text,
        "server_answer": side["server_answer"],
        "server_refused": side["server_refused"],
        "refused": refused,
        "contexts": contexts,
        "chunk_ids": side["chunk_ids"],
        "seen_pages": sorted(seen_pages),
        "gold_pages": sorted(gold),
        "citation": citation.to_dict(),
        "citation_server_text": citation_server_text,
        "cost": cost.to_dict(),
        "latency": {
            "wall_ms": round(wall_ms, 1),
            "throttle_ms": round(throttle_ms, 1),
            "server_ms": round(recorder.elapsed_ms, 1),
            "agent_ms": round(max(wall_ms - throttle_ms - recorder.elapsed_ms, 0.0), 1),
            "total_ms": round(max(wall_ms - throttle_ms, 0.0), 1),
        },
        "calls": len(episode.calls),
        "turns": episode.turns,
        "truncated": episode.truncated,
        "failure": episode.failure,
        # 오류 여부까지 남긴다. 이것이 없으면 "툴이 거절했다"와 "모델이 답을
        # 안 썼다"가 레코드에서 구별되지 않는다 — 실제로 한 스윕을 그렇게 잃었다.
        "tool_calls": [
            {"name": c.name, "args": c.args, "is_error": c.is_error,
             "error_text": c.error_text}
            for c in episode.calls
        ],
        "tool_errors": recorder.errors,
    }


async def _server_tokens(since: datetime) -> tuple[int, int]:
    """Generation tokens the server spent during this episode, from its traces.

    툴 응답에 실어 보내지 않는 이유: 그 필드는 에이전트가 읽는 프롬프트가 되고
    (``app/mcp/schemas.py`` 의 규칙), 우리 회계를 모델에게 읽힐 이유가 없다.
    대신 ``traces`` 에서 읽는다 — 그러라고 0015 가 ``mcp_answer`` 를 만들었다.
    이 조회 자체가 "리포트가 사라져도 DB 로 같은 비교를 되살릴 수 있다"는
    주장의 실행 가능한 증인이다.

    **질문 문자열이 아니라 시각으로 고른다.** 에이전트가 사용자의 말을 그대로
    넘긴다는 보장이 없고(툴 description 이 그러라고 말할 뿐이다), 한 글자라도
    바꾸면 문자열 조인은 조용히 0 을 돌려준다 — 토큰이 안 들었다는 뜻으로
    읽히는 0 이다. 시각으로 고르면 에이전트가 한 에피소드에서 두 번 부른
    경우까지 합산된다. 계정이 이 하네스 전용이라(``W6_USER_ID``) 같은 창에
    다른 트래픽이 끼어들 수 없다.
    """
    from app.models import Trace

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Trace.tokens_in, Trace.tokens_out).where(
                    Trace.user_id == W6_USER_ID,
                    Trace.source == w6.TRACE_SOURCE_OF[w6.MODE_SERVER],
                    Trace.created_at >= since,
                )
            )
        ).all()
    return sum(int(r[0]) for r in rows), sum(int(r[1]) for r in rows)


async def cmd_measure(args: argparse.Namespace) -> int:
    items = load_dataset(args.limit)
    _check_budget(len(items) * 2)

    out = Path(args.out)
    done: set = set()
    if args.resume and out.exists():
        done = {
            (r["mode"], r["question_id"]) for r in _read_rows(out)
        }
        print(f"[resume] 이미 끝난 실행 {len(done)}건은 건너뛴다")
    elif out.exists():
        raise SystemExit(
            f"{out} 이 이미 있다. 이어서 돌리려면 --resume, 다시 돌리려면 다른 "
            "--out 을 줄 것 — 덮어쓰면 이미 지불한 실행이 사라진다."
        )

    # 측정 조건 둘. 프로덕션 동작을 바꾸는 것이 아니라 이 실행에만 건다.
    # 근거는 모듈 docstring.
    settings.semantic_cache_enabled = False
    settings.llm_model = settings.eval_llm_model

    token = str(await _ensure_user())
    await _ensure_corpus(args.reindex)

    gold_pages = {
        item["id"]: w6.resolve_gold_pages(item.get("gold_spans") or [], spec.DOCUMENTS)
        for item in items
    }
    missing = [qid for qid, pages in gold_pages.items() if not pages]
    if missing:
        # 조용히 0 으로 두지 않는다 — gold 를 못 되짚은 문항은 인용 정확도의
        # 분모에서 빠지고(citation_metrics), 그 사실이 보여야 한다.
        print(f"[gold] 페이지를 되짚지 못한 문항 {len(missing)}건: {missing[:8]}")

    client = genai.Client(api_key=settings.gemini_api_key)
    throttle = MeteredThrottle(args.rpm)
    _install_throttled_generation(throttle)
    model = settings.eval_llm_model

    provenance = {
        "model": model,
        "server_llm_model": settings.llm_model,
        "system_prompt_sha256": l3.system_prompt_sha256(),
        "dataset": DATASET.name,
        "dataset_sha256": dataset_sha256(),
        # 파일 해시만 남기면 20문항 실행과 44문항 실행이 같은 provenance 를
        # 갖는다. 나중에 두 표를 나란히 놓는 순간 그 비교는 거짓말이 되므로,
        # 실제로 무엇을 돌렸는지를 따로 적는다.
        "dataset_questions_total": len(
            [ln for ln in DATASET.read_text(encoding="utf-8").splitlines() if ln.strip()]
        ),
        "dataset_questions_used": len(items),
        "dataset_subset_ids": [item["id"] for item in items],
        "dataset_subset_types": {
            t: sum(1 for i in items if i["type"] == t)
            for t in sorted({i["type"] for i in items})
        },
        "git_sha": _git_sha(),
        "replicates": 1,
        "quota_queries_per_day": settings.quota_queries_per_day,
        "rate_limit_query_per_min": settings.rate_limit_query_per_min,
        "semantic_cache_enabled": settings.semantic_cache_enabled,
        "hybrid_enabled": settings.hybrid_enabled,
        "cand_k": settings.cand_k,
        "rerank_top": settings.rerank_top,
        "rerank_min_score": settings.rerank_min_score,
        "chunk_size": settings.chunk_size,
        "rpm": args.rpm,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    print(
        f"[run] model={model} prompt={provenance['system_prompt_sha256'][:12]} "
        f"dataset={provenance['dataset_sha256'][:12]} git={provenance['git_sha']} "
        f"문항={len(items)} 모드=2 n=1"
    )

    for mode in w6.MODES:
        variant = variants.get(mode)
        print(f"\n=== {mode} ({', '.join(variant.tool_names())})")
        await _run_mode(
            mode=mode,
            variant=variant,
            items=items,
            token=token,
            client=client,
            model=model,
            throttle=throttle,
            provenance=provenance,
            out=out,
            done=done,
            gold_pages=gold_pages,
        )

    print(f"\n-> {out}")
    report(_read_rows(out), [], None)
    await engine.dispose()
    return 0


# --- 판정 ------------------------------------------------------------------


def _judge_item(row: dict) -> dict:
    return {
        "id": row["question_id"],
        "type": row["type"],
        "question": row["question"],
        "contexts": row["contexts"],
        "answer": row["final_text"],
        "reference": row.get("reference"),
    }


async def cmd_judge(args: argparse.Namespace) -> int:
    rows = _read_rows(Path(args.records))
    if args.limit:
        rows = rows[: args.limit]
    judge = LocalJudge()
    print(f"judge  : {judge.provider} · {judge.model} @ {judge.base_url}")
    print(f"rubric : {l2.RUBRIC_VERSION} ({l2.rubric_sha256()[:12]})")
    print(f"판정   : {len(rows)}건")
    print()
    print(l2.BANNER)
    print()

    records: list[l2.L2Record] = []
    for i, row in enumerate(rows, start=1):
        try:
            record = await judge_one(_judge_item(row), judge, variant=row["mode"])
        except (JudgeUnavailable, ValueError) as exc:
            print(f"[{i}/{len(rows)}] 판정 실패: {exc}")
            if records:
                l2.write_records(args.out, records)
                print(f"여기까지 {len(records)}건을 {args.out} 에 남겼다.")
            return 2
        records.append(record)
        # 문항마다 저장한다. 중간에 죽어도 이미 치른 추론을 버리지 않는다.
        l2.write_records(args.out, records)
        scores = " ".join(f"{v.dimension[:5]}={v.score}" for v in record.verdicts)
        print(
            f"[{i:3}/{len(rows)}] {row['mode']:14} {row['type']:14} "
            f"{scores:34} {record.latency_ms:>6}ms"
        )

    print(f"\n{len(records)} records -> {args.out}")
    return 0


async def cmd_pairwise(args: argparse.Namespace) -> int:
    """Compare the two modes' answers head to head, in both orders.

    W6: "비교 평가는 A/B 순서를 바꿔 두 번 돌린다 (위치 편향)." 그 장치는
    ``judge_pairwise`` 안에 이미 있고, 여기서는 그것을 실제로 쓰고 빠진 문항
    수를 세는 것이 일이다.
    """
    rows = _read_rows(Path(args.records))
    by_question: dict[str, dict[str, dict]] = {}
    for row in rows:
        by_question.setdefault(row["question_id"], {})[row["mode"]] = row

    pairs = [
        (qid, modes)
        for qid, modes in sorted(by_question.items())
        if w6.MODE_SERVER in modes and w6.MODE_CLIENT in modes
    ]
    if args.limit:
        pairs = pairs[: args.limit]
    judge = LocalJudge()
    print(f"judge : {judge.provider} · {judge.model}")
    print(f"비교  : {len(pairs)}문항 × 2순서 = {len(pairs) * 2}회 판정")

    outcomes: list[l2.PairwiseOutcome] = []
    serialized: list[dict] = []
    out = Path(args.out)
    for i, (qid, modes) in enumerate(pairs, start=1):
        a, b = modes[w6.MODE_SERVER], modes[w6.MODE_CLIENT]
        # 컨텍스트는 **두 모드의 합집합**을 정렬해서 넘긴다. 한쪽 것만 주면
        # 다른 쪽의 참인 주장이 근거 없는 것으로 보이고, 정렬하는 이유는 순서에
        # 어느 모드의 것인지가 묻어나지 않게 하기 위해서다.
        contexts = sorted({*a["contexts"], *b["contexts"]})
        item = {
            "id": qid,
            "type": a["type"],
            "question": a["question"],
            "contexts": contexts,
            "reference": a.get("reference"),
        }
        try:
            outcome = await judge_pairwise(
                item,
                {w6.MODE_SERVER: a["final_text"], w6.MODE_CLIENT: b["final_text"]},
                w6.MODE_SERVER,
                w6.MODE_CLIENT,
                judge,
            )
        except (JudgeUnavailable, ValueError) as exc:
            print(f"[{i}/{len(pairs)}] 비교 실패: {exc}")
            break
        outcomes.append(outcome)
        serialized.append(
            {
                "question_id": outcome.question_id,
                "type": a["type"],
                "variant_a": outcome.variant_a,
                "variant_b": outcome.variant_b,
                "pick_ab": outcome.pick_ab,
                "pick_ba": outcome.pick_ba,
                "winner": outcome.winner,
                "position_biased": outcome.position_biased,
            }
        )
        # 매 문항 저장 — judge 두 번이 이미 치러진 값이다.
        out.write_text(
            json.dumps(
                {
                    "rubric_version": l2.RUBRIC_VERSION,
                    "rubric_sha256": l2.rubric_sha256(),
                    "judge_model": judge.model,
                    "summary": l2.pairwise_summary(outcomes),
                    "outcomes": serialized,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        flag = " (위치 편향)" if outcome.position_biased else ""
        print(f"[{i:3}/{len(pairs)}] {qid:10} ab={outcome.pick_ab:14} "
              f"ba={outcome.pick_ba:14} winner={outcome.winner}{flag}")

    print(f"\n{len(outcomes)} outcomes -> {out}")
    print(json.dumps(l2.pairwise_summary(outcomes), ensure_ascii=False, indent=2))
    return 0


# --- 보고 ------------------------------------------------------------------


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _num(value: float | None, fmt: str = "{:.0f}") -> str:
    return "—" if value is None else fmt.format(value)


def report(rows: list[dict], l2_records: list[l2.L2Record], pairwise: dict | None) -> None:
    if not rows:
        print("(레코드 없음)")
        return
    # 저장된 판정을 믿지 않고 증거에서 다시 낸다. 이유는 w6.rescore 의 docstring.
    rows = w6.rescore(rows)
    summary = w6.summarize(rows)
    detector = w6.detector_agreement(rows)
    provenance = rows[0]

    print("\n" + "=" * 96)
    print("M7 W6 · 생성 위치 비교 — A 서버 생성 vs B 컨텍스트만 전달")
    print("=" * 96)
    print(
        f"model={provenance.get('model')} "
        f"dataset={provenance.get('dataset')}({str(provenance.get('dataset_sha256'))[:12]}) "
        f"git={provenance.get('git_sha')} n={provenance.get('replicates')}"
    )
    print(
        f"cache={provenance.get('semantic_cache_enabled')} "
        f"quota={provenance.get('quota_queries_per_day')}/day "
        f"rate={provenance.get('rate_limit_query_per_min')}/min "
        f"cand_k={provenance.get('cand_k')} rerank_top={provenance.get('rerank_top')}"
    )

    # 툴이 거절한 에피소드는 "답을 못 쓴 에피소드"와 다르다. 표 맨 위에 적는
    # 이유는, 한 번 그 둘을 섞은 표를 만들어 봤기 때문이다 — 8099 모델 서버가
    # 스윕 도중 죽었고 42문항이 조용히 빈 답으로 들어갔다.
    broken = [r for r in rows if r.get("tool_errors")]
    if broken:
        print(
            f"\n⚠️ 툴 오류가 난 문항 {len(broken)}건 — 이 표는 그만큼 믿을 수 없다. "
            f"예: [{broken[0]['mode']}/{broken[0]['question_id']}] "
            f"{broken[0]['tool_errors'][0][:90]}"
        )

    # --- 축 3: 거부 정확도 (judge 없이 나온다 — 가장 무겁게 읽을 칸) ---
    print("\n[거부 정확도] ⚠️ judge 를 쓰지 않는다. 라벨과 텍스트만으로 나온다.")
    print(f"{'모드':16}{'no_answer 거부':>16}{'거부율':>9}{'오거부':>12}{'오거부율':>10}")
    for mode in w6.MODES:
        if mode not in summary:
            continue
        r = summary[mode]["refusal"]
        hit = "{}/{}".format(r["refused_on_no_answer"], r["no_answer_n"])
        wrong = "{}/{}".format(r["false_refusals"], r["answerable_n"])
        print(
            f"{mode:16}{hit:>16}{_pct(r['refusal_recall']):>9}"
            f"{wrong:>12}{_pct(r['false_refusal_rate']):>10}"
        )
    for mode in w6.MODES:
        if mode not in summary:
            continue
        wrong = summary[mode]["refusal"]["wrongly_answered_ids"]
        if wrong:
            print(f"  {mode}: 거부했어야 하는데 답한 문항 {wrong}")
    if detector["n"]:
        print(
            f"  거부 탐지기 검증: 모드 A 의 구조화 refused 와 "
            f"{_pct(detector['agreement'])} 일치 (n={detector['n']}, "
            f"과탐 {detector['false_positive']}, 미탐 {detector['false_negative']}). "
            "모드 B 의 거부율은 전적으로 이 탐지기의 값이다."
        )

    # --- 축 2: 인용 정확도 ---
    print("\n[인용 정확도] 두 모드 모두 **답변 텍스트**에서 같은 규칙으로 뽑는다.")
    print(f"{'모드/텍스트':24}{'채점':>6}{'인용함':>9}{'귀속률':>9}{'gold':>7}{'gold 적중':>11}"
          f"{'구조화 인용':>13}")
    for mode in w6.MODES:
        if mode not in summary:
            continue
        c = summary[mode]["citation"]
        print(
            f"{mode + ' (최종)':24}{c['scored_n']:>6}{_pct(c['citation_rate']):>9}"
            f"{_pct(c['attributable']):>9}{c['gold_n']:>7}{_pct(c['gold_hit_rate']):>11}"
            f"{('있음' if summary[mode]['structured_citations'] else '없음'):>13}"
        )
        server_text = summary[mode].get("citation_server_text")
        if server_text:
            print(
                f"{mode + ' (서버 원문)':24}{server_text['scored_n']:>6}"
                f"{_pct(server_text['citation_rate']):>9}"
                f"{_pct(server_text['attributable']):>9}{server_text['gold_n']:>7}"
                f"{_pct(server_text['gold_hit_rate']):>11}{'—':>13}"
            )
    print(f"  ※ 구조화 인용 칸은 측정값이 아니다. {w6.STRUCTURED_CITATIONS_WHY}")

    # --- 축 4: 비용 ---
    print("\n[질의당 비용] 토큰. 모드 B 의 서버 토큰 0 은 **진짜 0** 이지 미측정이 아니다.")
    print(f"{'모드':16}{'서버 평균':>12}{'클라 평균':>12}{'합계 평균':>12}{'전체 합계':>12}")
    for mode in w6.MODES:
        if mode not in summary:
            continue
        c = summary[mode]["cost"]
        print(
            f"{mode:16}{_num(c['server_tokens_mean']):>12}"
            f"{_num(c['client_tokens_mean']):>12}"
            f"{_num(c['total_tokens_mean']):>12}{c['total_tokens_sum']:>12}"
        )

    # --- 축 5: 지연 ---
    print("\n[지연] 페이싱 수면은 뺐다. server=MCP 툴 안, agent=호출자 모델.")
    print(f"{'모드':16}{'server 중앙':>13}{'agent 중앙':>12}{'합계 중앙':>12}{'합계 평균':>12}")
    for mode in w6.MODES:
        if mode not in summary:
            continue
        latency = summary[mode]["latency"]
        print(
            f"{mode:16}{_num(latency['server_ms_median']):>13}"
            f"{_num(latency['agent_ms_median']):>12}"
            f"{_num(latency['total_ms_median']):>12}"
            f"{_num(latency['total_ms_mean']):>12}"
        )

    # --- 축 1: L2 품질 (미검증 judge) ---
    if l2_records:
        print("\n[L2 품질] ⚠️ UNVALIDATED — 아래 수치는 사람 라벨과 대조되지 않았다.")
        grouped: dict[str, list[l2.L2Record]] = {}
        for record in l2_records:
            grouped.setdefault(record.variant, []).append(record)
        keys = [l2.metric_key(d, l2_records[0].validation) for d in l2.DIMENSIONS]
        header = "".join(f"{k.split('__')[0][:14]:>16}" for k in keys)
        print(f"{'모드':16}{'n':>4}{header}")
        for mode in w6.MODES:
            records = grouped.get(mode)
            if not records:
                continue
            agg = l2.aggregate(records)
            cells = "".join(
                f"{('  n/a' if agg.get(k) is None else f'{agg[k]:5.2f}'):>16}"
                for k in keys
            )
            print(f"{mode:16}{len(records):>4}{cells}")
        print(f"  집계 키: {', '.join(keys)}")
        print(f"  {l2.UNVALIDATED_WHY}")
    else:
        print("\n[L2 품질] 판정 결과가 없다 — `python -m eval.w6_run judge` 를 먼저 돌릴 것.")

    # --- 위치 편향 ---
    if pairwise:
        s = pairwise["summary"]
        print("\n[쌍대 비교 · 위치 편향] A/B 순서를 바꿔 두 번 판정했다.")
        print(f"  문항 {s['n']}건 중 승부가 난 것 {s['decided']}건, "
              f"위치 편향으로 결론에서 제외 {s['position_biased']}건 "
              f"({s['position_bias_rate'] * 100:.0f}%)")
        print(f"  승수: {s['wins']}")
        print("  ⚠️ 이 비교도 같은 미검증 judge 의 의견이다.")

    print("\n" + "-" * 96)
    print(
        "⚠️ n=1 이다(Notion 은 반복을 요구하지 않았지만, 무료 티어 예산으로 "
        "반복 없이 갔다). 문항 44개, 그중 거부 판정의 근거인 no_answer 는 "
        "**6개뿐**이다. 6개에서 1건 차이는 17%p 로 보인다 — 절대 수치가 아니라 "
        "방향으로 읽을 것."
    )
    print(
        "⚠️ L2 품질과 쌍대 비교는 사람 라벨과 대조되지 않은 judge(qwen3:4b)의 "
        "값이다. 거부 정확도와 인용 귀속률은 judge 없이 나온 값이므로 결론은 "
        "그쪽에 실어야 한다."
    )


def cmd_report(args: argparse.Namespace) -> int:
    l2_records = l2.read_records(args.l2) if args.l2 and Path(args.l2).exists() else []
    pairwise = (
        json.loads(Path(args.pairwise).read_text(encoding="utf-8"))
        if args.pairwise and Path(args.pairwise).exists()
        else None
    )
    report(_read_rows(Path(args.records)), l2_records, pairwise)
    return 0


# --- main ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eval.w6_run", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    measure = sub.add_parser("measure", help="두 모드를 실제로 돌린다 (돈이 든다)")
    measure.add_argument("--out", default="/tmp/w6_records.jsonl")
    measure.add_argument("--limit", type=int, default=0, help="앞 N문항만")
    measure.add_argument("--rpm", type=int, default=10,
                         help="LLM 요청/분. 에이전트와 서버 생성이 이 하나를 나눈다")
    measure.add_argument("--reindex", action="store_true")
    measure.add_argument("--resume", action="store_true")

    judge = sub.add_parser("judge", help="로컬 judge 로 L2 채점 (measure 뒤에)")
    judge.add_argument("--records", default="/tmp/w6_records.jsonl")
    judge.add_argument("--out", default="/tmp/w6_l2.jsonl")
    judge.add_argument("--limit", type=int, default=0)

    pair = sub.add_parser("pairwise", help="두 모드를 쌍대 비교 (순서 뒤집기)")
    pair.add_argument("--records", default="/tmp/w6_records.jsonl")
    pair.add_argument("--out", default="/tmp/w6_pairwise.json")
    pair.add_argument("--limit", type=int, default=0)

    rep = sub.add_parser("report", help="다섯 축 표")
    rep.add_argument("--records", default="/tmp/w6_records.jsonl")
    rep.add_argument("--l2", default="/tmp/w6_l2.jsonl")
    rep.add_argument("--pairwise", default="/tmp/w6_pairwise.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "measure":
        return asyncio.run(cmd_measure(args))
    if args.cmd == "judge":
        return asyncio.run(cmd_judge(args))
    if args.cmd == "pairwise":
        return asyncio.run(cmd_pairwise(args))
    if args.cmd == "report":
        return cmd_report(args)
    raise SystemExit(f"알 수 없는 명령 {args.cmd!r}")


if __name__ == "__main__":
    raise SystemExit(main())
