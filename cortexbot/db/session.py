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
# SQLite (local dev): NullPool + no pool_size — SQLite is file-based
# PostgreSQL (production): connection pool with pre-ping

_is_sqlite = settings.database_url.startswith("sqlite")

if _is_sqlite:
    engine = create_async_engine(
        settings.database_url,
        echo=settings.is_development,
        poolclass=NullPool,
    )
else:
    engine = create_async_engine(
        settings.database_url,
        echo=settings.is_development,
        pool_pre_ping=True,
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

    For SQLite (USE_MOCKS=true), creates the minimal tables needed for
    mock testing using raw DDL. We cannot use create_all() here because
    the ORM models reference postgresql.UUID and postgresql.JSONB which
    have no SQLite equivalent.
    """
    from sqlalchemy import text

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    if _is_sqlite:
        await _create_minimal_sqlite_schema()

    print("✅ Database connection established")


_SQLITE_DDL = [
    """CREATE TABLE IF NOT EXISTS carriers (
        carrier_id TEXT PRIMARY KEY,
        mc_number TEXT UNIQUE NOT NULL,
        company_name TEXT NOT NULL,
        owner_name TEXT NOT NULL,
        owner_email TEXT NOT NULL,
        owner_phone TEXT NOT NULL DEFAULT '',
        driver_phone TEXT,
        whatsapp_phone TEXT,
        language_pref TEXT DEFAULT 'en',
        equipment_type TEXT NOT NULL DEFAULT '53_dry_van',
        max_weight_lbs INTEGER DEFAULT 44000,
        home_base_city TEXT,
        home_base_state TEXT,
        rate_floor_cpm REAL NOT NULL DEFAULT 2.50,
        max_deadhead_mi INTEGER DEFAULT 100,
        status TEXT DEFAULT 'ACTIVE',
        dispatch_fee_pct REAL DEFAULT 0.060,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS brokers (
        broker_id TEXT PRIMARY KEY,
        mc_number TEXT UNIQUE NOT NULL,
        company_name TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS loads (
        load_id TEXT PRIMARY KEY,
        tms_ref TEXT UNIQUE,
        carrier_id TEXT REFERENCES carriers(carrier_id),
        broker_id TEXT,
        status TEXT DEFAULT 'SEARCHING',
        bland_call_id TEXT,
        broker_called_at TEXT,
        origin_city TEXT,
        origin_state TEXT,
        destination_city TEXT,
        destination_state TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS call_log (
        call_id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
        bland_ai_call_id TEXT UNIQUE,
        load_id TEXT REFERENCES loads(load_id),
        carrier_id TEXT REFERENCES carriers(carrier_id),
        broker_phone TEXT,
        outcome TEXT,
        agreed_rate_cpm REAL,
        call_duration_sec INTEGER,
        recording_url TEXT,
        transcript_raw TEXT,
        extracted_data TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
        event_code TEXT NOT NULL,
        entity_type TEXT,
        entity_id TEXT,
        triggered_by TEXT,
        actor TEXT DEFAULT 'cortex-bot',
        data TEXT,
        previous_status TEXT,
        new_status TEXT,
        notes TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
]


async def _create_minimal_sqlite_schema():
    """Create the tables the mock test loop needs — without PG-specific types."""
    from sqlalchemy import text
    async with engine.begin() as conn:
        for stmt in [
            "DROP TABLE IF EXISTS events",
            "DROP TABLE IF EXISTS call_log",
            "DROP TABLE IF EXISTS loads",
            "DROP TABLE IF EXISTS brokers",
            "DROP TABLE IF EXISTS carriers",
        ]:
            await conn.execute(text(stmt))
        for stmt in _SQLITE_DDL:
            await conn.execute(text(stmt))
    print("✅ SQLite schema created (minimal mock DDL)")


async def close_db():
    """
    Close all database connections on shutdown.
    Called from main.py lifespan.
    """
    await engine.dispose()
    print("✅ Database connections closed")
