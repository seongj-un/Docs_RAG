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
    session_cookie_secure: bool = False
    session_cookie_samesite: str = "lax"
    # M5 frontend runs on its own origin and must send the session cookie, so
    # credentials are allowed — which forbids a "*" origin. List them exactly.
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # --- M3 Phase 2: abuse & cost defense ---
    # Rate limits are per-process token buckets (see services/ratelimit.py):
    # correct for a single instance, multiplied by N behind N workers.
    rate_limit_query_per_min: int = 20
    rate_limit_upload_per_min: int = 5
    # Quotas are counted in the database, so they hold across processes.
    quota_queries_per_day: int = 200
    quota_upload_pages_per_month: int = 1000
    # Upload guards, checked before any embedding spend.
    max_upload_mb: int = 50
    max_upload_pages: int = 500
    # Semantic cache: reuse a past answer when a new question is near-identical.
    semantic_cache_enabled: bool = True
    semantic_cache_threshold: float = 0.95


settings = Settings()
