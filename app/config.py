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
    rerank_top: int = 8      # chunks kept after reranking (context size)

    # Generation LLM (Gemini)
    llm_provider: str = "gemini"
    llm_model: str = "gemini-3.6-flash"
    gemini_api_key: str = ""

    # Chunking (token targets, approximated by words in M1)
    chunk_size: int = 700
    chunk_overlap: int = 100

    # Retrieval
    top_k: int = 8
    min_score: float = 0.2

    # Storage (M1: local disk)
    storage_dir: str = "./storage"

    # --- M3: auth & tenant isolation ---
    session_cookie_name: str = "session_id"
    session_ttl_days: int = 14
    # MUST be true in production — a session cookie without Secure can be sent
    # over plain HTTP. Left false so local http://localhost development works.
    session_cookie_secure: bool = False
    session_cookie_samesite: str = "lax"

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
