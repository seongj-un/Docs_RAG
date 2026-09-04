# 문서 Q&A RAG 봇 — M1

단일 유저 · PDF 1개 업로드 → 청킹 → pgvector 벡터 검색 → 인용 포함 답변 + "모른다" 가드레일.

## 스택
- Python 3.11+, FastAPI + uvicorn
- PostgreSQL 16 + pgvector, SQLAlchemy(async)
- PDF 파싱: PyMuPDF
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
uvicorn app.main:app --reload # init_db()가 확장·테이블·HNSW 생성
```
`/docs` 에서 스키마 확인.

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
pytest            # 인프라 불필요한 순수 로직(청킹·거부 가드레일) 검증
```

## 완료 기준 (M1)
1. 업로드 후 status `processing → ready`
2. 문서로 답 가능한 질문 → 정답 + `[p.N]` 인용
3. 문서에 없는 질문 → `refused=true`
4. 인용 페이지가 실제 위치와 일치
5. 파싱 실패 파일 → `failed` + error, 서버 유지
