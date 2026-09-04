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
    tei_url: str = "http://localhost:8080"

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


settings = Settings()
