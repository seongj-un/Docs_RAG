# 문서 Q&A RAG 봇 — M1

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
`/docs` 에서 스키마 확인. 스키마 변경은 Alembic으로: `alembic revision --autogenerate -m "..."` → `alembic upgrade head`.

## API
- `POST /documents` — multipart PDF 업로드 → 202 `{id, filename, status}`
- `GET /documents/{id}` — 상태 `{id, filename, num_pages, status, error}`
- `GET /documents` — 목록
- `DELETE /documents/{id}` — 문서+청크 삭제(cascade)
- `POST /query` — `{question, document_id?}` → `{answer, refused, citations[]}`
- `GET /health`

## 테스트
```bash
pip install pytest
pytest            # 인프라 불필요한 순수 로직(청킹·거부 가드레일·평가지표) 검증
```

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
