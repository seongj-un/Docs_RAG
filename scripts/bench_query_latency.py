"""실제 사용자가 실제로 기다리는 시간을 실제 HTTP API로 잰다.

## 왜 이 스크립트가 필요한가

이 프로젝트에 지금까지 기록된 지연시간 숫자는 전부 청크가 40~76자인 평가용
코퍼스(``eval_corpus_wide``/``eval_corpus_hard``/``golden_*``/``lease_test``/
``big60``)에서 나왔다. 그런데 이 DB에 실제로 들어 있는 진짜 문서의 청크
중앙값은 **1,295자**다. 리랭킹 비용은 후보 "개수"가 아니라 입력 "총 길이"에
지배되므로 — bge-reranker 는 (질문, 후보) 쌍을 통째로 인코딩한다 — 짧은
픽스처에서 잰 숫자는 실제 사용을 설명하지 못한다.

``app/config.py`` 의 ``cand_k`` 주석에 오늘 자로 남은 직접 측정:

    같은 CAND_K=20 에서   53자 청크 →   474ms
                       1,295자 청크 → 9,409ms

거의 20배 차이다. 이 저장소는 같은 함정에 이미 한 번 빠졌다(RERANK_MAX_CHARS
실험이 짧은 픽스처 때문에 "변화 없음"으로 나왔던 것). 그래서 이 스크립트는
**반드시** ``documents.filename LIKE '01.%'`` 문서(인공지능기본법 관련 논문,
55청크, 청크 길이 중앙값 1,295자)로만 잰다 — 코드 안에서 강제한다(아래
``_assert_realistic`` 참고). 다른 코퍼스로 재고 싶어지면, 그게 바로 이
스크립트가 존재하는 이유를 잊은 것이다.

## 무엇을, 어떻게 재는가

서비스 함수를 직접 부르지 않는다. 실제 서버(``:8000``)에 실제 HTTP 요청을
보낸다 — 사용자가 기다리는 것은 라우팅·인증·직렬화까지 포함한 전체 요청이지,
파이프라인 내부 함수 하나가 아니기 때문이다.

- ``POST /query`` (원샷): 클라이언트 벽시계 총 시간 + 서버가 ``traces`` 테이블에
  이미 기록해 두는 단계별 시간(embed/retrieve/rerank/generate/total).
- ``POST /conversations/{id}/query`` (스트리밍, SSE): 위와 같은 서버 단계별
  시간에 더해, ``traces`` 에는 없는 값 — **첫 토큰까지의 시간(TTFT)** —
  을 클라이언트에서 직접 잰다. 사용자가 "응답이 시작됐다"고 느끼는 시점은
  전체 완료 시점이 아니라 이 시점이다.
- 시맨틱 캐시 히트: 같은 질문을 곧바로 다시 물어 cold(최초)/warm(캐시 히트)
  쌍으로 만든다. 캐시는 (user_id, document_id) 로 스코프되므로(services/cache.py)
  자기 자신과의 코사인 유사도는 항상 1.0 — 임계값 0.95 를 넘는다.

새로 계측하지 않는다: ``traces`` 테이블이 이미 단계별 시간을 기록해 두므로
(app/services/tracing.py), 실행이 끝난 뒤 ``GET /traces`` 로 **읽어만** 온다.

## 인증을 어떻게 하는가

대상 문서(01.*)는 이미 이 DB의 실제 계정이 소유하고 있다. 그 계정 비밀번호는
모르고, 새 문서를 새 계정에 재업로드하면(원본 PDF가 storage/ 에 남아있지도
않고) "이미 인덱싱된 그 문서"가 아니라 다른 문서가 된다. 그래서
``app/services/auth.create_session`` 이 하는 것과 정확히 같은 일 — ``sessions``
테이블에 행 하나 — 을 직접 만든다. 비밀번호도, 다른 어떤 데이터도 건드리지
않는다(그 계정의 코드/설정은 물론 다른 행도 손대지 않는다). 측정이 끝나면 이
스크립트가 만든 세션 행만 지운다 — 측정으로 생긴 traces/usage_events/
query_cache 행은 측정 결과 그 자체이므로 남긴다(기존 소유 계정에 실제 질의가
28건 쌓이고, 일일 쿼터 200건 중 일부를 씀).

## GPU 락

MPS 는 이 맥에 하나뿐이고 다른 작업과 공유된다. 시간을 재는 구간(HTTP 요청을
보내는 구간)만 아래 프로토콜의 락을 잡는다 — 설정(문서 조회, 세션 생성,
스키마 확인)은 GPU 를 쓰지 않으므로 락이 필요 없다:

    <스크래치패드>/mps-lock.md

``--lock-path`` 로 락 파일 위치를 바꿀 수 있다(세션마다 스크래치패드 경로가
달라지므로, 다음에 재실행할 때는 그때의 mps-lock.md 가 가리키는 경로를 넘길 것).

이 스크립트의 첫 실행이 바로 락 프로토콜의 결함을 실증했다: v1 은 "20분 넘게
갱신 없고 PID 죽었으면 회수"였는데, 다른 에이전트가 락에 적어둔 PID 는 락을
잡은 **셸**의 PID(v1 이 시킨 대로)였다. 그 셸은 끝났어도 실제 추론 프로세스는
백그라운드에서 계속 돌고 있었고, 이 스크립트는 "PID 죽음 + 20분 경과"만 보고
회수해 GPU 를 같이 쓰기 시작했다 — 결과로 콜드 질의 하나가 embed_ms=104177
(104초), rerank_ms=37896, total_ms=148943 로 기록됐다(이 문서의 정상 범위는
embed 수백ms~6초, rerank 수백ms~19초). 명백히 오염된 값이라 버렸다. v2 는
시간 유예를 없애고 "적힌 PID 가 지금 살아있는가"만 본다(``acquire_lock`` 참고) —
이 스크립트는 재시도 없이 자기 프로세스 안에서 측정하므로, 적어두는 PID 는
처음부터 실제 추론을 요청하는 프로세스의 PID 다.

## 실행

    .venv/bin/python -m scripts.bench_query_latency
    .venv/bin/python -m scripts.bench_query_latency --dry-run   # 설정만, LLM 비용 없음
    .venv/bin/python -m scripts.bench_query_latency --n-cold-query 4 --n-cold-stream 3

``LLM_MODEL`` 호출(=Gemini 실비용)은 cold 요청 수만큼만 나간다 — warm(캐시
히트)은 LLM 을 타지 않는다. 기본값(8+6=14 cold)은 스프레드를 말할 수 있는
최소한이면서 무료 티어 쿼터를 하루치로 다 쓰지 않는 선.
"""

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

# 대상 문서. 이 프리픽스가 정확히 하나의, 실제 길이의 문서만 가리키는지
# _assert_realistic 에서 다시 확인한다 — 실수로 짧은 코퍼스를 집으면 안 된다.
DOC_PREFIX = "01."
MIN_REALISTIC_MEDIAN_CHARS = 500  # eval 코퍼스(30~77자)와 확실히 구분되는 문턱

DEFAULT_LOCK_PATH = (
    "/private/tmp/claude-501/-Users-seongjun-Desktop-project-Docs-RAG/"
    "d7ae1254-b636-42f4-a773-dac3e6eff156/scratchpad/mps.lock"
)

# 실제 문서 내용(강명수, "인공지능기본법의 개선방향에 관한 고찰")에서 근거를
# 확인하고 뽑은 질문. /query 와 스트리밍이 서로 다른 질문 집합을 쓰는 이유:
# 시맨틱 캐시는 (user_id, document_id) 스코프라 엔드포인트를 가리지 않는다 —
# 같은 질문을 두 경로에서 다시 쓰면 두 번째 경로의 "cold" 가 사실은 첫 번째
# 경로가 이미 채워둔 캐시의 "warm" 이 되어버린다.
ONE_SHOT_QUESTIONS = [
    "인공지능기본법은 총 몇 개의 장과 몇 개의 조문으로 구성되어 있나요?",
    "국가인공지능전략위원회는 언제, 어떤 법적 근거로 출범했나요?",
    "EU AI법은 몇 개의 장과 조항, 부속서로 구성되어 있나요?",
    "EU AI법의 고위험 AI 시스템 규정은 언제부터 시행되나요?",
    "미국의 AI 행동계획은 몇 개의 정책과 연방조치를 제시하나요?",
    "중국은 AI 의료기기 관리를 위해 어떤 규범을 마련했나요?",
    "사업자 개념에 배포자(Deployer)를 추가하자는 제안의 취지는 무엇인가요?",
    "인공지능기본법 개선 방향으로 저자가 제시한 정책 방향은 무엇인가요?",
]
STREAM_QUESTIONS = [
    "인공지능기본법의 벌칙 조항은 몇 조부터 몇 조까지인가요?",
    "개발사업자와 이용사업자는 어떻게 구분되나요?",
    "EU가 AI법 시행을 유예한다고 밝힌 이유는 무엇인가요?",
    "생성형 AI는 어떤 기준으로 분류될 수 있나요?",
    "미국 AI 행동계획은 언제 발표되었나요?",
    "피지컬 AI 분과 구성과 관련해 어떤 지적이 있었나요?",
]
# 이 문서 스코프에 이미 있던 캐시 항목(과거 실사용) — 절대 그대로 재사용하지
# 않는다. 재사용하면 "cold" 로 표시한 첫 호출이 사실은 이미 캐시 히트라서
# 콜드/웜의 의미가 뒤바뀐다.
_PRE_EXISTING_CACHED = {"문서 요약해줘", "인공지능 윤리에 대해 설명해줘"}
assert not (set(ONE_SHOT_QUESTIONS) | set(STREAM_QUESTIONS)) & _PRE_EXISTING_CACHED


# --------------------------------------------------------------------------
# MPS 락 — mps-lock.md v2. v1 은 "20분 넘게 갱신 없고 PID 죽었으면 회수"였는데,
# 이 스크립트의 첫 실행에서 바로 그 v1 결함에 당했다: 다른 에이전트(main-candk)의
# 락 파일에 적힌 PID 는 락을 잡은 셸의 PID(v1 이 시킨 대로 `$$`)였고, 그 셸이
# 끝난 뒤에도 실제 추론 프로세스는 백그라운드에서 계속 GPU 를 쓰고 있었다.
# 이 스크립트가 "20분 지났고 그 PID 죽었다"만 보고 회수해 GPU 를 같이 쓰기
# 시작했고, 결과: 콜드 질의 하나가 embed_ms=104177(104초!), rerank_ms=37896,
# total_ms=148943 로 기록됐다(정상 범위는 이 문서에서 embed 수백ms~6초,
# rerank 수백ms~19초) — 명백한 오염이라 그 trace/그때 만든 cache 행은 버렸다
# (아래 "실행" 절 및 latency-report.md 참고). v2 는 그래서 시간 기반 유예를
# 완전히 없앤다: 적힌 PID 가 살아있는 한 나이와 무관하게 절대 회수하지 않고,
# 죽었으면(kill -0 상당) 나이와 무관하게 바로 회수한다 — "얼마나 오래"가 아니라
# "지금 진짜로 그 프로세스가 있는가"만 본다. 이 스크립트는 별도 자식 프로세스로
# 포크하지 않고 측정을 하는 바로 이 프로세스 안에서 하므로, 여기 적는
# os.getpid() 는 처음부터 "실제로 추론을 요청하는 프로세스"의 PID 다.
# --------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 다른 소유자 프로세스가 살아있다는 뜻
    return True


def acquire_lock(path: str, who: str = "bench_query_latency") -> None:
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as fh:
                fh.write(f"{os.getpid()} {who} {time.strftime('%H:%M:%S')}\n")
            print(f"[lock] 확보: {path}")
            return

        try:
            holder = open(path).read().strip()
            holder_pid = int(holder.split()[0])
        except (FileNotFoundError, ValueError, IndexError):
            continue  # 그 사이 풀렸거나 손상 — 다음 루프에서 다시 시도

        # v2: 나이는 보지 않는다. 적힌 PID 가 지금 살아있는지만 본다 — 살아있으면
        # 아무리 오래돼도 회수하지 않고, 죽었으면 방금 생겼어도 바로 회수한다.
        if not _pid_alive(holder_pid):
            print(f"[lock] PID {holder_pid} 죽음 확인 — 회수한다: {holder!r}")
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            continue

        print(f"[lock] 다른 작업이 보유 중이고 살아있다({holder!r}), 30초 후 재확인")
        time.sleep(30)


def release_lock(path: str) -> None:
    try:
        os.remove(path)
        print(f"[lock] 반납: {path}")
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------
# 설정 단계 (락 불필요 — GPU 를 쓰지 않는다)
# --------------------------------------------------------------------------

@dataclass
class TargetDoc:
    owner_id: uuid.UUID
    doc_id: uuid.UUID
    filename: str
    n_chunks: int
    median_len: float
    max_len: int


async def find_target_document(engine) -> TargetDoc:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, user_id, filename FROM documents "
                    "WHERE filename LIKE :prefix ORDER BY created_at"
                ),
                {"prefix": f"{DOC_PREFIX}%"},
            )
        ).mappings().all()
        if not rows:
            raise SystemExit(
                f"'{DOC_PREFIX}' 로 시작하는 문서가 DB 에 없다 — 측정 대상이 없다."
            )
        if len(rows) > 1:
            print(f"[경고] '{DOC_PREFIX}' 로 시작하는 문서가 {len(rows)}개 — "
                  f"가장 오래된 것을 쓴다: {[r['filename'] for r in rows]}")
        row = rows[0]
        doc_id, owner_id, filename = row["id"], row["user_id"], row["filename"]

        stats = (
            await conn.execute(
                text(
                    "SELECT count(*) n, "
                    "percentile_cont(0.5) WITHIN GROUP (ORDER BY length(content)) median_len, "
                    "max(length(content)) max_len "
                    "FROM chunks WHERE document_id = :doc_id"
                ),
                {"doc_id": doc_id},  # UUID 객체 그대로 — str() 로 바꾸지 않는다
            )
        ).mappings().one()

    return TargetDoc(
        owner_id=owner_id, doc_id=doc_id, filename=filename,
        n_chunks=stats["n"], median_len=float(stats["median_len"] or 0),
        max_len=int(stats["max_len"] or 0),
    )


def _assert_realistic(doc: TargetDoc) -> None:
    """이 스크립트가 존재하는 이유를 코드로 강제한다.

    청크 길이 중앙값이 짧으면(eval 코퍼스 수준) 조용히 계속 진행하지 않고
    바로 멈춘다 — "재보니 빠르던데?"가 실은 잘못된 코퍼스에서 잰 결과였던
    이 프로젝트의 전례를 반복하지 않기 위해서다.
    """
    if doc.median_len < MIN_REALISTIC_MEDIAN_CHARS:
        raise SystemExit(
            f"'{doc.filename}' 의 청크 길이 중앙값이 {doc.median_len:.0f}자로 "
            f"너무 짧다(임계값 {MIN_REALISTIC_MEDIAN_CHARS}자). eval 코퍼스급 "
            "픽스처로 보인다 — 이 스크립트는 실제 길이 문서 전용이다. 중단."
        )


async def create_session(engine, user_id: uuid.UUID) -> uuid.UUID:
    """auth.create_session 과 완전히 같은 모양의 행을 직접 심는다.

    이 계정의 비밀번호를 모르고(로그인 불가), 원본 PDF 도 storage/ 에 남아있지
    않아 재업로드로 "같은" 문서를 새로 만들 수도 없다. sessions 테이블은 세션
    쿠키가 곧 이 행의 id 인 구조라(app/services/auth.py), 실제 로그인 한 번과
    구분되지 않는 유효한 세션을 이 방법으로만 얻을 수 있다. 비밀번호도 다른
    행도 건드리지 않는, 순수하게 추가적인 동작이다.
    """
    session_id = uuid.uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.session_ttl_days)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sessions (id, user_id, expires_at, created_at) "
                "VALUES (:id, :user_id, :expires_at, now())"
            ),
            {"id": session_id, "user_id": user_id, "expires_at": expires_at},
        )
    return session_id


async def delete_session(engine, session_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_id})


@dataclass
class Setup:
    doc: TargetDoc
    session_id: uuid.UUID | None


async def do_setup(database_url: str, *, make_session: bool = True) -> Setup:
    engine = create_async_engine(database_url)
    try:
        doc = await find_target_document(engine)
        _assert_realistic(doc)
        session_id = await create_session(engine, doc.owner_id) if make_session else None
        return Setup(doc=doc, session_id=session_id)
    finally:
        await engine.dispose()


async def do_cleanup(database_url: str, session_id: uuid.UUID) -> None:
    engine = create_async_engine(database_url)
    try:
        await delete_session(engine, session_id)
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------
# 시간 측정 — 실제 HTTP 왕복. 서비스 함수를 직접 부르지 않는다.
# --------------------------------------------------------------------------

@dataclass
class CallResult:
    question: str
    path: str        # "query" | "stream"
    warm: bool       # 두 번째(반복) 호출인가
    ok: bool
    status: int | None = None
    elapsed_s: float | None = None   # 클라이언트 벽시계 총 시간
    ttft_s: float | None = None      # 스트리밍만: 첫 token 이벤트까지
    cached: bool | None = None       # 서버가 알려준 경우만(스트리밍의 meta/done)
    refused: bool | None = None
    error: str | None = None


def timed_query(client: httpx.Client, question: str, document_id: uuid.UUID, *, warm: bool) -> CallResult:
    t0 = time.perf_counter()
    try:
        resp = client.post("/query", json={"question": question, "document_id": str(document_id)})
    except httpx.HTTPError as exc:
        return CallResult(question, "query", warm, False, error=repr(exc),
                           elapsed_s=time.perf_counter() - t0)
    elapsed = time.perf_counter() - t0
    if resp.status_code != 200:
        return CallResult(question, "query", warm, False, status=resp.status_code,
                           elapsed_s=elapsed, error=resp.text[:300])
    data = resp.json()
    return CallResult(question, "query", warm, True, status=200, elapsed_s=elapsed,
                       refused=data.get("refused"))


def timed_stream(
    client: httpx.Client, conversation_id: uuid.UUID, question: str, *, warm: bool
) -> CallResult:
    t0 = time.perf_counter()
    first_token_t: float | None = None
    done_t: float | None = None
    cached: bool | None = None
    refused: bool | None = None
    error: str | None = None
    status: int | None = None

    try:
        with client.stream(
            "POST", f"/conversations/{conversation_id}/query",
            json={"question": question},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            status = resp.status_code
            if status != 200:
                error = resp.read().decode("utf-8", "replace")[:300]
            else:
                event_name = None
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if line.startswith("event: "):
                        event_name = line[len("event: "):]
                        continue
                    if line.startswith("data: "):
                        now = time.perf_counter()
                        if event_name == "token" and first_token_t is None:
                            first_token_t = now
                        if event_name == "meta":
                            cached = json.loads(line[len("data: "):]).get("cached")
                        elif event_name == "done":
                            refused = json.loads(line[len("data: "):]).get("refused")
                            done_t = now
                            break
                        elif event_name == "error":
                            error = line[len("data: "):][:300]
                            done_t = now
                            break
    except httpx.HTTPError as exc:
        return CallResult(question, "stream", warm, False, error=repr(exc),
                           elapsed_s=time.perf_counter() - t0)

    ok = status == 200 and error is None and done_t is not None
    return CallResult(
        question, "stream", warm, ok, status=status,
        elapsed_s=(done_t - t0) if done_t else None,
        ttft_s=(first_token_t - t0) if first_token_t else None,
        cached=cached, refused=refused, error=error,
    )


# --------------------------------------------------------------------------
# 실행 로그 출력
# --------------------------------------------------------------------------

def _log(r: CallResult) -> None:
    tag = f"{r.path}/{'warm' if r.warm else 'cold'}"
    if not r.ok:
        print(f"  [FAIL {tag:11}] {r.question[:30]:30} status={r.status} err={r.error}")
        return
    extra = f" ttft={r.ttft_s:6.2f}s" if r.ttft_s is not None else ""
    cached = f" cached={r.cached}" if r.cached is not None else ""
    print(
        f"  [ OK  {tag:11}] {r.question[:30]:30} "
        f"total={r.elapsed_s:6.2f}s{extra}{cached} refused={r.refused}"
    )


# --------------------------------------------------------------------------
# 분석 — traces 테이블을 GET /traces 로 읽어만 온다(재계측하지 않는다)
# --------------------------------------------------------------------------

def fetch_traces(client: httpx.Client) -> list[dict]:
    resp = client.get("/traces", params={"limit": 200})
    resp.raise_for_status()
    return resp.json()


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def stage_stats(rows: list[dict], field_name: str) -> dict | None:
    vals = [r[field_name] for r in rows if r.get(field_name) is not None]
    if not vals:
        return None
    return {"n": len(vals), "median": statistics.median(vals), "max": max(vals)}


def client_stats(results: list[CallResult], field_name: str) -> dict | None:
    vals = [getattr(r, field_name) for r in results if r.ok and getattr(r, field_name) is not None]
    if not vals:
        return None
    return {"n": len(vals), "median": statistics.median(vals), "max": max(vals)}


def _fmt_ms(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0f}ms"


def _fmt_s(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.2f}s"


def print_stage_group(title: str, rows: list[dict]) -> None:
    print(f"\n  {title} (n={len(rows)})")
    for stage in ("embed_ms", "retrieve_ms", "rerank_ms", "generate_ms", "total_ms"):
        s = stage_stats(rows, stage)
        if s is None:
            print(f"    {stage:12} 기록 없음(이 경로에서 건너뛴 단계)")
        else:
            print(f"    {stage:12} median {_fmt_ms(s['median']):>9}   worst {_fmt_ms(s['max']):>9}   (n={s['n']})")


# --------------------------------------------------------------------------
# 메인
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--lock-path", default=DEFAULT_LOCK_PATH)
    parser.add_argument("--n-cold-query", type=int, default=len(ONE_SHOT_QUESTIONS))
    parser.add_argument("--n-cold-stream", type=int, default=len(STREAM_QUESTIONS))
    parser.add_argument("--pace", type=float, default=1.5,
                         help="요청 사이 대기(초) — 캐시 히트가 빨라서 분당 한도를 태울 수 있다")
    parser.add_argument("--dry-run", action="store_true",
                         help="문서/계정만 확인하고 끝. 락도 안 잡고 LLM 도 안 부른다.")
    args = parser.parse_args()

    one_shot = ONE_SHOT_QUESTIONS[: args.n_cold_query]
    stream_qs = STREAM_QUESTIONS[: args.n_cold_stream]

    print("=" * 70)
    print("실제 사용자 지연시간 측정 — 실제 HTTP API, 실제 길이 문서")
    print("=" * 70)

    setup = asyncio.run(do_setup(settings.database_url, make_session=not args.dry_run))
    doc = setup.doc
    print(f"\n대상 문서: {doc.filename}")
    print(f"  청크 {doc.n_chunks}개, 길이 중앙값 {doc.median_len:.0f}자, 최대 {doc.max_len}자")
    print(f"  소유 계정 user_id={doc.owner_id} (기존 실제 계정 — 세션만 새로 만든다)")

    if args.dry_run:
        print(f"\n[dry-run] 실행 계획: /query cold+warm {len(one_shot)}쌍, "
              f"스트리밍 cold+warm {len(stream_qs)}쌍 "
              f"(LLM 호출 {len(one_shot)+len(stream_qs)}건 예정). "
              "세션도 만들지 않았다 — 부작용 없이 종료.")
        return

    client = httpx.Client(
        base_url=args.base,
        cookies={settings.session_cookie_name: str(setup.session_id)},
        timeout=240.0,  # embed/rerank 각각 최대 120s(하드코딩) + generate — 여유 있게
    )

    try:
        health = client.get("/health")
        assert health.status_code == 200, f"백엔드 /health 실패: {health.status_code}"

        conv = client.post(
            "/conversations",
            json={"title": "latency bench", "scope_document_id": str(doc.doc_id)},
        )
        assert conv.status_code == 201, f"대화 생성 실패: {conv.status_code} {conv.text[:200]}"
        conversation_id = conv.json()["id"]
        print(f"측정용 대화 생성: {conversation_id}")

        results: list[CallResult] = []
        n_llm_calls = 0

        acquire_lock(args.lock_path)
        run_start = datetime.now(timezone.utc)
        try:
            print(f"\n[{len(one_shot)}쌍] POST /query — cold 직후 동일 질문 warm")
            for q in one_shot:
                cold = timed_query(client, q, doc.doc_id, warm=False)
                _log(cold); results.append(cold); n_llm_calls += 1
                time.sleep(args.pace)
                warm = timed_query(client, q, doc.doc_id, warm=True)
                _log(warm); results.append(warm)
                time.sleep(args.pace)

            print(f"\n[{len(stream_qs)}쌍] POST /conversations/{{id}}/query (SSE) — cold 직후 warm")
            for q in stream_qs:
                cold = timed_stream(client, conversation_id, q, warm=False)
                _log(cold); results.append(cold); n_llm_calls += 1
                time.sleep(args.pace)
                warm = timed_stream(client, conversation_id, q, warm=True)
                _log(warm); results.append(warm)
                time.sleep(args.pace)
        finally:
            release_lock(args.lock_path)  # 실패해도 반드시 반납

        # ---- 분석: traces 를 읽기만 한다 ----
        traces = fetch_traces(client)
        our_qs = set(one_shot) | set(stream_qs)
        window = [
            t for t in traces
            if t["question"] in our_qs and _parse_dt(t["created_at"]) >= run_start
        ]
        print(f"\n이번 실행 구간에 기록된 trace: {len(window)}건 "
              f"(요청 {len(results)}건 대비 — 트레이싱은 best-effort 라 적을 수 있다)")

        query_rows = [t for t in window if t["question"] in one_shot]
        stream_rows = [t for t in window if t["question"] in stream_qs]

        print("\n" + "=" * 70)
        print("서버 측 단계별 시간 (traces 테이블, ms) — 사용자가 못 보는 내부 분해")
        print("=" * 70)
        print_stage_group("/query · cold", [t for t in query_rows if not t["cached"]])
        print_stage_group("/query · warm(cache hit)", [t for t in query_rows if t["cached"]])
        print_stage_group("streaming · cold", [t for t in stream_rows if not t["cached"]])
        print_stage_group("streaming · warm(cache hit)", [t for t in stream_rows if t["cached"]])

        print("\n" + "=" * 70)
        print("클라이언트 체감 시간 (실제 HTTP 왕복, 초) — 사용자가 실제로 기다리는 것")
        print("=" * 70)
        for label, path, warm in (
            ("/query cold", "query", False), ("/query warm", "query", True),
            ("streaming cold", "stream", False), ("streaming warm", "stream", True),
        ):
            subset = [r for r in results if r.path == path and r.warm == warm]
            total = client_stats(subset, "elapsed_s")
            ttft = client_stats(subset, "ttft_s")
            n_fail = sum(1 for r in subset if not r.ok)
            line = f"  {label:16}"
            line += f" total median {_fmt_s(total['median']) if total else 'n/a':>7} worst {_fmt_s(total['max']) if total else 'n/a':>7}"
            if ttft:
                line += f"   TTFT median {_fmt_s(ttft['median']):>7} worst {_fmt_s(ttft['max']):>7}"
            if n_fail:
                line += f"   [실패 {n_fail}건]"
            print(line)

        # 캐시 이득: 클라이언트 체감 total 기준 cold/warm 비율
        print("\n" + "=" * 70)
        print("시맨틱 캐시 이득 (실제 콘텐츠 기준)")
        print("=" * 70)
        for label, path in (("/query", "query"), ("streaming", "stream")):
            cold = client_stats([r for r in results if r.path == path and not r.warm], "elapsed_s")
            warm = client_stats([r for r in results if r.path == path and r.warm], "elapsed_s")
            if cold and warm and warm["median"] > 0:
                print(f"  {label:10} cold median {_fmt_s(cold['median'])} -> "
                      f"warm median {_fmt_s(warm['median'])}  ({cold['median']/warm['median']:.1f}x)")

        cold_totals = [t for t in window if t["total_ms"] and not t["cached"]]
        worst_row = max(cold_totals, key=lambda t: t["total_ms"], default=None)
        print("\n" + "=" * 70)
        if worst_row:
            print(f"전체 cold 중 최악(worst) total_ms: {worst_row['total_ms']} "
                  f"— 질문: {worst_row['question']!r}")
        else:
            print("전체 cold 중 최악(worst) total_ms: 기록된 cold trace 없음")
        print(f"이번 실행에서 쓴 실제 LLM(Gemini) 호출: {n_llm_calls}건 "
              f"(모델={settings.llm_model}), HTTP 요청 총 {len(results)}건")
        print("=" * 70)

    finally:
        client.close()
        asyncio.run(do_cleanup(settings.database_url, setup.session_id))
        print("\n[정리] 이 스크립트가 만든 세션 행 삭제 완료"
              "(질의/trace/cache 행은 측정 결과이므로 남겨둔다).")


if __name__ == "__main__":
    main()
