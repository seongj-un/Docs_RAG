"""Application configuration loaded from environment / .env.

All M1 tunables (chunking, retrieval, providers) live here so services stay
free of magic numbers and the whole pipeline can be reconfigured without code
changes.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/docs_rag"

    # Embedding (BGE-M3 via TEI)
    embed_provider: str = "local"
    embed_model: str = "BAAI/bge-m3"
    embed_dim: int = 1024
    # BGE-M3 lexical (sparse) weights live over the tokenizer vocab (XLM-R = 250002).
    embed_sparse_dim: int = 250002
    tei_url: str = "http://localhost:8080"

    # --- M2: hybrid search + reranking ---
    # When true, /query uses dense+sparse RRF fusion then cross-encoder rerank.
    # When false, falls back to M1 dense-only cosine search.
    hybrid_enabled: bool = True
    rerank_url: str = "http://localhost:8081"  # bge-reranker-v2-m3 (TEI)
    rrf_k: int = 60          # RRF constant
    # Candidates each retriever (dense, sparse) contributes before RRF fusion
    # and reranking. `python -m eval.candk_sweep` on eval/corpora/wide.py (260
    # pages, built specifically so this cutoff binds) found R@1 held at 1.000
    # all the way from 50 down to 5, while reranking time fell 1226ms ->
    # 135ms (9.1x) — candidate count is what reranking time is most sensitive
    # to. Shipped 20, not 5: 5 leaves only ~1.7x headroom over the worst rank
    # the gold chunk fell to in that sweep, 20 leaves ~6.7x and still roughly
    # halves reranking time versus the old default of 50. Headroom shrinks as
    # a corpus grows, and the measurement corpus was smaller than a
    # production one would be — so the conservative number is what ships.
    #
    # ⚠️ 위 근거는 **실제 문서 크기에서 검증된 적이 없다.** 저 측정에 쓴
    # wide 는 청크가 53자, hard 는 73자다. 실제 문서(이 DB 의
    # 인공지능기본법)는 중앙 1,295자다. 리랭킹 비용은 후보 개수가 아니라
    # 입력 총길이에 지배되므로 둘은 다른 이야기다 — 2026-09-11 실측:
    #
    #   같은 CAND_K=20 에서   53자 청크 →   474ms
    #                      1,295자 청크 → 9,409ms
    #   1,295자 기준  K=10 → 6,760ms · K=5 → 1,175ms · K=3 → 696ms
    #
    # 그래서 eval/corpora/deep.py 를 만들어(wide 와 같은 조항·질의, 쪽만
    # 1,250자로 채움) 다시 쟀다. **결과가 뒤집혔다** — 2026-09-11:
    #
    #   코퍼스      K=100   K=50   K=30   K=20   K=10    K=5
    #   wide(53자)  1.000  1.000  1.000  1.000  1.000  1.000   ← 어디서 줄여도 공짜
    #   deep(1250자) 0.947  0.947  0.895  0.868  0.816  0.789   ← 줄일수록 손해
    #
    # 짧은 청크에서는 CAND_K 를 5까지 줄여도 R@1 이 1.000 이었다. 실제
    # 길이에서는 단조 감소한다. 지금 기본값 20 은 이미 R@1 0.868 — 천장
    # (후보적중)도 같은 0.868 이라, 리랭커가 떨어뜨리는 게 아니라 **검색이
    # 정답을 후보에 못 올린다.** 쪽이 길어지면 사실이 희석돼 dense 순위가
    # 나빠지기 때문이다(deep K=20 에서 dense 적중 0.737).
    #
    # 무릎은 40 이다(30~50 을 5 단위로 다시 훑음):
    #
    #   K=50  R@1 0.947  11.5초      K=35  R@1 0.895  7.7초
    #   K=45  R@1 0.947   9.9초      K=30  R@1 0.895  6.5초
    #   K=40  R@1 0.947   8.8초  ← 0.947 을 지키는 가장 싼 값
    #
    # 40 아래로 내려가는 순간 semantic 질의의 천장이 1.000 → 0.929 로
    # 꺾인다(dense 는 30 에서도 1.000 인데 sparse 가 0.714 라, 융합 후
    # 후보에 못 드는 것이 생긴다). exact 는 sparse 가 30 까지 1.000 을
    # 지켜 멀쩡하다 — 두 유형이 서로 다른 채널에 기대고 있어서, 깊이를
    # 줄이면 semantic 쪽이 먼저 무너진다.
    #
    # 즉 이 설정은 더 이상 "공짜 속도"가 아니라 정확도-지연 교환이다:
    #   K=20 → 4.4초 / R@1 0.868      K=40 → 8.8초 / R@1 0.947
    # 8점을 4.4초에 사는 셈이다. **기본값은 20 으로 두되 잠정이다** —
    # 어느 쪽을 살지는 제품 결정이고, 리랭킹 자체를 싸게 만드는 쪽
    # (ONNX int8: CPU 로 GPU 속도, 실제 길이 품질은 미측정)이 먼저
    # 풀리면 교환 자체를 피할 수 있다.
    cand_k: int = 20         # candidates per retriever (dense top-N, sparse top-N)
    # Context size after reranking. Was 8; the M4 golden-set evaluation measured
    # context_precision 0.246 at that size (7 of 8 chunks typically irrelevant)
    # while R@3 was 1.000 — the needed page was always within the top 3, so the
    # extra five were pure noise.
    rerank_top: int = 3
    # Grounding floor for the reranked path. Separate from MIN_SCORE because the
    # scores are not the same quantity: MIN_SCORE gates cosine similarity, this
    # gates the cross-encoder's sigmoid. Reusing 0.2 for both (the M2 decision)
    # deterministically refused 21% of answerable questions — the reranker scores
    # correct-but-reworded matches low (e.g. "강아지" vs "반려동물" -> 0.038),
    # while genuinely unanswerable questions score ~0.000-0.002. 0.005 sits in
    # that gap. It is a cheap pre-filter, not the refusal decision: borderline
    # cases still reach the LLM, which is instructed to refuse ungrounded asks.
    rerank_min_score: float = 0.005
    # Characters of each candidate sent to the cross-encoder. Reranking cost is
    # linear in input length up to the model's own cut-off, so this is the
    # largest single lever on query latency. 0 disables truncation.
    # Only the scoring input is shortened — stored content, the generation
    # context, and citation snippets are untouched.
    rerank_max_chars: int = 0

    # Generation LLM (Gemini)
    llm_provider: str = "gemini"
    # ⚠️ 무료 티어에서는 이 모델을 대화형으로 쓸 수 없다 — 실측으로 한 문장
    # 응답에 34~40초, 그리고 하루 20회에서 429. 같은 요청이 flash-lite 로는
    # 0.9초다. 무료 키로 돌린다면 LLM_MODEL 을 gemini-3.1-flash-lite 로 두고,
    # 유료 티어에서 이 기본값으로 돌아올 것.
    llm_model: str = "gemini-3.6-flash"
    # Evaluation runs on a separate, lighter model. The flagship Flash tier
    # allows only 20 requests/day free, which a 36-question sweep exhausts
    # before it finishes; Flash-Lite has room for repeated before/after runs.
    # Production generation is unaffected by this setting.
    eval_llm_model: str = "gemini-3.1-flash-lite"
    gemini_api_key: str = ""

    # Chunking (token targets, approximated by words in M1)
    chunk_size: int = 700
    chunk_overlap: int = 100

    # Retrieval
    top_k: int = 8
    min_score: float = 0.2

    # Storage (M1: local disk)
    storage_dir: str = "./storage"

    # Operational statistics endpoint. Empty disables it entirely — the route
    # then 404s rather than 401s, so an unconfigured deployment does not
    # advertise that an admin surface exists at all.
    admin_token: str = ""

    # Token spend accounting for scripts/cost_report.py. Rates default to 0 —
    # this project does not guess what the provider charges, because a made-up
    # number in a cost alert is worse than no alert. Fill them in from the
    # provider's pricing page, per one million tokens.
    cost_per_mtok_in: float = 0.0
    cost_per_mtok_out: float = 0.0
    cost_currency: str = "USD"
    # Spend over the report window that should trip an alert. 0 disables.
    cost_ceiling: float = 0.0

    # --- M3: auth & tenant isolation ---
    session_cookie_name: str = "session_id"
    session_ttl_days: int = 14
    # MUST be true in production — a session cookie without Secure can be sent
    # over plain HTTP. Left false so local http://localhost development works.
    # 기본을 True 로 둔다. 잊었을 때 안전한 쪽으로 실패해야 한다 — 반대로
    # 두면 공개 배포에서 세션 쿠키가 평문 HTTP 로도 나가는데 아무 증상이 없다.
    # 이 값이 True 여도 브라우저는 http://localhost 를 신뢰 가능한 출처로
    # 취급하므로 로컬 개발은 그대로 된다. 평문 HTTP 로 외부에 노출하는
    # 예외적인 경우에만 false 로 내린다.
    session_cookie_secure: bool = True
    session_cookie_samesite: str = "lax"
    # M5 frontend runs on its own origin and must send the session cookie, so
    # credentials are allowed — which forbids a "*" origin. List them exactly.
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # --- M3 Phase 2: abuse & cost defense ---
    # Rate limits are per-process token buckets (see services/ratelimit.py):
    # correct for a single instance, multiplied by N behind N workers.
    rate_limit_query_per_min: int = 20
    rate_limit_upload_per_min: int = 5
    # 로그인·가입 시도. 업로드·질의에만 제한이 있고 인증에는 없어서, 8자
    # 비밀번호에 무제한으로 시도할 수 있었다.
    rate_limit_auth_per_min: int = 10
    # Quotas are counted in the database, so they hold across processes.
    quota_queries_per_day: int = 200
    quota_upload_pages_per_month: int = 1000
    # usage.acquire_quota_lock 이 잠금을 쥔 김에 이 사용자의 이 kind 에 대해
    # settled_at 이 없는(예약 중) 채로 이 시간보다 오래된 usage_events 행을
    # 지운다 — reserve() 를 심은 프로세스가 commit_reservation/
    # release_reservation 에 이르기 전에 죽었다는 뜻이라, 그 슬롯을 영원히
    # 갉아먹게 둘 이유가 없다(미인증 계정의 맛보기 한도는 창이 없어 굴러
    # 넘어가지 않으므로 특히 그렇다). 진행 중인 정상 요청보다 짧게 잡으면
    # 그 요청의 예약을 다음 동시 요청이 스윕해 지워버려 쿼터가 새는 쪽으로
    # 사고가 난다 — 값은 반드시 "가장 긴 정상 요청"보다 넉넉히 커야 한다.
    # embed()/rerank() 는 각각 HTTP 클라이언트 타임아웃이 120초(하드코딩,
    # rerank.py/embeddings.py)이고 한 요청 안에서 순서대로 둘 다 겪을 수
    # 있다 — 240초. 여기에 LLM 호출 실측치(34~40초)를 더하면 최악의 "그래도
    # 정상적으로 끝나는" 요청이 300초 안팎이 된다. 600초는 그 두 배에 가까운
    # 여유이면서도, 진짜 죽은 예약이 다음 사용까지 10분 넘게 계정을 잠그지는
    # 않는다.
    reservation_ttl_seconds: int = 600
    # Upload guards, checked before any embedding spend.
    max_upload_mb: int = 50
    max_upload_pages: int = 500
    # Semantic cache: reuse a past answer when a new question is near-identical.
    semantic_cache_enabled: bool = True
    semantic_cache_threshold: float = 0.95

    # --- 이메일 인증 ---
    # console 은 링크를 로그로만 찍는다. RESEND_API_KEY 가 비어 있으면
    # resend 로 설정돼 있어도 console 로 내려온다 — 키 없이 clone 해도
    # 앱이 뜨고 테스트가 돌아야 하기 때문이다.
    mail_provider: str = "console"
    resend_api_key: str = ""
    # 도메인이 없으면 Resend 는 이 주소로만 보낼 수 있고, 수신도 계정
    # 소유자 본인에게만 전달된다. 공개 배포 시 no-reply@<도메인> 으로 바꾼다.
    mail_from: str = "onboarding@resend.dev"
    # 인증 링크가 가리키는 프론트엔드 오리진. 백엔드가 아니다 — 메일 링크는
    # /verify 페이지로 가고 그 페이지가 POST 를 친다.
    app_base_url: str = "http://localhost:3000"
    verify_token_ttl_hours: int = 24
    # 미인증 계정의 맛보기 한도. 기존 쿼터와 달리 **계정 수명 전체 누적**이다.
    # "하루 5회"로 두면 미인증 계정이 매일 5회씩 영원히 쓸 수 있어, 막으려던
    # 재가입 어뷰즈가 그대로 통과한다. 0 이면 게이트를 끈다.
    unverified_quota_queries: int = 5
    unverified_quota_documents: int = 1
    # 문서 개수만 세면 500쪽짜리 PDF 한 장으로 "문서 1개" 한도를 다 채우면서
    # 그 안에서 500쪽 분량의 BGE-M3 임베딩을 태울 수 있다 — 비용은 문서
    # 수가 아니라 쪽수에 비례하는데 문서 게이트는 쪽수를 전혀 보지 않는다.
    # 그래서 별도 쪽수 상한을 둔다. 0 이면 게이트를 끈다.
    unverified_quota_pages: int = 50
    # 인증 메일 재발송. 자기 메일함만 채우는 행위지만 Resend 무료 한도가
    # 하루 100통이라 태울 수 있다.
    rate_limit_verify_resend_per_min: int = 1


settings = Settings()
