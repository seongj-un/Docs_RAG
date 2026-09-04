"""Request/response models for the M1 API surface."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DocumentCreated(BaseModel):
    """202 response after an upload is accepted for background indexing."""

    id: uuid.UUID
    filename: str
    status: str


class DocumentStatus(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    num_pages: int | None = None
    status: str
    error: str | None = None
    created_at: datetime | None = None


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1)
    document_id: uuid.UUID | None = None


class Citation(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    page_from: int | None = None
    page_to: int | None = None
    snippet: str


class QueryResponse(BaseModel):
    answer: str
    refused: bool
    citations: list[Citation]
