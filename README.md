# 문서 Q&A RAG 봇

단일 유저 · PDF 1개 업로드 → 청킹 → pgvector 벡터 검색 → 인용 포함 답변 + "모른다" 가드레일.

## 스택
- Python 3.11+, FastAPI + uvicorn
- PostgreSQL 16 + pgvector, SQLAlchemy(async) + Alembic
- PDF 파싱: PyMuPDF / 청킹: BGE-M3 토크나이저 기반 토큰 윈도우(페이지 단위)
- 임베딩: BGE-M3 (TEI 서버, dense 1024) / 생성: Gemini (Flash)

## 구조
```
app/
├── main.py         # 앱 · lifespan(init_db)
├── config.py       # 환경변수
├── db.py           # async 엔진/세션 · init_db
├── models.py       # Document, Chunk(pgvector)
├── schemas.py      # 요청/응답
├── routers/        # documents.py, query.py
└── services/       # ingest, chunking, embeddings, retrieve, generate, llm
```

## 실행

### 전체 스택 (Docker)
```bash
cp .env.example .env     # GEMINI_API_KEY 등을 채운다
docker compose up -d     # db · 모델 · 앱 · 프록시
curl http://localhost:8088/health
```
첫 기동은 모델 가중치 약 4.6GB를 받는다(`hf-cache` 볼륨에 남아 이후엔 즉시).
그동안 앱은 이미 떠 있고, 질의는 503 `search unavailable`로 답한다 — 모델이
준비되기를 기다리느라 스택 전체가 멎지는 않는다.

| 변수 | 기본 | 뜻 |
| --- | --- | --- |
| `HTTP_PORT` / `HTTPS_PORT` | 8088 / 8443 | 프록시 호스트 포트. 운영에선 80/443 |
| `SITE_ADDRESS` | `:80` | 도메인을 넣으면 Caddy가 인증서를 자동 발급 |
| `ADMIN_TOKEN` | (빈 값) | 비우면 `/admin/stats`가 404 |
| `MODEL_THREADS` | 4 | 모델 컨테이너의 CPU 스레드 |

**모델 서비스는 CPU다.** macOS Docker는 리눅스 VM에서 돌고 Metal이 전달되지
않아 컨테이너가 이 맥의 GPU를 못 쓴다. NVIDIA 호스트라면
`docker compose --profile gpu up`으로 TEI를 대신 쓴다. 애플 실리콘에서 GPU로
돌리려면 아래 호스트 실행을 쓴다.

### 개발 (호스트에서 직접)
```bash
docker compose up -d db       # Postgres만
pip install -r requirements.txt
alembic upgrade head          # 시작 시 자동 적용도 됨
uvicorn app.main:app --reload
```

### 임베딩·리랭킹 서버를 로컬 GPU로 (macOS/Apple Silicon)

`docker compose`의 `tei`는 NVIDIA GPU를 요구한다. macOS 컨테이너에는 Metal이
전달되지 않으므로, 맥에서는 도커 대신 호스트에서 직접 띄운다.

```bash
pip install -r requirements-bench.txt   # torch·FlagEmbedding (앱 의존성 아님)
python -m scripts.local_model_server     # http://127.0.0.1:8081, MPS 자동 선택
```
`.env`에서 `TEI_URL`·`RERANK_URL`을 이 주소로 두면 앱 코드는 그대로다 —
두 서버 모두 같은 `/embed`·`/embed_full`·`/rerank` 계약을 쓴다.
앱은 torch를 임포트하지 않고 HTTP로만 부르므로 `requirements.txt`는 그대로 둔다.

프론트엔드:
```bash
cd web
npm install
cp .env.example .env.local   # NEXT_PUBLIC_API_BASE
npm run dev                  # http://localhost:3000
```
백엔드의 `cors_origins`에 이 오리진이 있어야 세션 쿠키가 오간다.
Turbopack dev가 포트를 잡지 못하는 환경에서는 `npm run dev:webpack`.
`/docs` 에서 스키마 확인. 스키마 변경은 Alembic으로: `alembic revision --autogenerate -m "..."` → `alembic upgrade head`.

## API
- `POST /documents` — multipart PDF 업로드 → 202 `{id, filename, status}`
- `GET /documents/{id}` — 상태 `{id, filename, num_pages, status, error}`
- `GET /documents` — 목록
- `DELETE /documents/{id}` — 문서+청크 삭제(cascade)
- `POST /query` — `{question, document_id?}` → `{answer, refused, citations[]}`
- `GET /chunks/{id}` — 청크 원문 (근거 모달용, 소유자 한정)
- `GET /usage` — 오늘 질문 수·이번 달 쪽수와 각 한도
- `GET /conversations` · `POST /conversations` — 대화 목록·생성
- `GET /conversations/{id}` · `DELETE /conversations/{id}` — 메시지 이력·삭제
- `POST /conversations/{id}/query` — SSE 스트리밍 질의
  (`meta` → `token`* → `done`, 언제든 `error`)
- `GET /traces` · `GET /traces/{id}` — 질의 진단 기록
- `GET /admin/stats` — 운영 통계 (헤더 `X-Admin-Token`)
- `GET /health`

### `/admin/stats` 읽는 법

M4에서 Langfuse를 기각한 이유가 여기에도 그대로 적용된다 — 필요한 숫자는
이미 `traces`·`usage_events`에 다 있고, 없던 것은 수집 파이프라인이 아니라
**읽을 창구**였다.

- `total_ms`는 **캐시 히트 포함** — 사용자가 실제로 기다린 시간이다.
- `stages`는 **캐시 히트 제외** — 히트는 임베딩·리랭킹·생성을 통째로 건너뛰므로,
  섞으면 모든 단계가 실제보다 빨라 보인다.
- `failures`는 error 문자열의 콜론 앞부분으로 묶는다. "글자 없는 PDF"와
  "임베딩 서버 다운"이 구분돼야 하고, 파일명은 보고서에 새면 안 된다.

`ADMIN_TOKEN`이 비어 있으면 401이 아니라 **404**다. 401은 설정한 적 없는
배포에서도 이 엔드포인트의 존재를 알려주기 때문이다.

### SSE 두 가지 주의점

스트리밍이 시작되면 HTTP 상태는 200으로 굳는다. 그래서 429(쿼터)·500은
상태 코드가 아니라 `event: error`의 본문으로 온다.

`document_id`는 **필드의 유무**로 읽는다. 없으면 대화에 저장된 범위,
있으면 호출자가 고른 것이고 `null`도 진짜 선택(전체 문서)이다.

### 모델 제공자 쪽 실패는 500이 아니다

Gemini의 무료 티어 한도(429)와 과부하(503)는 이 서버의 결함이 아니라
남의 형편이다. 그대로 두면 `Internal Server Error` 한 줄로 나가서 원인도
다음 행동도 알려주지 못하므로, 각각 429 `model quota exceeded` ·
503 `model unavailable`로 옮겨 담는다. 제공자가 재시도 시각을 알려줄 때만
`Retry-After`를 붙인다(일일 한도에는 안 붙는다 — 답이 "내일"이라서).
그 밖의 실패는 그대로 터뜨린다. 임시 장애로 위장하면 진짜 버그가 숨는다.

## 테스트
```bash
pip install pytest
pytest            # 순수 로직 + Postgres가 있으면 통합 테스트까지
npm --prefix web test   # 각주 매핑 등 프론트 순수 로직
```
통합 테스트는 Postgres에 닿지 못하면 스킵된다.

## 검색 품질 평가
라벨링된 코퍼스로 검색 구성(dense / hybrid / hybrid+rerank)을 정량 비교한다.
생성은 제외하고 검색 단계만 측정한다.
```bash
python -m eval.run --corpus hard   # 40p·20질의, 혼동 후보 포함 (판별력 있음)
python -m eval.run --corpus simple # 15p·15질의, 전 구성 만점 = 판별 불가(음성 대조군)
```

### 리랭커 입력 자르기 (`RERANK_MAX_CHARS`)

리랭킹 비용은 입력 길이에 선형이라 자르기가 가장 큰 지연 지렛대지만,
**측정 결과 품질을 깨뜨려서 기본값은 0(끔)이다.**
```bash
python -m eval.rerank_truncation --corpus longchunk  # 사실 위치별 손실 측정
python -m eval.rerank_truncation --corpus hard       # 음성 대조군
```
`longchunk`은 이 실험을 위해 만든 코퍼스다. 다른 코퍼스는 조항이 전부
128자 미만이라 **자르기가 아무 일도 하지 않아** 손실을 측정할 수 없다.
깊이별로 나눠 읽을 것 — 평균은 효과를 가린다. 결과는 Notion M6 페이지 참조.

### 후보 수 (`CAND_K`)

리랭커에 넘길 후보 수. 자르기와 달리 청크 안 정보를 지우지 않고 개수만 줄인다.
```bash
python -m eval.candk_sweep --corpus wide   # 260쪽, CAND_K가 실제로 물림
```
`wide`도 이 실험용이다 — 다른 코퍼스는 전부 `CAND_K`(50)보다 작아
전 청크가 항상 후보가 되므로 이 설정이 아무 일도 하지 않는다.

읽는 법: **후보 적중률이 R@1의 천장이다.** 후보에 없으면 리랭커가 복구할 수
없다. 그리고 실제로 이 설정을 좌우하는 값은 **두 채널 중 좋은 쪽의 순위**
`min(dense, sparse)`이므로, 스윕 끝의 여유 표를 함께 볼 것.
Postgres + 임베딩/리랭커 서버(`TEI_URL`, `RERANK_URL`)가 필요하다.
결과 해석은 Notion M2 페이지의 D18 항목 참조.

## 완료 기준 (M1)

1. 업로드 후 status `processing → ready`
2. 문서로 답 가능한 질문 → 정답 + `[p.N]` 인용
3. 문서에 없는 질문 → `refused=true`
4. 인용 페이지가 실제 위치와 일치
5. 파싱 실패 파일 → `failed` + error, 서버 유지

## 완료 기준 (M5 · 프론트엔드)
1. 드래그&드롭 업로드 → 상태 뱃지가 처리중→완료로 갱신
2. 질문 시 답변이 스트리밍으로 표시
3. 각주 번호 클릭 → 모달에 해당 청크·페이지 정확히 표시
4. 질의 범위(전체/특정 문서) 전환 동작
5. 사이드바에서 이전 대화 열람·이어보기
6. 라이트/다크 토글, 사이드바 접기/펼치기
7. 로그인 안 하면 접근 불가

디자인 시스템(색 토큰·타이포·컴포넌트 규칙)의 원본은 Notion `M5 · 프론트엔드/UX`.
저장소 쪽 구현 결정은 `docs/superpowers/specs/2026-09-07-m5-frontend-design.md`.
`/design` 라우트에서 프리미티브를 한자리에 모아 볼 수 있다.
