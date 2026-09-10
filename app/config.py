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
    cand_k: int = 50         # candidates per retriever (dense top-N, sparse top-N)
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
    # 인증 메일 재발송. 자기 메일함만 채우는 행위지만 Resend 무료 한도가
    # 하루 100통이라 태울 수 있다.
    rate_limit_verify_resend_per_min: int = 1


settings = Settings()
