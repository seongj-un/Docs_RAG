"""Password hashing and server-side session management.

Sessions are rows in ``sessions``; the cookie carries the row id and nothing
else, so logging out (or revoking a session) is a delete — the reason the M3
spec chose sessions over JWT for document data.
"""

import uuid
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.constants import UNUSABLE_PASSWORD_HASH
from app.models import Session, User

_hasher = PasswordHasher()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-ish time check. Never raises — a malformed or sentinel hash
    (e.g. the seed account's) simply fails to verify."""
    try:
        return _hasher.verify(password_hash, password)
    except (Argon2Error, InvalidHashError):
        return False


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(
        select(User).where(User.email == normalize_email(email))
    )
    return result.scalars().first()


async def create_user(session: AsyncSession, email: str, password: str) -> User:
    user = User(email=normalize_email(email), password_hash=hash_password(password))
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def authenticate(
    session: AsyncSession, email: str, password: str
) -> User | None:
    """Return the user for valid credentials, else None.

    Runs a verification even when the email is unknown so that a missing
    account and a wrong password take similar time and are indistinguishable.
    """
    user = await get_user_by_email(session, email)
    if user is None:
        verify_password(UNUSABLE_PASSWORD_HASH, password)
        return None
    if not verify_password(user.password_hash, password):
        return None
    return user


async def create_session(session: AsyncSession, user_id: uuid.UUID) -> Session:
    row = Session(
        user_id=user_id,
        expires_at=datetime.now(timezone.utc)
        + timedelta(days=settings.session_ttl_days),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def get_session_user(
    session: AsyncSession, session_id: uuid.UUID
) -> User | None:
    """Resolve a session id to its user, or None if unknown/expired."""
    row = await session.get(Session, session_id)
    if row is None:
        return None
    if row.expires_at <= datetime.now(timezone.utc):
        await session.delete(row)
        await session.commit()
        return None
    return await session.get(User, row.user_id)


async def delete_session(session: AsyncSession, session_id: uuid.UUID) -> None:
    await session.execute(delete(Session).where(Session.id == session_id))
    await session.commit()
