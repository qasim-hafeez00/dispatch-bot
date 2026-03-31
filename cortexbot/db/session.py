"""
cortexbot/db/session.py

Database connection management.

Creates an async PostgreSQL connection pool that FastAPI
reuses across all requests (efficient — no reconnecting each time).

Usage in any skill or route:
    async with get_db_session() as session:
        result = await session.execute(select(Load))
        loads = result.scalars().all()
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from cortexbot.config import settings


# ── Create Database Engine ─────────────────────────────────────
# The engine manages the connection pool to PostgreSQL.
# 
# pool_size=10: Keep 10 connections open (reused across requests)
# max_overflow=20: Allow 20 extra connections during traffic spikes
# echo=True in dev: Prints all SQL queries to console (helpful for debugging)

engine = create_async_engine(
    settings.database_url,
    echo=settings.is_development,    # Print SQL in development
    pool_pre_ping=True,              # Check connection is alive before using
    pool_size=10,
    max_overflow=20,
)

# ── Session Factory ────────────────────────────────────────────
# Creates AsyncSession objects.
# expire_on_commit=False: Keep objects usable after commit
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager for database sessions.
    
    Automatically handles:
    - Session creation
    - Committing on success
    - Rolling back on error
    - Closing session when done
    
    Usage:
        async with get_db_session() as db:
            carrier = Carrier(mc_number="MC-123456", ...)
            db.add(carrier)
            # Commit happens automatically on exit
    """
    session = AsyncSessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency injection version.
    
    Use this in FastAPI routes:
        @app.get("/carriers")
        async def get_carriers(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with get_db_session() as session:
        yield session


async def init_db():
    """
    Initialize database connection on startup.
    Called from main.py lifespan.
    """
    from sqlalchemy import text

    # Test connection
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    print("✅ Database connection established")


async def close_db():
    """
    Close all database connections on shutdown.
    Called from main.py lifespan.
    """
    await engine.dispose()
    print("✅ Database connections closed")
