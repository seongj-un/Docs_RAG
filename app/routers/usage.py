"""Usage endpoint.

The settings screen shows what the account has spent against its quotas. The
counting already lives in ``services.usage`` (aggregated from ``usage_events``,
so it holds across processes); this route only exposes it.

Limits are returned alongside the counts rather than pre-computed into a
percentage, so the caller can say how much is left in its own words.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.deps import get_current_user
from app.models import User
from app.schemas import UsageOut
from app.services import usage as usage_service

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("", response_model=UsageOut)
async def get_usage(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UsageOut:
    return UsageOut(
        queries_today=await usage_service.queries_today(session, user.id),
        queries_per_day=settings.quota_queries_per_day,
        pages_this_month=await usage_service.pages_this_month(session, user.id),
        pages_per_month=settings.quota_upload_pages_per_month,
        email_verified=user.email_verified,
        queries_total=await usage_service.queries_total(session, user.id),
        documents_total=await usage_service.documents_total(session, user.id),
        unverified_query_limit=settings.unverified_quota_queries,
        unverified_document_limit=settings.unverified_quota_documents,
        pages_uploaded_total=await usage_service.pages_uploaded_total(
            session, user.id
        ),
        unverified_page_limit=settings.unverified_quota_pages,
    )
