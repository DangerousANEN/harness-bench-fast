"""FastAPI dependency injection helpers."""

from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from web.db.database import async_session


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async DB session for request-scoped use."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
