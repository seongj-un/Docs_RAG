"""Run the M7 W4 tool-design A/B and print the result table.

    python -m eval.l3_run --out /tmp/l3_records.jsonl
    python -m eval.l3_run --report-only --out /tmp/l3_records.jsonl

Needs Postgres, the embedding/rerank server (search actually runs), and
``GEMINI_API_KEY``. There is no offline mode and no simulated agent: the numbers
are worth reading only if a real model read a real description and called a real
tool over the real wire.

## Scale, and why it is smaller than Notion's

Notion W4 asks for four experiments at n=3. The free tier does not pay for that,
so this runs **two experiments at n=2**: tool decomposition and description
phrasing, the pair Notion itself called the core of the week ("툴 설명이 곧
프롬프트"). ``parameter_schema`` and ``output_length`` exist in
``app/mcp/variants.py`` and have never been executed — ``--experiments`` will
run them the day there is budget, and nothing else has to change.

n=2 is not a sample size that supports a statistical claim. It is enough to say
which way a number moved and to catch a variant that is wildly broken. Every
report this writes says so, and so should anything quoting it.

## Isolation

Everything runs as one dedicated account (``L3_USER_ID``) that owns exactly the
three corpus documents and nothing else. The seed account accumulates leftovers
from every other eval runner, and ``EVAL_USER_ID`` is the L1 harness's corpus —
sharing either would make "which document did it search" depend on what someone
indexed last week. Like both of those, this account can never be logged into.

## Quota

The MCP search tool spends the user's daily query quota, on purpose (see the
quota note in ``app/mcp/tools.py``). A sweep spends a few hundred, so the run
refuses to start unless the limits are raised for it:

    QUOTA_QUERIES_PER_DAY=1000 RATE_LIMIT_QUERY_PER_MIN=60 python -m eval.l3_run

Raising them here is not a shortcut around a product guard: hitting the quota
**mid-sweep** would inject a variable nobody asked for (half the conditions
running against a tool that returns "you have hit your search limit"), and that
is a worse outcome than running with the guard widened for one account that
cannot log in. The effective values go into every record's provenance.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from google import genai
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.routing import Route

from eval import l3
from eval import scenarios as scen
from eval.indexing import index_pages
from eval.l3_client import McpClient
from eval.throttle import Throttle

from app.config import settings
from app.constants import UNUSABLE_PASSWORD_HASH
from app.db import SessionLocal, engine
from app.mcp import variants
from app.mcp.server import (
    MCP_HTTP_METHODS,
    build_mcp_app,
    build_mcp_server,
)
from app.models import Chunk, Document, Session, User

# W4 L3 하네스 전용 계정. SEED_USER_ID·EVAL_USER_ID 와 따로 두는 이유는 모듈
# docstring 참조. 로그인은 영원히 불가능하다(암호 해시가 검증될 수 없는 값).
L3_USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000e713")
L3_USER_EMAIL = "m7-l3-eval@local.invalid"

# 기본 허용 Host 가 로컬호스트뿐이라(app/mcp/server.py) 클라이언트도 그렇게
# 말해야 한다. http://test 로 보내면 421 이다.
BASE_URL = "http://localhost:8000"

from eval.corpora import spec  # noqa: E402 - 코퍼스는 설정 읽은 뒤에 부른다

CORPUS = {f"{name}.pdf": pages for name, pages in spec.DOCUMENTS.items()}


# --- 환경 ------------------------------------------------------------------


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - 저장소 밖에서도 돌 수 있어야 한다
        return "unknown"


def _check_budget(episodes: int) -> None:
    """Refuse to start a sweep the quota cannot finish. 근거는 모듈 docstring."""
    # 에피소드당 검색 1.5회를 넉넉히 잡는다. 정확한 수는 모델이 정하므로 미리
    # 알 수 없고, 모자라서 중간에 죽는 쪽이 여유 있게 잡는 쪽보다 훨씬 비싸다.
    needed = int(episodes * 1.5) + 10
    if settings.quota_queries_per_day < needed:
        raise SystemExit(
            f"일일 쿼터가 {settings.quota_queries_per_day} 인데 이 스윕은 최대 "
            f"{needed} 회를 쓴다. QUOTA_QUERIES_PER_DAY 를 올려서 다시 부를 것 "
            "— 중간에 쿼터로 죽으면 이미 지불한 실행의 절반이 다른 조건과 "
            "비교 불가능해진다."
        )
    if settings.rate_limit_query_per_min < 30:
        raise SystemExit(
            f"분당 한도가 {settings.rate_limit_query_per_min} 이다. "
            "RATE_LIMIT_QUERY_PER_MIN=60 이상으로 올려서 다시 부를 것."
        )
    if not settings.gemini_api_key:
        raise SystemExit(
            "GEMINI_API_KEY 가 비어 있다. 키 없이 낼 수 있는 숫자는 없다 — "
            "지어낸 표보다 '못 쟀다'가 낫다."
        )


async def _ensure_user() -> uuid.UUID:
    """The dedicated account and a fresh session bearer for it."""
    async with SessionLocal() as db:
        user = await db.get(User, L3_USER_ID)
        if user is None:
            user = User(
                id=L3_USER_ID,
                email=L3_USER_EMAIL,
                password_hash=UNUSABLE_PASSWORD_HASH,
                email_verified_at=datetime.now(timezone.utc),
            )
            db.add(user)
        elif user.email_verified_at is None:
            # 미인증이면 계정 수명 전체에 검색 5회가 상한이다(M3). 스윕이
            # 6번째 호출에서 죽는다.
            user.email_verified_at = datetime.now(timezone.utc)
        await db.commit()

        row = Session(
            user_id=L3_USER_ID,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def _ensure_corpus(reindex: bool) -> scen.Resolver:
    """Index the spec corpus under the L3 account, and map the placeholders.

    이미 인덱싱돼 있으면 건너뛴다. 재인덱싱은 임베딩 서버를 60청크만큼 태우는
    일이고, 코퍼스는 이 실험 중에 바뀌지 않는다.
    """
    documents: dict[str, str] = {}
    chunks: dict[str, list[str]] = {}

    for name, pages in CORPUS.items():
        async with SessionLocal() as db:
            existing = (
                await db.execute(
                    select(Document).where(
                        Document.user_id == L3_USER_ID, Document.filename == name
                    )
                )
            ).scalars().first()
            ready = existing is not None and existing.status == "ready"
        if reindex or not ready:
            await index_pages(
                list(pages), f"/tmp/l3_{name}", name, owner_id=L3_USER_ID
            )

        async with SessionLocal() as db:
            doc = (
                await db.execute(
                    select(Document).where(
                        Document.user_id == L3_USER_ID, Document.filename == name
                    )
                )
            ).scalars().first()
            rows = (
                await db.execute(
                    select(Chunk.id)
                    .where(Chunk.document_id == doc.id)
                    .order_by(Chunk.chunk_index)
                )
            ).scalars().all()
        documents[name] = str(doc.id)
        chunks[name] = [str(c) for c in rows]
        print(f"[corpus] {name}: document_id={doc.id} chunks={len(rows)}")

    return scen.Resolver(documents, chunks)


# --- 스윕 ------------------------------------------------------------------


def _mcp_app(variant: variants.ToolVariant):
    """A live MCP endpoint wired the way ``main.py`` wires the real one."""
    server = build_mcp_server(variant)
    host = FastAPI()
    app = build_mcp_app(server)
    host.router.routes.append(
        Route(settings.mcp_path, endpoint=app, methods=MCP_HTTP_METHODS)
    )
    return server, host


def _key(row: dict) -> tuple:
    return (row["experiment"], row["condition"], row["replicate"], row["scenario_id"])


async def _run_condition(
    *,
    experiment: str,
    variant: variants.ToolVariant,
    items: list[scen.Scenario],
    token: str,
    client: genai.Client,
    model: str,
    throttle: Throttle,
    replicates: int,
    provenance: dict,
    out: Path,
    done: set,
) -> None:
    server, host = _mcp_app(variant)
    async with server.session_manager.run():
        transport = ASGITransport(app=host)
        async with AsyncClient(transport=transport, base_url=BASE_URL,
                               timeout=180.0) as http:
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
                "experiment": experiment,
                "condition": variant.name,
                "tools": names,
                # 어떤 문구로 잰 숫자인지 기록에 박는다. 조건 이름만으로는
                # 나중에 문구를 고친 사실이 드러나지 않는다.
                "tool_description_sha256": {
                    t["name"]: hashlib.sha256(
                        (t.get("description") or "").encode("utf-8")
                    ).hexdigest()
                    for t in listed
                },
            }

            for replicate in range(1, replicates + 1):
                for scenario in items:
                    key = (experiment, variant.name, replicate, scenario.id)
                    if key in done:
                        continue
                    episode = await l3.run_episode(
                        scenario, mcp, declarations,
                        client=client, model=model, throttle=throttle,
                    )
                    scored = l3.score(scenario, episode, variant)
                    row = l3.record(
                        provenance={**base, "replicate": replicate},
                        scenario=scenario,
                        episode=episode,
                        scored=scored,
                    )
                    # 매 실행마다 저장한다. 한 줄이 곧 지불이 끝난 하나이고,
                    # 429 로 죽어도 앞의 것들이 버려지면 안 된다.
                    with out.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    mark = "o" if scored.tool_choice else "x"
                    arg = {True: "o", False: "x", None: "-"}[scored.arguments]
                    print(
                        f"  [{experiment}/{variant.name} r{replicate}] "
                        f"{scenario.id} tool={mark} args={arg} "
                        f"calls={scored.calls} turns={scored.turns} "
                        f"first={scored.first_tool}"
                    )


# --- 보고 ------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def report(records: list[dict]) -> None:
    groups: dict[tuple[str, str], list[l3.Score]] = {}
    for row in records:
        groups.setdefault((row["experiment"], row["condition"]), []).append(
            l3.Score(**row["score"])
        )

    print("\n" + "=" * 92)
    print("M7 W4 · 툴 설계 A/B — 기록 표")
    print("=" * 92)
    header = (
        f"{'실험':<22}{'조건':<16}{'툴 선택':>9}{'인자':>9}"
        f"{'평균 호출':>10}{'평균 턴':>9}  메모"
    )
    print(header)
    print("-" * 92)
    for experiment, (a, b) in variants.EXPERIMENTS.items():
        for condition in (a, b):
            rows = groups.get((experiment, condition))
            if not rows:
                continue
            agg = l3.aggregate(rows)
            note = (
                f"n={agg['n']}, 인자 채점 {agg['argument_scored_n']}건, "
                f"불필요 {agg['unnecessary_call_rate'] * 100:.0f}%, "
                f"무툴 문항 호출 {agg['calls_on_no_tool_scenarios']}회"
            )
            if agg["truncated_n"]:
                note += f", 잘림 {agg['truncated_n']}건"
            print(
                f"{experiment:<22}{condition:<16}"
                f"{_pct(agg['tool_choice_accuracy']):>9}"
                f"{_pct(agg['argument_accuracy']):>9}"
                f"{agg['mean_calls']:>10.2f}{agg['mean_turns']:>9.2f}  {note}"
            )
    print("-" * 92)

    print("\n유형별 툴 선택 정확도")
    kinds = sorted({r["kind"] for r in records})
    keys = [k for k in groups if groups[k]]
    print(f"{'유형':<24}" + "".join(f"{c:>18}" for _, c in keys))
    for kind in kinds:
        line = f"{kind:<24}"
        for key in keys:
            rows = [r for r in groups[key] if r.kind == kind]
            line += (
                f"{_pct(sum(r.tool_choice for r in rows) / len(rows)):>18}"
                if rows else f"{'—':>18}"
            )
        print(line)

    tokens = sum(
        r["episode"]["tokens_in"] + r["episode"]["tokens_out"] for r in records
    )
    print(f"\n총 토큰(입력+출력): {tokens}")
    print(
        "⚠️ n=2 다(Notion 은 n=3 을 요구한다 — 무료 티어 예산으로 줄였다). "
        "20문항에서 1~2개 차이는 노이즈다. 절대 수치가 아니라 방향으로 읽을 것."
    )
    print("⚠️ 지연·비용 숫자는 내지 않는다 — 이 맥에서 다른 작업이 함께 돌았다.")


# --- main ------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/l3_records.jsonl")
    parser.add_argument(
        "--experiments",
        default=",".join(variants.MEASURED_EXPERIMENTS),
        help="쉼표 구분. 기본은 실제로 측정한 둘",
    )
    parser.add_argument("--replicates", type=int, default=2)
    parser.add_argument("--rpm", type=int, default=10,
                        help="LLM 요청/분. 무료 티어 flash-lite 기준")
    parser.add_argument("--limit", type=int, default=0, help="앞 N개 시나리오만")
    parser.add_argument(
        "--only", default="", help="쉼표 구분 시나리오 id 만 (디버그용)"
    )
    parser.add_argument("--reindex", action="store_true")
    parser.add_argument("--report-only", action="store_true",
                        help="--out 을 다시 읽어 표만 낸다")
    parser.add_argument("--resume", action="store_true",
                        help="--out 에 이미 있는 (실험,조건,반복,시나리오)는 건너뛴다")
    args = parser.parse_args()

    out = Path(args.out)
    if args.report_only:
        report(l3.load_records(out))
        return

    items = scen.load()
    if args.only:
        wanted = {i.strip() for i in args.only.split(",") if i.strip()}
        items = [s for s in items if s.id in wanted]
        missing = wanted - {s.id for s in items}
        if missing:
            raise SystemExit(f"그런 시나리오가 없다: {sorted(missing)}")
    if args.limit:
        items = items[: args.limit]
    experiments = [e.strip() for e in args.experiments.split(",") if e.strip()]
    for name in experiments:
        if name not in variants.EXPERIMENTS:
            raise SystemExit(f"모르는 실험 {name!r}")

    _check_budget(len(items) * len(experiments) * 2 * args.replicates)

    done: set = set()
    if args.resume and out.exists():
        done = {_key(r) for r in l3.load_records(out)}
        print(f"[resume] 이미 끝난 실행 {len(done)}건은 건너뛴다")
    elif out.exists():
        raise SystemExit(
            f"{out} 이 이미 있다. 이어서 돌리려면 --resume, 다시 돌리려면 "
            "다른 --out 을 줄 것 — 덮어쓰면 이미 지불한 실행이 사라진다."
        )

    token = str(await _ensure_user())
    resolver = await _ensure_corpus(args.reindex)
    items = [resolver.scenario(s) for s in items]

    client = genai.Client(api_key=settings.gemini_api_key)
    throttle = Throttle(args.rpm)
    model = settings.eval_llm_model

    provenance = {
        "model": model,
        "system_prompt_sha256": l3.system_prompt_sha256(),
        "dataset": scen.DEFAULT_PATH.name,
        "dataset_sha256": scen.dataset_sha256(),
        "git_sha": _git_sha(),
        "quota_queries_per_day": settings.quota_queries_per_day,
        "rate_limit_query_per_min": settings.rate_limit_query_per_min,
        "hybrid_enabled": settings.hybrid_enabled,
        "cand_k": settings.cand_k,
        "rerank_top": settings.rerank_top,
        "chunk_size": settings.chunk_size,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    print(f"[run] model={model} prompt={provenance['system_prompt_sha256'][:12]} "
          f"dataset={provenance['dataset_sha256'][:12]} git={provenance['git_sha']}")

    for experiment in experiments:
        for condition in variants.EXPERIMENTS[experiment]:
            variant = variants.get(condition)
            if variant.schema == variants.SCHEMA_COLLECTION_ENUM:
                variant = variant.with_collections(tuple(CORPUS))
            surface = ", ".join(variant.tool_names())
            print(f"\n=== {experiment} / {condition} ({surface})")
            await _run_condition(
                experiment=experiment,
                variant=variant,
                items=items,
                token=token,
                client=client,
                model=model,
                throttle=throttle,
                replicates=args.replicates,
                provenance=provenance,
                out=out,
                done=done,
            )

    report(l3.load_records(out))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
