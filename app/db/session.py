"""
Async SQLAlchemy session factory for the Policy & Risk Guardrails PoC.

- Uses DATABASE_URL from app.core.config.settings.
- If DATABASE_URL is a sync-style DSN (e.g. postgresql://...), it is
  automatically upgraded to an asyncpg DSN:
      postgresql://...  ->  postgresql+asyncpg://...

- Provides:
    * engine: Async engine (create_async_engine)
    * async_session: sessionmaker producing AsyncSession instances
    * get_session(): FastAPI-friendly dependency
    * get_db(): alias of get_session() for routers that expect get_db

Notes:
- pool_pre_ping=True to survive transient network hiccups (managed Postgres).
- expire_on_commit=False so loaded objects remain usable after commit.
"""

from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings


def _make_async_dsn(dsn: str) -> str:
    """
    Ensure we use an async driver DSN.

    - If already async (contains '+asyncpg'), return as-is.
    - If plain 'postgresql://', upgrade to 'postgresql+asyncpg://'.
    - Otherwise, return as-is (you can still point to other async drivers).
    """
    if not dsn:
        raise ValueError("DATABASE_URL is empty or not set in configuration.")

    # Already asyncpg-style
    if "+asyncpg://" in dsn:
        return dsn

    # Common case: psycopg-style DSN from other services
    if dsn.startswith("postgresql://"):
        upgraded = "postgresql+asyncpg://" + dsn[len("postgresql://") :]
        print(f"[db] Upgrading sync DSN to asyncpg DSN:\n  {dsn}\n  -> {upgraded}")
        return upgraded

    # Fallback: assume caller knows what they're doing
    print(f"[db] Using DATABASE_URL as-is (ensure it's an async driver): {dsn}")
    return dsn


# ----------------------------------------------------------------------
# Initialize engine & sessionmaker at import time
# ----------------------------------------------------------------------
ASYNC_DATABASE_URL: str = _make_async_dsn(settings.DATABASE_URL)

engine: AsyncEngine = create_async_engine(
    ASYNC_DATABASE_URL,
    pool_pre_ping=True,
    future=True,
)

async_session = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


# ----------------------------------------------------------------------
# FastAPI dependencies
# ----------------------------------------------------------------------
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Usage:

        @router.get("/foo")
        async def foo(db: AsyncSession = Depends(get_session)):
            ...
    """
    async with async_session() as session:
        yield session


# Backwards-compatible alias for routers that expect `get_db`
get_db = get_session
