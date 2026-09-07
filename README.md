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
docker compose up -d     # db · 모델 · 앱 · 프록시
curl http://localhost:8088/health
```

첫 기동은 모델 가중치 약 4.6GB를 받는다(`hf-cache` 볼륨에 남아 이후엔 즉시).
그동안에도 앱은 이미 떠 있고, 질의는 503 `search unavailable`로 답한다 —
모델을 기다리느라 스택 전체가 멎지는 않는다.

| 변수 | 기본 | 뜻 |
| --- | --- | --- |
| `HTTP_PORT` / `HTTPS_PORT` | 8088 / 8443 | 프록시 호스트 포트. 운영에선 80/443 |
| `SITE_ADDRESS` | `:80` | 도메인을 넣으면 Caddy가 인증서를 자동 발급 |
| `ADMIN_TOKEN` | (빈 값) | 비우면 `/admin/stats`가 404 |
| `MODEL_THREADS` | 4 | 모델 컨테이너의 CPU 스레드 |

**모델 서비스는 CPU로 돈다.** macOS Docker는 리눅스 VM에서 돌고 Metal이
전달되지 않아, 컨테이너는 이 맥의 GPU를 쓸 수 없다. NVIDIA 호스트라면
`docker compose --profile gpu up`으로 TEI를 대신 쓴다. 애플 실리콘에서 GPU로
돌리려면 아래 "로컬 GPU 모델 서버"를 쓴다.

정리는 `docker compose down`. **`-v`는 붙이지 말 것** — `pgdata`(DB)·
`hf-cache`(가중치 4.6GB)·`uploads`(업로드 원본)가 함께 사라진다.

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
백엔드의 `cors_origins`에 이 오리진이 있어야 세션 쿠키가 오간다.
Turbopack dev가 포트를 잡지 못하는 환경에서는 `npm run dev:webpack`.

## 구조

```
app/
├── main.py           # 앱 · lifespan(run_migrations) · CORS
├── config.py         # 모든 튜너블 (pydantic-settings)
├── models.py         # Document, Chunk, User, Session, Trace, Conversation ...
├── deps.py           # 인증 의존성
├── routers/          # admin auth chunks conversations documents query traces usage
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
    └── tracing.py    # 단계별 지연 기록
alembic/versions/     # 마이그레이션 7개
web/                  # Next.js 16 App Router · TypeScript · Tailwind v4
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

필요한 숫자는 이미 `traces`·`usage_events`에 다 있었다. 없던 것은 수집
파이프라인이 아니라 **읽을 창구**였다(M4에서 Langfuse를 기각한 논리 그대로).

- `total_ms`는 **캐시 히트 포함** — 사용자가 실제로 기다린 시간.
- `stages`는 **캐시 히트 제외** — 히트는 임베딩·리랭킹·생성을 통째로
  건너뛰므로, 섞으면 모든 단계가 실제보다 빨라 보인다.
- `failures`는 error 문자열의 콜론 앞부분으로 묶는다. 원인은 구분돼야 하고
  파일명은 보고서에 새면 안 된다.

`ADMIN_TOKEN`이 비어 있으면 401이 아니라 **404**다. 401은 설정한 적 없는
배포에서도 이 엔드포인트의 존재를 알려주기 때문이다.

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
| `TEI_URL` · `RERANK_URL` | :8080 · :8081 | 임베딩·리랭커 서버 |
| `GEMINI_API_KEY` | — | 필수 |
| `LLM_MODEL` | `gemini-3.6-flash` | 생성 모델 |
| `EVAL_LLM_MODEL` | `gemini-3.1-flash-lite` | 평가용. 무료 티어가 `3.6-flash`는 **하루 20회**라 분리했다 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 700 / 100 | 토큰 단위 |
| `CAND_K` | 50 | 리랭커에 넘길 후보 수 (아래 평가 참조) |
| `RERANK_TOP` | 3 | 생성에 넘길 청크 수 |
| `RERANK_MIN_SCORE` | 0.005 | 리랭커 점수 하한. **`MIN_SCORE`와 다른 값이다** |
| `RERANK_MAX_CHARS` | 0 (끔) | 리랭커 입력 자르기. 측정 결과 켜면 안 된다 |
| `RATE_LIMIT_*` · `QUOTA_*` | 20/분 · 200/일 · 1000쪽/월 | 남용 방지 |
| `SEMANTIC_CACHE_THRESHOLD` | 0.95 | 코사인 유사도 |

> `MIN_SCORE`(코사인)를 리랭커 시그모이드에 재사용했다가 답변 가능한 질문의
> 21%를 LLM 호출도 없이 거부한 적이 있다. 두 점수는 같은 양이 아니다.

## 테스트

```bash
pytest                  # 순수 로직 + Postgres가 있으면 통합 테스트까지
npm --prefix web test   # 프론트 순수 로직 (각주 매핑·SSE 파서·에러 문구)
npm --prefix web run build   # 타입 검사 포함 — vitest는 타입을 보지 않는다
python -m scripts.e2e_smoke  # 실제 HTTP로 전 구간 (서버·DB·모델·LLM 필요)
```
통합 테스트는 Postgres에 닿지 못하면 스킵된다. 스모크는 `--base`로 대상을
바꿀 수 있다(예: 컨테이너 스택 `--base http://localhost:8088`).

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
python -m eval.candk_sweep --corpus wide   # 260쪽, CAND_K가 실제로 물림
```
`wide`도 이 실험용이다 — 다른 코퍼스는 전부 `CAND_K`(50)보다 작아 전 청크가
항상 후보가 되므로 이 설정이 아무 일도 하지 않는다.

읽는 법: **후보 적중률이 R@1의 천장이다.** 후보에 없으면 리랭커가 복구할 수
없다. 실제로 이 설정을 좌우하는 값은 두 채널 중 좋은 쪽의 순위
`min(dense, sparse)`이므로, 스윕 끝의 여유 표를 함께 볼 것. RRF 순위는
천장이 아니다 — 융합의 순서일 뿐이고 리랭커가 다시 정렬한다.

### 그 밖

```bash
python -m eval.sparse_channels   # lexical 채널 비교 (fts / trgm / bge)
python -m eval.golden_run        # 골든셋 3문서·36문항 → 기록 덤프
python -m eval.judge_run         # 위 기록으로 4개 품질 지표 산출
```

## 운영

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

## 마일스톤

| | 내용 | 상태 |
| --- | --- | --- |
| M1 | 기본 RAG (청킹 → pgvector → 인용 → 거부) | ✅ |
| M2 | 하이브리드 검색 + 리랭킹 | ✅ |
| M3 | 멀티유저 (인증 · 격리 · rate limit · 쿼터 · 캐시) | ✅ |
| M4 | 평가 + 관측성 | ✅ |
| M5 | 프론트엔드/UX (스트리밍 · 각주 · 대화) | ✅ |
| M6 | 배포/운영 | 진행 중 |

**M6 남은 것:** DB 백업 + 복구 리허설 · 배포 롤백 경로 · 비용 알림 ·
업로드 저장소(로컬 볼륨 → 오브젝트 스토리지) 검토.

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
