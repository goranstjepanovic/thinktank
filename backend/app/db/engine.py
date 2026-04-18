from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False},
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def _migrate_db() -> None:
    """Apply additive schema migrations for columns added after initial creation."""
    from sqlalchemy import text
    migrations = [
        "ALTER TABLE phase3_sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'classic'",
    ]
    async with engine.begin() as conn:
        for stmt in migrations:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass  # column already exists


async def init_db() -> None:
    """Create all tables on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _migrate_db()


async def get_session() -> AsyncSession:
    """FastAPI dependency for request-scoped DB sessions."""
    async with AsyncSessionLocal() as session:
        yield session
