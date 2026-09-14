# 문서Q&A RAG 봇

PDF를 올리고 자연어로 물으면 **근거 페이지를 인용해** 답한다. 문서에 없는 것은
지어내지 않고 모른다고 답한다. 회원별로 문서가 격리되고, 답변은 토큰 단위로
스트리밍된다.

- **검색:** dense(pgvector) + sparse(BGE-M3 lexical) → RRF 융합 → 크로스인코더 리랭킹
- **생성:** Gemini, `[p.N]` 인용 + "모른다" 가드레일
- **멀티유저:** 세션 쿠키 인증, 테넌트 격리, rate limit·쿼터·시맨틱 캐시
- **관측:** 단계별 지연·사용량을 Postgres에 기록, `/admin/stats`로 조회
- **평가:** 라벨링 코퍼스로 검색 구성을 정량 비교하는 하네스(`eval/`)

로드맵과 마일스톤 스펙의 원본은 저장소가 아니라 **Notion**이다. 이 파일은
"어떻게 돌리는가"와 "왜 이렇게 돼 있는가"만 다룬다.

## 빠른 시작 (Docker)

```bash
cp .env.example .env     # GEMINI_API_KEY 를 채운다
docker compose up -d     # db · 모델 · 앱 · 프론트 · 프록시
open http://localhost:8088
```

**애플 실리콘에서는 `scripts/stack.sh up` 을 쓴다.** 모델 서버까지 함께 띄운다:

```bash
scripts/stack.sh up      # 호스트 MPS 모델 서버 + 컨테이너 스택
scripts/stack.sh status
scripts/stack.sh down    # 둘 다 정지 (볼륨은 남긴다)
```

컨테이너 CPU 로 리랭킹하면 **실제 크기 청크 50개에 184초**가 걸려 클라이언트
타임아웃(120초)을 넘겨 503 `search unavailable` 이 된다. 같은 작업이 호스트
MPS 에서는 10.6초다(스레드를 늘려도 CPU 는 나아지지 않는다). 그래서 맥에서는
모델 서버만 컨테이너 밖에 두고, `.env` 의 `MODEL_URL` 이 그쪽을 가리킨다.

**프론트와 API 가 한 오리진이다.** 프록시가 `/` 는 프론트로, `/api/*` 는
백엔드로 보낸다(접두사는 떼고 넘기므로 백엔드는 자기가 하위 경로에 붙어
있다는 사실을 모른다). 그래서

- **CORS 가 아예 없다** — 브라우저가 보기에 출처가 하나다.
- **프론트 이미지에 호스트 주소가 박히지 않는다.** `NEXT_PUBLIC_*` 은 런타임이
  아니라 빌드 시점에 번들에 굳는데, 값이 상대 경로(`/api`)라 localhost 든
  도메인이든 같은 이미지가 그대로 돈다.

첫 기동은 모델 가중치를 받는다(`hf-cache` 볼륨에 약 6.9GB로 남아 이후엔 즉시).
그동안에도 앱은 이미 떠 있고, 질의는 503 `search unavailable`로 답한다 —
모델을 기다리느라 스택 전체가 멎지는 않는다.

| 변수 | 기본 | 뜻 |
| --- | --- | --- |
| `HTTP_PORT` / `HTTPS_PORT` | 8088 / 8443 | 프록시 호스트 포트. 운영에선 80/443 |
| `SITE_ADDRESS` | `:80` | 도메인을 넣으면 Caddy가 인증서를 자동 발급 |
| `ADMIN_TOKEN` | (빈 값) | 비우면 `/admin/stats`가 404 |
| `MODEL_THREADS` | 4 | 모델 컨테이너의 CPU 스레드 |
| `MAIL_PROVIDER` | `console` | 기본값인 `console`이면 가입 인증 메일이 서버 로그에만 찍히고 **아무에게도 발송되지 않는다.** 실제로 보내려면 `resend`로 바꾸고 `RESEND_API_KEY`를 채운다 |
| `APP_BASE_URL` | `http://localhost:3000` | 인증 메일 링크가 이 값 뒤에 `/verify#token=…`을 붙여 조립된다(토큰이 `?`가 아니라 `#` 뒤에 있는 건 의도다 — 아래 '로그는 어디에 있나'). **배포 도메인으로 바꾸지 않으면 모든 가입자가 죽은 링크를 받는다** — 재발송도 같은 죽은 링크를 다시 보낼 뿐이다 |

**모델 서비스는 기본적으로 CPU로 돈다.** macOS Docker는 리눅스 VM에서 돌고
Metal이 전달되지 않아, 컨테이너는 이 맥의 GPU를 쓸 수 없다. NVIDIA
호스트라면 `docker compose --profile gpu up`으로 임베딩·리랭킹 각각을 TEI
컨테이너(`tei`·`tei-rerank`)로 돌릴 수 있다 — 둘이 필요한 이유와 정확한
명령은 아래 "실제 호스트에 배포하기"에 있다. 애플 실리콘에서 GPU로 돌리려면
아래 "로컬 GPU 모델 서버"를 쓴다.

정리는 `docker compose down`. **`-v`는 붙이지 말 것** — `pgdata`(DB)·
`hf-cache`(가중치 6.9GB)·`uploads`(업로드 원본)가 함께 사라진다.

## 개발 실행

### 백엔드 (호스트)
```bash
docker compose up -d db        # Postgres만 컨테이너로
pip install -r requirements.txt
uvicorn app.main:app --reload  # 시작 시 alembic 마이그레이션 자동 적용
```
`/docs`에서 스키마를 볼 수 있다. 스키마 변경은 Alembic으로:
`alembic revision --autogenerate -m "..."` → `alembic upgrade head`.

### 로컬 GPU 모델 서버 (macOS / Apple Silicon)
```bash
pip install -r requirements-bench.txt   # torch·FlagEmbedding (앱 의존성 아님)
python -m scripts.local_model_server     # http://127.0.0.1:8081, MPS 자동 선택
```
`.env`의 `TEI_URL`·`RERANK_URL`을 이 주소로 두면 **앱 코드는 그대로다** — 이
서버가 TEI와 같은 `/embed`·`/embed_full`·`/rerank` 계약을 구현한다.
앱은 torch를 임포트하지 않고 HTTP로만 부르므로 `requirements.txt`는 건드리지
않는다(합치면 프로덕션 이미지에 2GB가 쓸모없이 붙는다).

### 프론트엔드
```bash
cd web
npm install
cp .env.example .env.local   # NEXT_PUBLIC_API_BASE
npm run dev                  # http://localhost:3000
```
`NEXT_PUBLIC_API_BASE` 는 무엇을 보느냐에 따라 다르다 — 컨테이너 스택이면
`http://localhost:8088/api`, 호스트 uvicorn 이면 `http://localhost:8000`.
개발 서버는 배포본과 다른 오리진(:3000)이므로 **여기서는 CORS 가 필요하다**:
백엔드의 `cors_origins` 에 이 오리진이 있어야 세션 쿠키가 오간다.
Turbopack dev가 포트를 잡지 못하는 환경에서는 `npm run dev:webpack`.

## 구조

```
app/
├── main.py           # 앱 · lifespan(run_migrations) · CORS
├── config.py         # 모든 튜너블 (pydantic-settings)
├── models.py         # Document, Chunk, User, Session, Trace, Conversation ...
├── deps.py           # 인증 의존성
├── routers/          # admin auth chunks conversations documents query traces usage
├── mcp/              # MCP 서버 — 같은 앱에 마운트된다
│   ├── server.py     # 서버·ASGI 앱 조립 · 인증 모드 · 캐시 힌트
│   ├── tools.py      # search_documents (QueryRunner 를 그대로 탄다)
│   ├── auth.py       # 세션 행 베어러 검증기
│   ├── oauth.py      # OAuth 리소스 서버 — JWT/JWKS 검증. 발급은 하지 않는다
│   ├── scopes.py     # 스코프 선언·강제 + tools/list 필터
│   ├── schemas.py    # 툴 입출력 (모델이 읽는다)
│   └── descriptions.py # 툴 설명 = 프롬프트. W4 가 A/B 한다
└── services/
    ├── pipeline.py   # 질의 1건의 전 과정 — /query 와 SSE 가 공유
    ├── ingest.py     # 파싱 → 청킹 → 임베딩 → 저장
    ├── chunking.py   # BGE-M3 토크나이저 기반 토큰 윈도우(원문 substring 보존)
    ├── retrieve.py   # dense / hybrid 검색 — 테넌트 격리가 여기 산다
    ├── fusion.py     # RRF
    ├── rerank.py     # 크로스인코더
    ├── generate.py   # 프롬프트 · 인용 · 거부 가드레일
    ├── llm.py        # Gemini 클라이언트
    ├── embeddings.py # BGE-M3 (TEI 계약)
    ├── upstream.py   # 모델 서버 장애를 도메인 예외로
    ├── cache.py      # 시맨틱 캐시
    ├── ratelimit.py  # 인메모리 토큰버킷
    ├── usage.py      # DB 집계 쿼터
    ├── tracing.py    # 단계별 지연 기록 (제품의 기록: traces 테이블)
    └── otel.py       # OpenTelemetry 스팬 (운영자의 기록). 기본 no-op
alembic/versions/     # 마이그레이션 7개
web/                  # Next.js 16 App Router · TypeScript · Tailwind v4 (+ Dockerfile)
eval/                 # 검색 품질 평가 하네스 + 라벨링 코퍼스
scripts/              # 로컬 모델 서버 · e2e 스모크 · 벤치마크
tests/                # pytest (Postgres 없으면 통합 테스트는 스킵)
```

**파이프라인이 한 곳인 이유:** `/query`와 대화 스트리밍이 `pipeline.QueryRunner`를
공유한다. 한쪽에만 걸린 제한은 차이가 아니라 구멍이다.

## API

| 엔드포인트 | 설명 |
| --- | --- |
| `POST /auth/signup` · `/auth/login` · `/auth/logout` · `GET /auth/me` | 세션 쿠키 인증 |
| `POST /documents` | multipart PDF 업로드 → 202 `{id, filename, status}` |
| `GET /documents` · `GET /documents/{id}` · `DELETE /documents/{id}` | 목록 · 상태 · 삭제(청크·파일 함께) |
| `POST /query` | `{question, document_id?, hybrid?}` → `{answer, refused, citations[]}` |
| `POST /conversations` · `GET /conversations` | 대화 생성 · 목록 |
| `GET /conversations/{id}` · `DELETE /conversations/{id}` | 이력 · 삭제 |
| `POST /conversations/{id}/query` | SSE 스트리밍 질의 |
| `GET /chunks/{id}` | 청크 원문 (근거 모달용, 소유자 한정) |
| `GET /usage` | 오늘 질문 수 · 이번 달 쪽수와 각 한도 |
| `GET /traces` · `GET /traces/{id}` | 질의 진단 기록 |
| `GET /admin/stats` | 운영 통계 (헤더 `X-Admin-Token`) |
| `GET /health` | 공개 |
| `POST /mcp` | MCP 서버 (Streamable HTTP). 아래 참조 |

컨테이너 스택에서는 전부 `/api` 아래에 붙는다 — `/api/health`, `/api/query` …

## MCP 서버 — 에이전트로 붙이기 (M7 W2·W7)

Claude·Cursor 같은 에이전트가 **우리 검색을 직접 쓰게** 하는 엔드포인트다.
별도 프로세스가 아니라 같은 FastAPI 앱에 마운트돼 있으므로, 앱이 떠 있으면
이미 떠 있다.

노출하는 툴은 `search_documents` **하나뿐이다.** 이 툴은 **검색까지만** 한다 —
청크를 돌려주고, 답을 쓰는 것은 붙인 에이전트다. 생성까지 MCP 로 내보낼지는
W6 에서 정한다.

### 1. 세션 토큰 얻기

새 토큰 체계는 없다. **웹에서 쓰는 그 세션**을 그대로 쓰고, 운반 수단만
쿠키에서 `Authorization: Bearer` 로 바뀐다. 즉 로그아웃하면 에이전트의 접근도
같은 순간에 끊기고, 만료도 웹과 같다(`SESSION_TTL_DAYS`, 기본 14일).

로그인 응답의 `Set-Cookie` 에 들어 있는 값이 그대로 토큰이다:

```bash
curl -sS -D - -o /dev/null -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"비밀번호"}' \
  | sed -n 's/^[Ss]et-[Cc]ookie: session_id=\([^;]*\).*/\1/p'
```

출력되는 UUID 하나가 토큰이다. 컨테이너 스택이라면 주소는
`https://<도메인>/api/auth/login`.

### 2. 클라이언트에 등록

전송 방식은 **Streamable HTTP**(stateless)다. 핸드셰이크도 세션도 없다.

| | 값 |
| --- | --- |
| URL (로컬) | `http://localhost:8000/mcp` |
| URL (컨테이너·배포) | `https://<도메인>/api/mcp` |
| 헤더 | `Authorization: Bearer <세션 UUID>` |

Claude Code:

```bash
claude mcp add --transport http docs-rag http://localhost:8000/mcp \
  --header "Authorization: Bearer <세션 UUID>"
```

Cursor (`~/.cursor/mcp.json`) 및 `.mcp.json` 형식의 클라이언트 일반:

```json
{
  "mcpServers": {
    "docs-rag": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <세션 UUID>" }
    }
  }
}
```

붙었는지 확인하는 가장 짧은 방법은 에이전트에게 "내 문서에서 …를 찾아줘"라고
시키고 `GET /traces` 에 행이 남는지 보는 것이다 — MCP 호출은 `source` 가
`mcp_search` 인 트레이스로 남는다. `GET /admin/stats` 의 `by_source` 는 같은
것을 소비자별 호출 수로 보여준다(`query` · `conversation` · `mcp_search`).

### 3. 배포에서 한 번은 막히는 것

- **421 Invalid Host header.** DNS 리바인딩 보호가 기본으로 켜져 있고, 설정을
  비워 두면 로컬호스트만 허용한다. 공개 도메인으로 붙이려면
  `MCP_ALLOWED_HOSTS=["docs.example.com"]` 을 넣어야 한다. 기본값을 "전부
  허용"이 아니라 이쪽으로 둔 것은 의도다 — 잊었을 때 조용히 열린 채로 도는
  대신 시끄럽게 실패한다.
- **경로에 `/api` 가 붙는다.** Caddy 가 `handle_path /api/*` 로 접두사를
  떼므로 공개 URL 은 `/api/mcp` 다. 앱 자체는 `/mcp` 로 듣는다.
- **401 이 계속 난다면** 토큰이 UUID 인지, 그리고 그 세션이 아직 살아 있는지
  (로그아웃하지 않았는지) 본다. 만료·폐기·오타는 전부 같은 401 이다 — 어느
  쪽인지 알려주지 않는 것이 의도다.

### 4. 알아둘 비용

`search_documents` 한 번은 **사용자의 하루 질의 쿼터(`QUOTA_QUERIES_PER_DAY`,
기본 200)를 1 깎는다.** LLM 을 부르지 않는데도 그렇게 한 이유는, 이 툴이 하는
일이 질의 비용의 비싼 절반이기 때문이다 — 실측으로 리랭킹만 8.8초다
(`CAND_K` 절 참조). 근거 전문은 `app/mcp/tools.py` 의 쿼터 주석에 있다.

에이전트는 사람보다 훨씬 자주 부른다. 한 질문이 검색 다섯 번이 되는 것이
정상이므로, 하루 실사용에서 200이 모자라다고 느껴질 수 있다 — 그 관찰 자체가
W2 의 완료 기준("불편한 지점 3개 이상 기록")에 해당한다.

### 5. OAuth 로 바꿔 달기 (M7 W7)

**이 서버는 토큰을 발급하지 않는다.** 리소스 서버다 — 로그인·동의·발급은 기존
IdP 의 일이고, 인가 서버를 직접 만드는 것은 범위 밖이다. 여기 있는 것은
"남이 발급한 토큰을 어떻게 검증할 것인가"뿐이다.

`MCP_AUTH_MODE` 가 어느 자격증명을 받을지 정한다:

| 값 | 받는 것 | 쓰는 때 |
| --- | --- | --- |
| `session` (기본) | 세션 행 베어러 (위 1~4절) | 지금. 실제로 동작하는 유일한 경로다 |
| `oauth` | IdP 가 발급한 JWT 만 | IdP 를 붙인 뒤 |
| `both` | JWT 를 먼저, 아니면 세션 | 이행 구간 |

기본이 `session` 인 이유는 두 가지다. 하나는 오늘 붙어 있는 클라이언트를 끊지
않는 것이고, 다른 하나는 **위험한 방향이 막혀 있기 때문**이다 — "IdP 토큰을
검증한다고 믿는데 사실 아무거나 받는" 상태는 `oauth`/`both` 에서 설정이 하나라도
비면 **앱이 기동하지 않으므로** 만들어지지 않는다. 조용히 약해지는 경로가 없다.

```bash
MCP_AUTH_MODE=oauth
MCP_OAUTH_ISSUER=https://idp.example.com/            # iss 와 정확히 같은 문자열
MCP_OAUTH_JWKS_URL=https://idp.example.com/.well-known/jwks.json
MCP_RESOURCE_SERVER_URL=https://docs.example.com/api/mcp   # = aud
MCP_OAUTH_ALGORITHMS=["RS256"]
```

검사하는 것: 서명(JWKS 로 로컬 검증, 요청마다 IdP 를 때리지 않는다) · `iss` ·
**`aud`**(= 우리 서버용으로 발급된 토큰인가) · `exp` · 허용 알고리즘 목록.
거절 사유는 전부 같은 401 이다.

**주체는 검증된 `email` 클레임으로 로컬 계정에 붙는다.** 계정을 만들지는
않는다 — 없으면 401 이다. `email_verified` 가 참이 아닌 토큰도 거절한다(아니면
IdP 에 남의 주소를 적는 것만으로 그 사람의 테넌트를 지목할 수 있다). 제대로 된
계정 연결은 별도의 식별 테이블과 사용자 동의 흐름을 요구하는 별개의 작업이고,
지금은 하지 않았다.

`oauth`/`both` 에서 RFC 9728 문서가 켜진다:

```
GET /.well-known/oauth-protected-resource/mcp
→ {"resource": "...", "authorization_servers": ["https://idp.example.com/"], ...}
```

그리고 401 의 `WWW-Authenticate` 가 그 위치를 안내한다
(`resource_metadata="https://…/.well-known/oauth-protected-resource/mcp"`).
`session` 모드에서는 둘 다 켜지 않는다 — 가리킬 인가 서버가 없는데 광고하면
클라이언트는 쓸 수 있는 베어러 대신 실패할 디스커버리로 끌려간다.

⚠️ **Caddy 뒤에서는 `/.well-known/...` 이 `/api/*` 규칙에 걸리지 않는다.**
RFC 9728 은 이 문서를 호스트 루트에 두라고 하므로 접두사가 붙지 않는다. 공개
배포에서는 Caddyfile 에 이 경로를 백엔드로 보내는 규칙을 따로 넣어야 한다.

⚠️ **알려진 한계:** 진짜 IdP 와 맞춰 본 적이 없다. 테스트는 자체 서명 토큰으로
검증 로직만 돌린다(`tests/test_mcp_oauth.py` — 그 픽스처는 IdP 대체물이
아니다). JWKS 회전이나 ID 토큰/액세스 토큰 구분 같은 것은 붙이는 날 처음 겪는다.

### 6. 스코프

`docs:search`(읽기) · `docs:write`(쓰기)가 있다. **`tools/list` 는 호출자가 든
스코프에 따라 다르게 나온다** — 읽기만 있으면 쓰기 툴은 목록에 없다. 직접
이름을 불러도 거절되고, 그 거절은 어느 스코프가 필요한지 말해 준다(모르면
에이전트가 같은 호출을 영원히 재시도한다).

오늘 쓰기 툴은 **없다.** 툴이 `search_documents` 하나뿐이고 전부 읽기다. 없는
쓰기 툴을 증명용으로 만들지 않았고, 대신 게이팅 메커니즘을 만든 뒤 테스트에서
가짜 쓰기 툴로 증명한다(`tests/test_mcp_scopes.py`).

세션 토큰은 **모든 스코프**를 받는다. 스코프는 *클라이언트*가 사용자보다 적게
가질 수 있게 하는 장치인데, 세션 id 는 사용자 본인의 자격증명이고 그 사람은 웹
UI 에서 이미 전부 할 수 있기 때문이다. 좁은 스코프를 가진 클라이언트는 OAuth 가
만든다.

툴에 스코프를 선언하는 것과 강제하는 것은 **한 줄**이다(`app/mcp/scopes.py` 의
`scoped_tool`). 선언을 잊은 툴은 아무에게도 보이지 않는다 — 조용히 모두에게
노출되는 것보다 첫 실행에서 사라지는 편이 낫다.

### 7. 트레이스 (OpenTelemetry)

기본은 **꺼져 있고, 꺼짐은 no-op 이다.** `TracerProvider` 를 아예 설치하지
않으므로 스팬이 만들어지지 않는다 — 수집기 없이 clone 해도 앱은 그대로 돈다.

```bash
OTEL_ENABLED=true
OTEL_EXPORTER=console        # 또는 otlp
OTEL_SERVICE_NAME=docs-rag
```

`otlp` 는 `opentelemetry-exporter-otlp-proto-http` 를 따로 설치해야 한다
(`requirements.txt` 에 없는 이유는 protobuf/requests 를 끌고 오는데 계측을
켜는 배포에서만 필요해서다). 없으면 기동 시 에러 로그를 남기고 계측 없이 계속
간다 — 관측이 관측 대상을 내리지는 않는다.

스팬 구조는 **툴 호출 → 검색 → (생성)** 이다. 생성이 괄호인 것은 MCP 경로에는
없기 때문이다. 툴 호출 스팬과 `_meta` 의 `traceparent` 전파는 **MCP SDK 가 이미
한다**(`mcp/server/_otel.py`) — 우리는 그것을 다시 하지 않고(중복 계측은 스팬을
두 번 만든다), 단계 스팬(`docs_rag.embed`/`retrieve`/`rerank`/`generate`)과
도메인 속성만 더한다:

```
tools/call search_documents        gen_ai.tool.name, mcp.method.name      ← SDK
  ├─ docs_rag.embed                                                       ← 우리
  └─ docs_rag.retrieve                                                    ← 우리
     docs_rag.{source,tenant,top_k,chunks,tokens_in,tokens_out,total_ms}  ← 우리
```

`traceparent` 는 **클라이언트가 주는 입력이다.** 부모 스팬을 정하는 데만 쓰고,
신원이나 테넌트 판단에는 절대 쓰지 않는다. 테넌트는 오직 검증된 토큰에서 온다.

`docs_rag.tenant` 는 **해시**다(blake2s, `person="docs-rag"`, 8바이트). 원문
`users.id` 는 스팬에 남지 않는다 — 트레이스는 보통 외부 백엔드로 나가고, 거기에
우리 기본키의 사본을 두지 않으려는 것이다. 키 없는 해시라 사용자 id 목록을 이미
가진 사람은 추측을 확인할 수 있다. 목적은 비밀 유지가 아니라 비식별화다.

이 스팬들은 `traces` 테이블을 **대체하지 않는다.** 저쪽은 제품의 기록(소유자
범위, `/traces`·`/admin/stats` 로 조회, W4 의 L3 지표 원천)이고 이쪽은 운영자의
기록(실시간, 분산, 원문 id 없음)이다. 단계 지연만 공유한다 — 두 번 재면 두
값이 어긋날 수 있어서다.

### 8. confused deputy 는 만들지 않는다

클라이언트에게 받은 토큰이 업스트림(TEI·리랭커·Gemini)으로 흘러가는 경로는
**없다.** 그 경로가 생기면 모델 서버를 운영하는 쪽이 우리 사용자의 자격증명을
쥐게 된다. 업스트림 호출은 `app/services/{embeddings,rerank,llm}.py` 가 하고,
어느 것도 요청에서 자격증명을 받지 않는다.
`tests/test_mcp_observability.py` 가 툴 호출 중 나가는 HTTP 요청을 전부 기록해
토큰이 헤더에도 본문에도 없음을 고정한다.

## 알아둘 동작

여기 적힌 것들은 전부 한 번씩 틀렸다가 고친 자리다.

### SSE는 상태 코드를 쓸 수 없다

스트리밍이 시작되면 HTTP 상태는 200으로 굳는다. 그래서 429(쿼터)·503·500은
상태 코드가 아니라 `event: error`의 본문(`{status, detail}`)으로 온다.
프레임 순서는 `meta` → `token`* → `done`이고, `error`는 언제든 올 수 있다.

`document_id`는 **필드의 유무**로 읽는다. 없으면 대화에 저장된 범위,
있으면 호출자가 고른 것이고 `null`도 진짜 선택(전체 문서)이다.

### 남의 형편은 500이 아니다

| 상황 | 응답 |
| --- | --- |
| Gemini 무료 한도 소진 | `429 model quota exceeded` |
| Gemini 과부하 | `503 model unavailable` |
| 임베딩·리랭커 서버 미기동/타임아웃/5xx | `503 search unavailable` |
| 그 밖의 4xx | **그대로 터뜨림** — 요청이 잘못된 건 이쪽 버그다 |

`Retry-After`는 **제공자가 재시도 시각을 알려줄 때만** 붙인다. 분당 한도는
몇 초를 주지만 일일 한도는 아무것도 주지 않는다 — 답이 "내일"이라서다.
없는 숫자를 지어내느니 헤더를 빼는 편이 정직하다.

4xx를 "잠시 뒤에 다시"로 포장하면 버그를 숨기고 무한 재시도를 부른다.

detail 문자열은 사용자에게 보여줄 문장이 아니라 **프론트가 문구를 고르는
식별자**다(`web/lib/api/errors.ts`). 429 하나가 "너무 빠름"과 "오늘 다 씀"
두 가지를 뜻하므로 상태 코드만으로는 갈라지지 않는다. 백엔드와 프론트의
표가 어긋나면 `tests/test_error_details.py`가 실패한다.

### 색인 실패는 두 종류다

`documents.status='failed'`는 "글자 없는 PDF"와 "임베딩 서버에 못 닿음"
둘 다를 뜻한다. 뭉뚱그리면 멀쩡한 문서에 "스캔본일 수 있습니다"라고 잘못
안내하게 되므로, `web/lib/failure.ts`가 error 문자열로 갈라 처리한다.

### `/admin/stats` 읽는 법

필요한 숫자는 이미 `traces`와 `documents`에 다 있었다. 없던 것은 수집
파이프라인이 아니라 **읽을 창구**였다(M4에서 Langfuse를 기각한 논리 그대로).
이 엔드포인트는 `usage_events`를 읽지 않는다 — 그쪽을 읽는 것은 쿼터 판정과
`scripts/cost_report.py`다.

- `total_ms`는 **캐시 히트 포함** — 사용자가 실제로 기다린 시간.
- `stages`는 **실패한 질의만 제외**한다. 캐시 히트는 포함된다 — 시맨틱
  캐시라 히트도 임베딩은 실제로 돌고, 나머지 세 단계는 NULL이라 백분위가
  알아서 건너뛴다. 실패를 빼는 이유는 반대다: 죽은 임베더로의 연결 거부는
  몇 밀리초 만에 돌아와서, 섞으면 **장애 중에 p50이 좋아진다.**
- `queries.failed`와 `query_failures`가 실패를 상태 코드·사유별로 센다.
  예전엔 실패한 질의가 `traces`에 행조차 안 남아서, 임베딩 서버가 죽으면
  total이 **줄어들어** 한가한 시간처럼 보였다.
- `failures`(색인 실패)는 error 문자열의 콜론 앞부분으로 묶는다. 원인은
  구분돼야 하고 파일명은 보고서에 새면 안 된다. 이건 `updated_at` 기준으로
  창을 지키지만, `documents`(상태별 개수)는 **일부러 창을 안 건다** —
  `hours=1`로 물었다고 멀쩡한 코퍼스를 `ready: 0`으로 보고하면 안 되기
  때문이다.

`ADMIN_TOKEN`이 비어 있으면 401이 아니라 **404**다. 401은 설정한 적 없는
배포에서도 이 엔드포인트의 존재를 알려주기 때문이다.

### 로그는 어디에 있나

관측의 본체는 로그가 아니라 Postgres다(위 `/admin/stats`). 로그는 **삼킨
예외**를 남기는 자리다 — 사용자 요청을 죽이지 않으려고 잡아먹은 실패가
어디에도 안 남으면 나중에 "왜 그게 실패했나"에 답할 수 없기 때문이다.

| 무엇 | 어디 |
| --- | --- |
| 앱 로그 | `docker compose logs app` (stderr) |
| 액세스 로그 | **두 군데** — 같은 요청이 `app`(uvicorn)과 `proxy`(Caddy) 양쪽에 찍힌다 |
| 모델 서버(맥, 호스트 프로세스) | `~/Library/Logs/docs_rag/models.log` |

앱 로그 한 줄은 이렇게 생겼다:

```
2026-09-14T11:13:29.108Z WARNING  app.routers.conversations [9f2c…] stream failed …
```

시각은 **UTC**이고 그래서 `Z`를 붙인다. DB의 모든 시각이 UTC(timestamptz)인데
운영자는 KST라, 로그만 세 번째 표기를 쓰면 사고 때마다 암산을 한다. 컨테이너
로그 중 시각이 없는 줄(uvicorn 액세스 로그)은 `docker compose logs -t`로 도커가
붙여주는 시각을 쓴다.

**대괄호 안이 요청 ID다.** 한 요청이 남기는 네 갈래를 이 값 하나로 묶는다:

- 응답 헤더 `X-Request-Id` (프론트/curl에서 바로 보인다)
- Caddy 액세스 로그의 `request_id`
- 앱 로그의 `[…]`
- `traces` 행

클라이언트나 프록시가 보낸 값은 **모양이 멀쩡할 때만** 이어받는다. 이 값은 모든
로그 줄에 그대로 박히므로, 검사 없이 실으면 개행 하나로 로그 줄을 위조할 수
있다. 버린 경우엔 새로 만든다. Caddy는 자기가 만든 `uuid`도 같은 줄에 따로
남기므로, 클라이언트가 ID를 고른 요청에도 **손댈 수 없는 서버 쪽 키**가 항상
있다. 다만 Caddy의 **오류 로거**(업스트림이 죽었을 때의 502 줄)에는 이 칸이
붙지 않는다 — 그 줄은 같은 요청의 액세스 줄과 시각·URI로 맞춰 읽어야 한다.

성공한 `/health`는 액세스 로그에서 뺀다. 컨테이너 헬스체크가 30초마다 치므로
아무도 안 쓰는 날에도 하루 2,880줄이고, 그게 보관 한도 안에서 진짜 요청을
밀어낸다. **실패한 헬스체크는 남긴다** — "컨테이너가 재시작되는 중"과
"컨테이너는 멀쩡한데 그 앞이 이상하다"를 가르는 줄이라서다.

보관은 서비스마다 10MB × 3세대(`docker-compose.yml`의 `x-logging`). 호스트
모델 서버 로그는 기동할 때마다 이어 쓰고(예전엔 덮어써서 **죽은 이유가 다음
기동에 지워졌다**) 5MB를 넘으면 한 세대만 굴린다. 경로와 크기는 `MODEL_LOG` ·
`MODEL_LOG_MAX_BYTES`로 바꾼다.

**로그에 일부러 안 남기는 것:** 업로드 문서 원문·질문·답변. 계약서를 다루는
서비스라 그렇다. DB 오류가 바인딩 값을 통째로 쏟던 경로도 막았다(`app/db.py`의
`hide_parameters`). 인증 토큰도 마찬가지 이유로 `#` 뒤에 둔다 — 프래그먼트는
브라우저가 서버로 보내지 않으므로 프록시 로그에 애초에 도달하지 않는다.
Caddy 쪽 토큰 리댁션은 아직 살아 있는 레거시 링크를 위한 2차 방어선이다.

### 프록시 뒤에서는 클라이언트 주소를 챙겨야 한다

per-IP rate limit이 `request.client.host`를 쓴다. 프록시를 붙이면 그게 전부
프록시 주소가 되어 **전 사용자가 한 바구니**를 쓰게 된다. `--proxy-headers`
만으로는 부족하고, uvicorn이 `FORWARDED_ALLOW_IPS`에 있는 주소에서 온
`X-Forwarded-For`만 믿으므로 그 값도 함께 설정해야 한다(compose에 반영돼
있다). 앱 포트를 직접 publish하면 이 신뢰가 위험해지니, 그때는 먼저 좁힐 것.

## 설정

전체 목록은 `app/config.py`. 자주 만지는 것만:

| 변수 | 기본 | 뜻 |
| --- | --- | --- |
| `DATABASE_URL` | localhost:5432 | Postgres |
| `LOG_LEVEL` | `WARNING` | 앱 로그 레벨. 올려도 SQL은 안 나온다 — SQLAlchemy가 자기 로거를 따로 못박는다 |
| `TEI_URL` · `RERANK_URL` | :8080 · :8081 | 임베딩·리랭커 서버 |
| `GEMINI_API_KEY` | — | 필수 |
| `LLM_MODEL` | `gemini-3.6-flash` | 생성 모델. **무료 티어라면 `gemini-3.1-flash-lite` 로 바꿀 것** — 아래 참조 |
| `EVAL_LLM_MODEL` | `gemini-3.1-flash-lite` | 평가용. 무료 티어가 `3.6-flash`는 **하루 20회**라 분리했다 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 700 / 100 | 토큰 단위 |
| `CAND_K` | 40 | 리랭커에 넘길 후보 수 (아래 평가 참조) |
| `RERANK_TOP` | 3 | 생성에 넘길 청크 수 |
| `RERANK_MIN_SCORE` | 0.005 | 리랭커 점수 하한. **`MIN_SCORE`와 다른 값이다** |
| `RERANK_MAX_CHARS` | 0 (끔) | 리랭커 입력 자르기. 측정 결과 켜면 안 된다 |
| `RATE_LIMIT_*` · `QUOTA_*` | 20/분 · 200/일 · 1000쪽/월 | 남용 방지 |
| `SEMANTIC_CACHE_THRESHOLD` | 0.95 | 코사인 유사도 |
| `MAIL_PROVIDER` | `console` | 메일 발송 방식. `console`은 로그에 `[mail]` 줄로만 찍고, `resend`면 실제 발송 |
| `RESEND_API_KEY` | — | `resend` 발송 시 필수 |
| `MAIL_FROM` | `onboarding@resend.dev` | 발신 주소. 도메인 인증 전엔 이 주소만, 수신도 계정 소유자에게만 간다 |
| `APP_BASE_URL` | `http://localhost:3000` | 인증 링크가 가리키는 프론트엔드 오리진(백엔드 아님) |
| `VERIFY_TOKEN_TTL_HOURS` | 24 | 인증 링크 유효 시간 |
| `UNVERIFIED_QUOTA_QUERIES` | 5 | 미인증 계정 질의 한도. **계정 수명 전체 누적** |
| `UNVERIFIED_QUOTA_DOCUMENTS` | 1 | 미인증 계정 업로드 한도. 역시 수명 전체 누적 |
| `RATE_LIMIT_VERIFY_RESEND_PER_MIN` | 1/분 | 인증 메일 재발송 제한 |
| `MCP_ENABLED` | `true` | MCP 서버 마운트 여부. 끄면 경로가 404 |
| `MCP_PATH` | `/mcp` | MCP 엔드포인트 경로 |
| `MCP_ALLOWED_HOSTS` | 비움 = 로컬호스트만 | 허용 Host. **공개 배포에서는 도메인을 넣어야 한다** (아니면 421) |
| `MCP_ALLOWED_ORIGINS` | 비움 = 로컬호스트만 | 허용 Origin. Claude·Cursor 는 Origin 을 안 보내므로 보통 비워 둔다 |
| `MCP_TOOLS_CACHE_TTL_MS` | 60000 | `tools/list` 응답의 `ttlMs`. `cacheScope` 는 `private` 고정 |
| `MCP_RESOURCE_SERVER_URL` | 비움 = 끔 | RFC 9728 메타데이터 라우트. W7 에서 켠다 |
| `JUDGE_PROVIDER` · `JUDGE_BASE_URL` | `ollama` · `127.0.0.1:11434` | L2 judge 서버. **평가 전용** — 앱 경로는 안 읽는다 |
| `JUDGE_MODEL` | `qwen3:4b` | L2 judge 모델. Gemini 와 다른 계열이어야 한다(W6) |
| `JUDGE_NUM_CTX` · `JUDGE_KEEP_ALIVE` | 8192 · `5m` | 루브릭 전문 + 컨텍스트가 들어간다. 넘치면 잘려 나가는 것이 루브릭이다 |

> `MIN_SCORE`(코사인)를 리랭커 시그모이드에 재사용했다가 답변 가능한 질문의
> 21%를 LLM 호출도 없이 거부한 적이 있다. 두 점수는 같은 양이 아니다.

### 무료 티어에서 생성 모델 고르기

기본값 `gemini-3.6-flash` 는 **무료 티어에서 대화형으로 쓸 수 없다.** 실측:

| 모델 | 한 문장 응답 | 무료 한도 |
| --- | --- | --- |
| `gemini-3.6-flash` | 34\~40초 | 하루 20회 |
| `gemini-3.1-flash-lite` | 0.9초 | 훨씬 넉넉 |

무료 키로 돌린다면 `LLM_MODEL=gemini-3.1-flash-lite`. 이 값으로 30쪽 문서
요약이 스트리밍 13.2초에 끝난다(같은 질의가 3.6-flash 에서는 59초 만에
503 `model unavailable` 로 실패했다). 유료 티어로 가면 기본값으로 돌아오면 된다.

`EVAL_LLM_MODEL` 이 이미 flash-lite 인 것도 같은 이유다(M4).

### 이메일 인증

가입하면 확인 메일이 나간다. 확인 전에는 질문 5회·문서 1개까지만 쓸 수
있고 그 뒤로는 403이 나온다 — 이 한도는 하루가 아니라 **계정 수명 전체
누적**이다. "하루 5회"로 두면 미인증 계정이 매일 5회씩 다시 받아서, 막으려던
재가입 어뷰즈가 그대로 통과하기 때문이다.

`MAIL_PROVIDER=console`(기본)이면 메일 대신 링크가 서버 로그에 `[mail]`
줄로 찍힌다. 실제 발송은 `MAIL_PROVIDER=resend` + `RESEND_API_KEY`.

**도메인이 없으면 본인 주소로만 전달된다.** Resend 는 도메인을 검증하기
전까지 `onboarding@resend.dev` 발신만 허용하고, 그 경우 수신도 계정
소유자에게만 간다. 남이 가입해서 인증까지 마치게 하려면 도메인을 붙이고
SPF/DKIM 을 설정한 뒤 `MAIL_FROM` 을 바꾼다. 코드 변경은 필요 없다.

## 테스트

```bash
pytest                  # 순수 로직 + Postgres가 있으면 통합 테스트까지
npm --prefix web test   # 프론트 순수 로직 (각주 매핑·SSE 파서·에러 문구)
npm --prefix web run build   # 타입 검사 포함 — vitest는 타입을 보지 않는다
python -m scripts.e2e_smoke  # 실제 HTTP로 전 구간 (서버·DB·모델·LLM 필요)
```
통합 테스트는 Postgres에 닿지 못하면 스킵된다. 스모크는 `--base`로 대상을
바꿀 수 있다(예: 컨테이너 스택 `--base http://localhost:8088/api`).

## 검색 품질 평가

라벨링된 코퍼스로 검색 구성을 정량 비교한다. 생성은 제외하고 검색만 잰다.
Postgres와 임베딩/리랭커 서버가 필요하다.

```bash
python -m eval.run --corpus hard    # 40p·20질의, 혼동 후보 포함 (판별력 있음)
python -m eval.run --corpus simple  # 15p·15질의, 전 구성 만점 = 판별 불가(음성 대조군)
```

**코퍼스는 재려는 것에 맞춰 고른다.** 이 하네스에서 두 번, 기본 코퍼스로는
답할 수 없는 질문에 답하려다 잘못된 안심을 얻을 뻔했다.

### 리랭커 입력 자르기 (`RERANK_MAX_CHARS`) — 켜면 안 된다

```bash
python -m eval.rerank_truncation --corpus longchunk  # 사실 위치별 손실
python -m eval.rerank_truncation --corpus hard       # 음성 대조군
```
`longchunk`은 이 실험용이다. 다른 코퍼스는 조항이 전부 128자 미만이라
**자르기가 아무 일도 하지 않아** 손실을 측정할 수 없다. 깊이별로 나눠 읽을 것
— 평균은 효과를 가린다. 사실이 청크 앞쪽에 있으면 무손실, 뒤쪽에 있으면
전멸한다.

### 후보 수 (`CAND_K`)

```bash
python -m eval.candk_sweep --corpus deep              # 실제 문서 크기 — 이쪽을 쓸 것
python -m eval.candk_sweep --corpus deep --ks 50,40,30   # 구간을 좁혀서
```

**`wide`로 재지 말 것.** 260쪽이라 폭은 맞지만 한 쪽이 53자다. 실제 문서는
청크 중앙값이 1,295자이고, 리랭킹 비용도 검색 난이도도 **쪽 개수가 아니라
쪽 길이**에 좌우된다. 그 차이가 결론을 뒤집는다 — 같은 조항·같은 질의로
길이만 바꿔 재면:

| CAND_K | 100 | 50 | 40 | 30 | 20 | 10 | 5 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `wide` (53자) R@1 | 1.000 | 1.000 | — | 1.000 | 1.000 | 1.000 | 1.000 |
| `deep` (1,250자) R@1 | 0.947 | 0.947 | **0.947** | 0.895 | 0.868 | 0.816 | 0.789 |
| `deep` 리랭킹 | 26.8s | 11.5s | **8.8s** | 6.5s | 4.4s | 2.2s | 1.1s |

짧은 청크에서는 5까지 줄여도 R@1이 1.000이라 "공짜 속도"로 보인다. 실제
길이에서는 단조 감소하고, 무릎은 40이다. 기본값 40은 여기서 나왔다.

40 아래에서 먼저 무너지는 것은 semantic 질의다(천장 1.000 → 0.929). dense는
30에서도 1.000인데 sparse가 0.714라 융합 후 후보에 못 드는 것이 생긴다.
exact는 반대로 sparse가 30까지 1.000을 지킨다 — 두 유형이 서로 다른 채널에
기대므로, 깊이를 줄이면 한쪽만 먼저 깨진다.

이 저장소는 같은 함정에 이미 한 번 빠졌다(`RERANK_MAX_CHARS` 실험이 짧은
픽스처 때문에 "변화 없음"으로 나왔던 것 — `eval/corpora/longchunk.py` 참조).
길이 감각이 필요한 실험은 `deep` 또는 `longchunk`로 할 것.

읽는 법: **후보 적중률이 R@1의 천장이다.** 후보에 없으면 리랭커가 복구할 수
없다. 실제로 이 설정을 좌우하는 값은 두 채널 중 좋은 쪽의 순위
`min(dense, sparse)`이므로, 스윕 끝의 여유 표를 함께 볼 것. RRF 순위는
천장이 아니다 — 융합의 순서일 뿐이고 리랭커가 다시 정렬한다.

### 그 밖

```bash
python -m eval.sparse_channels   # lexical 채널 비교 (fts / trgm / bge)
python -m eval.golden_run        # 골든셋 3문서·36문항 → 기록 덤프
python -m eval.judge_run         # 위 기록으로 M4 의 RAGAS 4지표 산출 (Gemini judge)
```

`eval.judge_run` 은 **M4 때 만든 judge** 다 — Gemini 로 faithfulness ·
answer_relevancy · context_precision · context_recall 을 낸다. M7 의 L2 judge는
차원도 모델도 다른 별개의 물건이고, 아래 절에 있다. 두 수치를 섞어 쓰지 말 것.

## L1 평가 하네스 (M7 W3)

위의 러너들이 실험용 일회성이라면, 이쪽은 **회귀를 자동으로 잡는 것**이
목적이다. 데이터셋은 jsonl, 지표는 청크 단위 L1(recall@k · MRR · nDCG@10),
결과는 Postgres에 버전별로 쌓이고, 두 실행을 diff하면 회귀 시 non-zero로
끝난다. LLM은 한 번도 부르지 않는다 — 결정론적이고 빨라서 매 커밋 돌 수 있다.

```bash
python -m eval.harness validate --dataset eval/datasets/synthetic_golden.jsonl
python -m eval.harness --dataset eval/datasets/synthetic_golden.jsonl
python -m eval.harness run --dataset <path> --config dense --config hybrid+rerank
python -m eval.harness diff --base latest~1 --head latest --dataset <path>
python -m eval.harness list --dataset <path>
```

`validate`는 DB도 모델 서버도 필요 없다. `run`은 둘 다 필요하고, `--no-store`
로 저장을 끌 수 있다. `diff`는 `--base-json`/`--head-json`으로 파일만 놓고도
돈다.

**정답은 청크 ID가 아니라 원문 스니펫으로 저장한다.** 청크 UUID로 고정하면
인덱싱할 때마다 참조가 죽고, 무엇보다 청킹 전략을 바꾸는 순간(W5의 주요 실험
대상) 골든셋 전체가 무효가 된다. 러너가 실행 시점에 스니펫을 지금 인덱스의
청크로 역매칭하므로 그 종속성이 생기지 않는다. 스니펫이 문서에서 사라지면
0점이 아니라 **예외로 죽는다** — 라벨이 문서를 못 따라갔다는 신호다. 포맷과
규칙은 [`eval/datasets/FORMAT.md`](eval/datasets/FORMAT.md).

이 저장소에 든 데이터셋(`synthetic_golden`, `synthetic_hard`)은 기존 합성
코퍼스를 변환한 **하네스 검증용 픽스처**다. M7의 실제 측정 대상인 업무 문서
골든셋은 private repo에 있고, `--provider` 로 갈아끼운다.

CI에서는 backend 잡이 데이터셋 검증과 하네스 전체(포맷·스니펫 고유성·지표·회귀
판정·Postgres 저장)를 매 커밋 돌리고, 실제 수치와 diff는 모델 서버가 붙은
러너에서만 도는 `eval` 잡이 맡는다(저장소 변수 `EVAL_ENABLED=true`).
이유는 `.github/workflows/ci.yml` 주석 참조.

## 검색 파이프라인 A/B (M7 W5)

하네스 위에서 **무엇이 얼마나 기여했는지 분리해서** 말하려는 것이다. 네 손잡이
(청킹 단위 · 헤딩 경로 접두사 · 하이브리드 · 리랭커)를 한꺼번에 켜고 "좋아졌다"
고 쓰면 기여도를 못 말하므로, 누적 사다리로 **한 칸에 하나씩만** 켠다.

```bash
python -m eval.harness ab --dataset eval/datasets/spec_golden.jsonl --k 10
python -m eval.harness ab --dataset <path> --only A1-section   # 한 칸만 다시
```

2026-09-14 실측 (`eval/corpora/spec.py` 30쪽 3문서, 44문항 중 채점 27):

| 칸 | 청킹 | R@1 | R@5 | R@10 | P@10 | MRR | 재색인s | 지연ms |
|---|---|---|---|---|---|---|---|---|
| A0-baseline | fixed | 0.444 | 0.722 | 0.759 | 0.093 | 0.629 | 16.9 | 54 |
| A1-section | section | 0.481 | 0.759 | 0.796 | 0.093 | 0.663 | 13.6 | 52 |
| A2-heading-prefix | section+prefix | 0.500 | 0.796 | 0.796 | 0.093 | 0.690 | 13.7 | 53 |
| A3-hybrid | section+prefix | 0.537 | **0.778** | 0.870 | 0.104 | 0.751 | (재사용) | 70 |
| A4-rerank | section+prefix | 0.704 | 1.000 | 1.000 | 0.126 | 0.901 | (재사용) | 9457 |

베이스라인 대비: **R@5 0.722 → 1.000 (+0.278)**, R@1 0.444 → 0.704 (+0.259).

**사다리는 단조가 아니다 — 하이브리드 칸이 R@5를 떨어뜨린다**(0.796 → 0.778).
같은 칸에서 R@10은 +0.074, R@1은 +0.037, MRR은 +0.061로 전부 오른다. 즉
하이브리드는 정답을 더 깊이 끌어오면서 4~5위 대역을 흐트러뜨린다 — sparse의
literal 오답이 RRF에서 상위 가중치를 받기 때문이고, **M2의 D18 실험에서 이미
관측된 것과 같은 모양이다**(그때는 semantic 질의 R@1이 1.00 → 0.75로 퇴행했다).
두 마일스톤 뒤 다른 코퍼스에서 독립적으로 재현됐고, 그때와 같이 **리랭커가
복구한다**(다음 칸에서 전 대역 1.000). "리랭커는 선택이 아니라 필수"라는 M2의
결론이 M7에서도 유지된다.

**헤딩 접두사는 R@10을 전혀 안 움직였는데 R@5는 +0.037, MRR은 +0.027 올렸다.**
R@10 대역은 이미 포화라 거기서는 보이지 않았을 뿐이다. R@10만 봤다면 "효과
없음"으로 기각했을 것이고 그게 틀린 결론이다 — 커버리지가 아니라 순위를 고치는
손잡이다. 그래서 이 사다리는 여러 컷오프와 precision과 MRR을 항상 같이 찍는다.

**리랭커가 가장 크게 기여하지만 지연이 135배다**(70ms → 9,457ms). 설정
[`app/config.py`](app/config.py)의 `cand_k` 주석에 적힌 "CAND_K=40·실제 길이
청크에서 8.8초"와 맞는 교차검증이고, M7에서도 정확도-지연 교환이 그대로
살아 있다는 뜻이다.

**기본값은 `fixed`로 남겼다.** 섹션 청킹이 이 코퍼스에서 공짜였는데도(R@10이
오르고 재색인이 오히려 빨라졌다) 안 바꾼 이유는 측정과 무관하다 — 숫자가 합성
코퍼스 하나에서 나왔고(이 저장소는 코퍼스 하나로 일반화했다가 두 번 뒤집혔다),
청킹을 바꾸면 **이미 색인된 모든 문서를 다시 색인해야** 이득이 생긴다. 켜려면
`CHUNK_STRATEGY=section` · `CHUNK_HEADING_PREFIX=true`.

헤딩 경로는 **임베딩 입력에만** 붙고 저장되는 `content`는 안 건드린다. `content`가
원문의 축자 부분문자열이라는 계약 위에 인용 스니펫과 골든셋 스팬 역매칭이 서
있어서, 본문에 접두사를 넣으면 골든셋 전체가 해석 불가가 된다.

⚠️ `eval/corpora/spec.py`는 헤딩을 마크다운 `#` 표기로 **직접 준다.** 실제 PDF에서
헤딩 추출은 그 자체로 오차 있는 별도 단계이고, 섞으면 결과가 청킹 얘기인지 헤딩
추출 얘기인지 구분할 수 없다. 실제 문서에 켜려면 헤딩 추출기가 먼저 필요하다.

## 툴 설계 A/B (M7 W4)

**툴 설명이 곧 프롬프트**라는 것을 수치로 확인한다. 검색 품질이 아니라 **에이전트
행동**을 보므로 골든셋과 별도의 시나리오셋(`eval/scenarios_l3.jsonl`, 20문항)을
쓰고, 지표는 툴 선택 정확도 · 인자 정확도 · 호출 수 · 불필요 호출 비율이다.

```bash
# MCP 검색 툴이 "query" 쿼터를 깎는다. 스윕 중간에 쿼터로 죽으면 절반의 조건이
# "한도 초과" 툴 오류를 읽는 다른 실험이 되므로, 러너는 쿼터가 모자라면 시작을
# 거부하고 유효값을 매 기록의 provenance 에 박는다.
QUOTA_QUERIES_PER_DAY=2000 RATE_LIMIT_QUERY_PER_MIN=120 \
python -m eval.l3_run --out /tmp/w4.jsonl --replicates 2 --rpm 12 --resume

python -m eval.l3_run --report-only --out /tmp/w4.jsonl        # 표만 다시
python -m eval.l3_run --experiments parameter_schema,output_length   # 미측정 2종
```

2026-09-14 실측 · `gemini-3.1-flash-lite` · 온도 0.0 · **n=2** · 160 에피소드.
인자 정확도 분모가 조건마다 달라, 두 조건 모두 검색이 정답인 공통 22실행으로
맞춘 값:

| 실험 | 조건 | 툴 선택 | 인자 | 평균 호출 |
|---|---|---|---|---|
| 툴 분해 | `monolithic` (배포본) | 100% | 73% | 1.00 |
| 툴 분해 | `decomposed` | 100% | 82% | 1.23 |
| description 문구 | 기능 서술형 | 100% | 82% | 1.00 |
| description 문구 | 사용 시점 명시형 | 100% | 91% | 1.18 |

**분해의 값은 "검색을 더 잘 고른다"가 아니다.** 전체 기준 선택 정확도는
80% → 100%인데 그 상승분이 전부 fetch/list 문항 4개에서 나왔다 — 단일 툴
조건은 "그 구절 원문 보여줘"와 "내 문서 뭐뭐 있어?"에 예외 없이
`search_documents`를 불렀다(8/8). 나머지 16문항은 두 조건 모두 100%. 즉
**검색이 아닌 의도를 검색으로 오인하지 않는다**는 쪽의 이득이다.

**시나리오 라벨은 우리 구현이 아니라 사용자 입장을 정답으로 둔다.**
`expected_tool`이 툴 이름이 아니라 **역할**(`search`/`fetch`/`list`/`none`)이라
툴을 리네임해도 "행동 변화"로 집계되지 않고, 그 역할이 조건에 없으면 정답은
`none`이 된다 — 목록 툴이 없을 때 옳은 행동은 검색으로 흉내 내는 것이 아니라
못 한다고 말하는 것이다. 20문항 중 **5문항은 툴을 안 부르는 게 정답**이다.
각 문항의 `why`는 필수이고 20자 미만이면 로더가 거절한다: "우리 툴이 그렇게
생겨서"가 이유인 라벨을 쓰는 순간 드러나게 하는 장치다.

인자 정확도는 **이진**이다. 제약이 이질적이라(부분 문자열 / 정확한 UUID /
인자의 부재) 부분 점수의 "0.5"가 문항마다 다른 뜻이 되고, 20문항×n=2에서
소수점은 정밀도의 환상이다. 툴 선택이 틀렸으면 인자는 채점하지 않는다 —
같은 실패를 두 지표에 두 번 세지 않기 위해서다.

⚠️ **축소해서 쟀다.** 설계는 4실험 × n=3이었으나 무료 티어 예산으로 **2실험 ×
n=2**로 줄였다. 파라미터 스키마·출력 길이 두 실험은 코드만 있고 **한 번도 돌지
않았다**(위 `--experiments`로 바로 돈다, 추가 80 에피소드). 20문항에서 1~2개
차이는 노이즈이므로 절대 수치가 아니라 방향성으로 읽을 것. 모델 버전이 바뀌면
이전 수치와 비교가 무효라, run마다 모델명·프롬프트 해시·데이터셋 해시·git sha가
박힌다. **지연·비용은 재지 않았다** — 측정 중 이 맥에서 다른 작업이 함께 돌아
시간 값이 오염된다.

## L2 답변 품질 judge (M7 W6) — ⚠️ 아직 검증되지 않은 수치다

> **이 judge 가 내는 모든 수치는 `unvalidated` 다.** 사람 라벨과의 Cohen's
> kappa 를 아직 재지 않았고, W6 설계 노트의 표현대로 **보정 없는 judge 점수는
> 장식**이다. 이 절의 어떤 숫자도 품질 주장의 근거로 인용할 수 없다.
> 표식은 문서에만 있는 것이 아니다 — 저장 레코드의 `validation.status` 에
> 박히고, 집계 키 이름 자체가 `groundedness__unvalidated` 다. kappa 가
> 채택선(0.6)을 넘기 전까지 `groundedness` 라는 이름의 수치는 존재하지 않는다.

L1 이 검색만 재는 데 비해 L2 는 **답변**을 잰다. 차원은 M7 개요의 헤드라인
지표 그대로 groundedness · 정답성(correctness) · 거부 정확도이고, 채점은 0\~3
순서형이다.

```bash
python -m eval.l2_run rubric                                        # 루브릭 전문·버전·해시
python -m eval.l2_run score  --in /tmp/golden_records.json --out /tmp/l2.jsonl
python -m eval.l2_run report --records /tmp/l2.jsonl
python -m eval.l2_run labels --records /tmp/l2.jsonl --out /tmp/l2_labels.jsonl
python -m eval.l2_run kappa  --records /tmp/l2.jsonl --labels /tmp/l2_labels.jsonl
python -m eval.l2_run compare --base /tmp/before.jsonl --head /tmp/after.jsonl
```

`score` 만 judge 모델이 필요하다. 나머지는 DB 도 모델도 네트워크도 없이 돈다.

**judge 는 로컬 모델이다 — 청크가 이 맥을 떠나지 않는다.** 두 제약이 여기서
만난다. W6 은 피평가 모델과 다른 계열을 쓰라고 하고(생성은 Gemini), M7 개요의
미결정 항목은 "judge용 외부 LLM 호출에 문서 청크를 넣어도 되는지"를 아직 못
정했다. 로컬 judge 는 앞을 만족하면서 뒤를 없앤다 — BGE-M3 를 로컬로 돌리는
근거와 같은 문장이다.

| | 값 | 왜 |
| --- | --- | --- |
| 모델 | `qwen3:4b` (Ollama) | Qwen 계열 — Gemini 와 다른 계보. 같은 맥의 `gemma3` 는 Gemini 와 같은 구글 계보라 **쓰지 않았다** |
| 크기 | 디스크 2.5GB · 상주 3.9GB | 16GB 에 BGE-M3 + 리랭커 6.9GB 가 이미 있다. 8B 급은 같이 못 올린다 |
| 라이선스 | Apache-2.0 | 수치를 공개하는 저장소에서 판정자 라이선스가 걸리면 수치를 못 싣는다 |
| 실측(MPS) | 로드 2.2초 · 첫 판정 9.7초 · 이후 3.5\~8.4초(중앙값 5.4초) | 같은 MPS 를 모델 서버와 나눠 쓰므로 동시 부하에 흔들린다 |

모델 서버(`scripts/local_model_server.py`)에 얹지 않고 Ollama 를 따로 쓴다.
그쪽은 임베딩·리랭커를 기동 시점에 올려 상주시키는데, 세 번째 가중치를 같은
프로세스에 넣으면 16GB 에서 셋이 동시 상주하고 무엇보다 **하나뿐인 MPS 를 두고
실시간 검색과 경쟁**한다 — 이 저장소가 계속 재고 있는 지연 수치가 오염된다.

### 루브릭과 버전 태그

루브릭은 프롬프트에 **전문이 실린다**. "좋은 답"을 판정자가 안다고 가정하지
않고, 차원마다 3/2/1/0점이 무엇인지 글로 적는다. 출력은 **근거 먼저, 점수
나중**이고, 그것을 부탁이 아니라 문법으로 만든다 — 구조적 출력 스키마에서
`evidence` 가 `score` 앞에 있어 토큰 순서가 강제된다.

`rubric_version` 은 프롬프트에 실리고 레코드에 저장된다. 여기에 루브릭 본문의
**sha256** 을 함께 남긴다. 버전 문자열만 보면 "고치고 안 올린" 경우를 놓치기
때문이고, `compare` 는 둘 중 하나라도 다르면 비교를 거부한다(W3 의 `diff` 가
데이터셋 해시로 하는 것과 같다). judge 모델이 다를 때도 거부한다 — 채점자가
바뀐 것을 품질 변화로 읽을 수는 없다.

현재 버전은 `l2-ko-v2` 다. v1 에서 올라간 이유는 `eval/l2.py` 상단의 이력에
적혀 있다 — 4문항 동작 확인에서 거부 답변의 groundedness 가 0 으로 나오고(없는
주장은 근거 없는 주장이 아니다) 답을 회피한 답변에 correctness 3점이 나왔다.

A/B 비교는 **순서를 바꿔 두 번** 돌린다(위치 편향). 두 판정이 같은 *변형*을
고르면 승부이고, 같은 *자리*를 고르면 그 문항은 승부가 아니라 편향이라
`position_biased` 로 표시되고 결론에서 빠진다. 지금은 비교 대상이 없다 —
W6 의 나머지 절반(서버 생성 vs 클라이언트 생성)은 `app/mcp/` 와 함께 뒤에 온다.

### kappa 를 붙이려면 (사람이 할 일)

```bash
python -m eval.l2_run labels --records /tmp/l2.jsonl --out /tmp/l2_labels.jsonl --sample 40
# 각 행의 human_score 를 루브릭대로 채운다. judge_score 는 보지 말 것.
python -m eval.l2_run kappa --records /tmp/l2.jsonl --labels /tmp/l2_labels.jsonl
```

`labels` 는 유형을 고르게 섞어 30\~50건을 뽑는다(무작위로 뽑으면 `no_answer`
가 한두 건만 걸려 거부 정확도의 kappa 가 계산되지 않는다). 채점 함수는 이미
있고 단위 테스트도 붙어 있다 — 사람 라벨만 들어오면 한 걸음이다.

헤드라인은 **이차 가중** kappa 다. 점수가 순서형이라 3점을 2점으로 본 것과
3점을 0점으로 본 것을 같은 불일치로 셀 수 없기 때문이고, 가중 없는 값도 함께
찍는다(이차 가중은 점수가 쏠린 표본에서 후하게 나온다). `kappa` 는 채택선
0.6 미만이면 **exit 1** 이다 — 미달을 exit 0 으로 내보내면 CI 도 사람도 통과로
읽는다.

`kappa < 0.4` 면 루브릭이 모호한 것이므로 재작성하고 버전을 올린다.
`>= 0.6` 이면 프롬프트를 잠그고 레코드의 `validation` 을 채운다. 그 전까지
W6 의 대안은 "L2 는 샘플 사람 평가로 대체하고 L1·L3 만 자동화한다" 이다 —
자동화가 목적이 아니라 신뢰할 수 있는 수치가 목적이다.

**작은 로컬 모델이라 이 경고는 두 배로 무겁다.** W6 노트가 정확히 이 조합을
짚는다 — "작은 모델은 사람과의 일치도가 떨어지므로 kappa 검증이 더 중요해진다."
지금 이 judge 는 그 두 조건(작은 로컬 모델 + 미검증)을 동시에 만족한다.

## 운영

### 실제 호스트에 배포하기

지금까지는 전부 로컬(맥)을 가정한다. 실제 호스트에 올릴 때 뭐가 달라지는지,
그리고 설정으로 안 되는 것은 무엇인지를 정리한다.

#### 어디에 뭐가 뜨는가

| | 맥(로컬, 이 저장소의 기본값) | 리눅스 + GPU | 리눅스, GPU 없음 |
| --- | --- | --- | --- |
| db · app · web · proxy | 컨테이너 | 컨테이너 | 컨테이너 |
| 임베딩(BGE-M3) | **호스트**(MPS, `scripts/local_model_server.py`) | 컨테이너(`--profile gpu`의 `tei`) | 컨테이너(`models`, CPU) |
| 리랭킹(bge-reranker-v2-m3) | **호스트**(MPS, 같은 프로세스) | 컨테이너(`--profile gpu`의 `tei-rerank`) | 컨테이너(`models`, CPU) — 아래 참고 |

맥에서 모델 서버가 호스트에 있는 이유는 `scripts/stack.sh` 상단에 적힌
그대로다 — macOS Docker는 리눅스 VM에서 돌고 Metal이 전달되지 않아,
컨테이너 CPU로 리랭킹하면 실제 크기 청크 50개에 184초가 걸려 클라이언트
타임아웃(120초, `rerank.py`·`embeddings.py`에 하드코딩)을 넘긴다. **이
제약은 맥 전용이다** — 진짜 리눅스 배포에는 이 문제가 없거나(GPU 있음)
다른 모습으로 있다(GPU 없음, 아래).

리눅스 + GPU 호스트에서는 임베딩·리랭킹을 각각 컨테이너에서 GPU로 돌릴 수
있다. 단, 둘은 서로 다른 모델이라 TEI 인스턴스가 하나 더 필요하다 —
`tei`(임베딩)와 `tei-rerank`(리랭킹)를 모두 띄우고, 둘 다 가리키도록
명시적으로 지정한다:

```bash
EMBED_MODEL_URL=http://tei:80 RERANK_MODEL_URL=http://tei-rerank:80 \
  docker compose --profile gpu up -d db app web proxy tei tei-rerank
```

**서비스를 명시하는 이유:** `models` 컨테이너에는 프로파일이 없어서 그냥
`up -d` 라고 하면 GPU 프로파일에서도 함께 뜬다. 그런데 위처럼 두 URL 을
TEI 로 돌려놓으면 아무도 그 컨테이너를 부르지 않는다 — 그냥 `up -d` 로
띄우면 BGE-M3 와 리랭커를 **기동 시점에 CPU RAM 으로 올려놓고**
(`scripts/local_model_server.py` 상단: "Models load at startup and stay
resident") 한 번도 안 쓰인 채 상주한다. 가중치만 약 6.9GB 다. 위처럼
서비스를 나열하거나 `--scale models=0` 을 붙여서 빼야 한다.

(`tei-rerank`와 `EMBED_MODEL_URL`/`RERANK_MODEL_URL`은 이번에 추가됐다.
전에는 `tei` 하나만 있었는데, `MODEL_URL` 하나로 임베딩·리랭킹 주소가
같이 정해지는 구조라 `--profile gpu`를 켜도 리랭킹은 안내 없이 여전히
`models` 컨테이너의 CPU에서 돌았다 — GPU 호스트에서도 정작 리랭킹은
가속되지 않는 구성이었다.)

**리눅스인데 GPU가 없다면** — 이 구성은 정직하게 작동을 보장하지 못한다.
`models` 컨테이너는 뜨고 응답도 하지만, 184초라는 측정치는 이 저장소가
테스트한 맥의 CPU 기준이다. 클라우드 범용 vCPU가 그보다 빠르다는 보장은
없다 — 오히려 그 반대인 경우가 흔하다. 즉 184초는 바닥값이지 최악값이
아니다. 실제 크기 문서(청크당 ~700토큰)에 대한 질의는 120초 타임아웃 안에
못 끝날 위험이 크고, 이 타임아웃 자체가 지금은 환경변수가 아니라 코드에
박혀 있어 배포에서 조정할 수도 없다. 손댈 수 있는 손잡이는 둘뿐이다:

- `CAND_K`를 낮춘다(기본 40) — 리랭킹 비용은 후보 수에 선형이다. 다만
  이제는 공짜가 아니다: 실제 크기에서 40 → 20 은 R@1을 0.947에서 0.868로
  떨어뜨린다(위 "후보 수" 표). **이 호스트에서 이 손잡이를 쓴다는 것은
  정확도를 팔아 타임아웃을 피한다는 뜻**이지 최적화가 아니다.
  컨테이너 CPU 측정치는 10과 50에서만 있고(29초 / 184초) 40은 잰 값이
  아니다 — 그 둘 사이로 추정할 뿐이며, 맥 CPU 기준이라 클라우드 vCPU가
  더 빠르다는 보장도 없다.
- `RERANK_MAX_CHARS`로 입력을 자르는 것은 **손잡이가 아니다** — 측정
  결과 품질이 깨진다(`eval/rerank_truncation.py`). 시도하지 말 것.

**정직한 결론: GPU 없는 리눅스에서 실제 크기 문서를 안정적으로 리랭킹하는
구성은 지금 이 저장소에 없다.** CAND_K를 낮추는 것은 완화이지 해결이
아니다 — 코퍼스가 작다는 확신이 있거나 타임아웃 초과를 감수할 수 있을
때만 이 경로를 쓰고, 그 외에는 GPU 호스트를 전제로 계획할 것.

#### 배포 전 반드시 바꿔야 하는 값

`MAIL_PROVIDER`·`APP_BASE_URL`은 이미 위 "빠른 시작"·"설정"에 있다. 여기는
그것들을 포함해 실제 호스트로 넘어갈 때 챙겨야 하는 전체 목록이다 — 기본값
그대로 두면 각각 무엇이 어떻게 깨지는지까지.

| 변수 | 기본값 | 안 바꾸면 벌어지는 일 |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | `postgres` | 가장 먼저 시도되는 비밀번호다. DB 포트는 루프백에만 열려 있어 인터넷에서 곧장 붙지는 못하지만, 호스트에 다른 경로가 하나라도 뚫리면 그다음 문이 이거다 |
| `GEMINI_API_KEY` | (빈 값) | 앱은 정상적으로 뜨지만 첫 질의에서 500(`GEMINI_API_KEY is not set`)으로 죽는다 — 배포하고 나서야, 그것도 사용자가 먼저 발견한다 |
| `SITE_ADDRESS` | `:80` | 도메인을 안 넣으면 평문 HTTP만 나가고 Caddy가 인증서를 발급할 대상 자체가 없다. 도메인의 DNS가 이 호스트를 먼저 가리키고 있어야 한다(아래 "사람이 해야 하는 일") |
| `HTTP_PORT` / `HTTPS_PORT` | 8088 / 8443 | 80/443이 아니면 도메인의 표준 포트로 못 들어온다. 인증서 발급도 443이 실제로 열려 있어야 된다 |
| `MAIL_PROVIDER` | `console` | 가입 인증 메일이 서버 로그에만 찍히고 아무에게도 발송되지 않는다 |
| `APP_BASE_URL` | `http://localhost:3000` | 인증 메일 링크가 배포 도메인이 아니라 localhost를 가리켜 모든 가입자가 죽은 링크를 받는다 — 재발송도 같은 죽은 링크다 |
| `MAIL_FROM` | `onboarding@resend.dev` | 발신 도메인을 인증하기 전엔 계정 소유자 본인 외에는 아무도 메일을 못 받는다. **운영자 본인 계정으로 가입해서 테스트하면 성공해 보인다** — 그래서 이 문제는 다른 사람이 처음 가입할 때까지 들키지 않는다 |
| `EMBED_MODEL_URL` / `RERANK_MODEL_URL`(또는 `MODEL_URL`) | (미설정 — compose 기본은 CPU 컨테이너) | 리눅스에 GPU가 있는데 이 값을 안 맞추면 `--profile gpu`를 켜도 리랭킹은 여전히 CPU에서 돈다(위 참고). GPU가 없다면 이 값들과 무관하게 위 "GPU 없음" 항목이 적용된다 |
| `ADMIN_TOKEN` | (빈 값) | 비워두는 것 자체는 안전한 기본값이다(`/admin/stats`가 404). 운영 통계를 보고 싶을 때만 채운다 |
| `SESSION_COOKIE_SECURE` | `true` | 이미 안전한 기본값이다 — **바꾸지 말 것.** `false`로 내리면 세션 쿠키가 평문 HTTP로도 나가는데 증상이 없어 알아채지 못한다(평문으로 외부에 노출하는 예외적인 경우에만 예외) |

#### 가로로 늘릴 때 (worker·replica를 늘리는 경우)

`RATE_LIMIT_*`·쿼터는 프로세스 메모리의 토큰버킷이다(`app/services/ratelimit.py`
주석에 이미 적혀 있다). `uvicorn --workers N`이든 `docker compose up --scale
app=N`이든 app 프로세스가 여러 개가 되면 버킷도 그만큼 늘어, 설정한 한도가
조용히 N배 느슨해진다 — 에러도 로그도 없다. 지금 `Dockerfile`의 CMD는
worker 1개로 고정돼 있어 기본값 그대로는 이 문제가 없지만, 트래픽이 늘어
worker나 replica를 늘리는 순간 발생한다.

**이건 설정으로 못 고친다.** 프로세스 밖(Redis 같은 공유 저장소, 또는
프록시 앞단의 레이트리밋)으로 옮기는 코드 변경이 있어야 한다 — 여러
worker·replica로 늘릴 계획이 있다면 이 저장소가 아직 그 준비가 안 됐다는
뜻으로 읽을 것.

#### 뭐가 남고 뭐가 사라지는가 (볼륨)

| 볼륨 | 내용 | 백업 | 사라지면 |
| --- | --- | --- | --- |
| `pgdata` | Postgres(문서 메타데이터·청크·임베딩·세션·사용량) | `scripts/db_backup.sh` | 서비스 데이터 전체 손실. 복구 절차는 위 "백업과 복구 리허설" |
| `uploads` | 업로드된 PDF 원본 | **없음** | 검색·답변은 DB에 남은 청크로 계속되지만 원본 파일은 영영 사라진다. 재청킹·재색인을 하려면 원본이 있어야 하는데 그게 없다 |
| `hf-cache` | 모델 가중치(~6.9GB) | 불필요 — 재다운로드로 복구된다 | 다음 기동이 다시 느려질 뿐 |
| `caddy-data` / `caddy-config` | TLS 인증서·ACME 상태 | 불필요하지만 주의 | 인증서를 다시 발급받아야 한다. Let's Encrypt는 도메인당 발급 빈도에 상한이 있어(주당 5회), 이 볼륨을 반복해서 지우면 한동안 인증서를 못 받는 상태에 걸릴 수 있다 |

`uploads`에 백업이 없는 것은 마일스톤의 "M6 남은 것"(로컬 볼륨 → 오브젝트
스토리지)이 아직이라서다. 그 전까지는 호스트에서 이 볼륨의 실제 경로를
별도로 백업하거나, 원본 손실 위험을 감수하고 갈 것.

#### 사람이 해야 하는 일 (순서대로)

1. **호스트를 고르고 만든다.** 리눅스, Docker·Docker Compose 설치. GPU
   유무가 위 리랭킹 배치를 가른다 — 실 사용을 계획한다면 GPU가 있는
   쪽을 기본으로 생각할 것.
2. **도메인을 산다**(선택 — IP만으로도 TLS 없이는 돌아간다).
3. **DNS A(/AAAA) 레코드를 이 호스트의 공인 IP로 건다.** Caddy가 인증서를
   발급하려면 그 시점에 도메인이 실제로 이 호스트를 가리키고 있어야 한다
   — `SITE_ADDRESS`에 도메인을 넣는 것은 이게 끝난 뒤의 일이다.
4. **방화벽 / 보안그룹에서 80·443을 연다.** 인증서 발급과 이후 트래픽
   모두 이 포트로 온다. `5432`·`8080`·`8082`는 컴포즈에서 루프백에만
   묶여 있어 추가로 막을 필요는 없지만, 클라우드 보안그룹의 기본값이 더
   넓게 열려 있는 경우가 있으니 실제로 확인할 것.
5. **`GEMINI_API_KEY`를 발급받는다**(Google AI Studio). 없으면 첫 질의부터
   막힌다.
6. **실제 메일 발송이 필요하면** Resend 계정을 만들고 발신 도메인을
   등록해, 그 도메인의 DNS에 SPF/DKIM 레코드를 추가하고 검증을 기다린다.
   검증 전에는 `MAIL_PROVIDER=console`로 두거나, `resend`로 켜더라도
   본인 계정 외에는 메일이 안 간다는 것을 알고 있을 것.
7. **`ADMIN_TOKEN`을 쓰려면** 무작위 문자열을 하나 정해 넣는다(예:
   `openssl rand -hex 32`).
8. **DB 백업을 호스트 밖으로도 내보낸다.** `scripts/db_backup.sh`는 같은
   호스트의 `backups/`에 쌓는다 — 호스트 자체가 사라지는 사고에는 이것만
   으론 대비가 안 된다. 오프호스트 사본은 사람이 별도로 만들어야 한다.

1·3·5는 이 저장소 밖의 일이라 설정으로 대신할 수 없다. 나머지는 `.env`를
채우는 일이지만 순서가 있다 — 예를 들어 6을 건너뛰고 `MAIL_PROVIDER=resend`
만 켜면 위 표의 `MAIL_FROM` 문제를 그대로 맞는다.

### 백업과 복구 리허설

```bash
scripts/db_backup.sh          # backups/docs_rag-<utc>.dump (최근 7개 유지)
scripts/db_restore_check.sh   # 최신 덤프를 임시 DB에 복구해 대조
```

`db_restore_check.sh`는 **리허설이지 복구가 아니다.** 라이브 DB를 건드리지
않고 임시 DB에 복구한 뒤 행 수를 원본과 대조하고, 임베딩을 하나 읽어
1024차원인지까지 본 다음 임시 DB를 지운다. 실패하면 1로 끝난다.

한 번도 복구해보지 않은 백업은 백업이 아니다. 그 사실은 보통 필요한 날
알게 된다.

`pg_dump`·`pg_restore`는 **컨테이너 안에서** 돈다. 클라이언트가 서버보다
낮으면 덤프가 실패하는데, 컨테이너에는 항상 맞는 버전이 들어 있다.
덤프 파일은 파이프가 아니라 파일로 넘긴다 — 커스텀 포맷은 seek으로 읽어서,
멀쩡한 덤프인데도 `pg_restore --list`가 "did not find magic string"으로
실패한다.

실제 복구는 리허설과 같은 절차에 대상만 다르다:
```bash
docker compose stop app
docker compose exec -T db dropdb -U postgres --force docs_rag
docker compose exec -T db createdb -U postgres docs_rag
docker compose cp backups/<파일>.dump db:/tmp/r.dump
docker compose exec -T db pg_restore -U postgres -d docs_rag --no-owner /tmp/r.dump
docker compose start app
```

### 배포와 롤백

```bash
scripts/release.sh            # 현재 커밋 SHA 로 이미지 빌드·태그
APP_TAG=<태그> MODELS_TAG=<태그> docker compose up -d      # 배포
APP_TAG=<이전태그> MODELS_TAG=<이전태그> docker compose up -d  # 롤백
```

돌아갈 이미지가 **이름을 갖고 있어야** 롤백이 성립한다. `docker compose build`
만 쓰면 암묵적인 태그 하나를 매번 덮어써서 이전 버전이 사라진다. 그래서
compose에 `image:`를 명시하고, release 스크립트가 커밋 SHA로 태그를 남긴다.
워킹트리가 더러우면 거절한다 — SHA가 이미지 내용을 설명하지 못하면 롤백
대상이 재현 불가능해진다.

### 비용

```bash
python -m scripts.cost_report --days 30
```
알림 채널은 **종료 코드**다. `COST_CEILING`을 넘으면 1로 끝나므로, cron이나
CI가 이미 아는 방식으로 알린다 — 메일·Slack 연동을 따로 들이면 보관할
자격증명과 조용히 고장 날 곳이 하나씩 는다.

**단가는 내장하지 않는다.** `COST_PER_MTOK_IN`·`COST_PER_MTOK_OUT`이 비어
있으면 토큰 수만 보고하고 금액은 계산하지 않는다. 제공자 가격은 바뀌고,
코드에 굳어버린 옛 단가는 없는 것보다 나쁘다 — 믿게 되기 때문이다.

## 마일스톤

| | 내용 | 상태 |
| --- | --- | --- |
| M1 | 기본 RAG (청킹 → pgvector → 인용 → 거부) | ✅ |
| M2 | 하이브리드 검색 + 리랭킹 | ✅ |
| M3 | 멀티유저 (인증 · 격리 · rate limit · 쿼터 · 캐시) | ✅ |
| M4 | 평가 + 관측성 | ✅ |
| M5 | 프론트엔드/UX (스트리밍 · 각주 · 대화) | ✅ |
| M6 | 배포/운영 | 진행 중 |
| M7 | MCP 서버 노출 + 평가 하네스 | 진행 중 |

**M6 남은 것:** 업로드 저장소(로컬 볼륨 → 오브젝트 스토리지) 검토.

**M7 진행 상황** — 주차별로 쪼개져 있고 스펙은 Notion `M7` 하위 페이지에 있다.

| | 내용 | 상태 |
| --- | --- | --- |
| W1 | 업무 문서 골든셋 + Notion MCP 베이스라인 | 미착수 — 실제 업무 문서가 있어야 시작 |
| W2 | MCP 서버 최소 구현 | 구현 완료, **실사용 기록 미충족** |
| W3 | L1 평가 하네스 | ✅ |
| W4 | 툴 설계 A/B | 측정 완료 (2실험, n=2 — 축소) |
| W5 | 검색 파이프라인 A/B | ✅ |
| W6 | 생성 위치 비교 + judge | judge ⚠️ **kappa 미검증** |
| W7 | 인증 + 관측성 | 구현 완료, **실제 IdP 미연동** |
| W8 | 마무리 · 케이스 스터디 | 미착수 |

세 가지가 아직 참이 아니다. **W2의 완료 기준은 "실사용 중 불편한 지점 3개
이상 기록"**인데 아직 Claude/Cursor에 붙여 하루 써 보지 않았다. **W6의 L2
수치는 사람 라벨과의 일치도를 재지 않았다**(위 L2 절의 경고 참조). **W7의
OAuth 경로는 진짜 IdP와 맞춰 본 적이 없다** — 테스트가 자체 서명 토큰으로
검증 로직만 돌린다.

M7의 측정은 전부 **합성 코퍼스**에서 나왔다. W1의 업무 문서 골든셋이 들어오면
같은 하네스로 다시 재는 것이 설계이고, 그 전까지 이 숫자들은 방법이 도는지에
대한 증거이지 제품 품질에 대한 주장이 아니다.

### 완료 기준 (M1)
1. 업로드 후 status `processing → ready`
2. 문서로 답 가능한 질문 → 정답 + `[p.N]` 인용
3. 문서에 없는 질문 → `refused=true`
4. 인용 페이지가 실제 위치와 일치
5. 파싱 실패 파일 → `failed` + error, 서버 유지

`python -m scripts.e2e_smoke`가 위 5개를 포함해 29개 항목을 검사한다.

### 완료 기준 (M5 · 프론트엔드)
1. 드래그&드롭 업로드 → 상태 뱃지가 처리중→완료로 갱신
2. 질문 시 답변이 스트리밍으로 표시
3. 각주 번호 클릭 → 모달에 해당 청크·페이지 정확히 표시
4. 질의 범위(전체/특정 문서) 전환 동작
5. 사이드바에서 이전 대화 열람·이어보기
6. 라이트/다크 토글, 사이드바 접기/펼치기
7. 로그인 안 하면 접근 불가

## 원본 문서

- **로드맵·마일스톤 스펙·측정 결과:** Notion "📄 문서 Q&A RAG 봇"
  (D18 A/B 실험, D19 sparse 채널 비교, M6 서빙 실측 등은 각 마일스톤 하위 페이지)
- **프론트 디자인 시스템:** Notion `M5 · 프론트엔드/UX`
  (저장소 쪽 구현 결정은 `docs/superpowers/specs/2026-09-07-m5-frontend-design.md`)
- `/design` 라우트에서 프리미티브를 한자리에 볼 수 있다(개발 전용).
