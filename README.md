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
```bash
cp .env.example .env          # 값 채우기 (GEMINI_API_KEY 등)
docker compose up -d db tei   # pgvector + BGE-M3 TEI (GPU 필요)
pip install -r requirements.txt
alembic upgrade head          # 스키마 마이그레이션 (확장·테이블·HNSW)
uvicorn app.main:app --reload # 시작 시 마이그레이션 자동 적용도 됨
```

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
- `GET /health`

### SSE 두 가지 주의점

스트리밍이 시작되면 HTTP 상태는 200으로 굳는다. 그래서 429(쿼터)·500은
상태 코드가 아니라 `event: error`의 본문으로 온다.

`document_id`는 **필드의 유무**로 읽는다. 없으면 대화에 저장된 범위,
있으면 호출자가 고른 것이고 `null`도 진짜 선택(전체 문서)이다.

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
