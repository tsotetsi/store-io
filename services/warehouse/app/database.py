from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import text

from warehouse.app.config import get_warehouse_settings

settings = get_warehouse_settings()

# Async Database Engine for no-blocking operations on PostgreSQL 18+.
engine = create_async_engine(settings.database_url, echo=settings.DEBUG)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

Base = declarative_base()


async def get_db() -> AsyncGenerator[AsyncSession]:
    """
    Dependency injection helper to manage clean session lifecycles.

    Usage:
        @router.get("/stock")
        async def get_stock(db: AsyncSession = Depends(get_db)):
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_database_health() -> dict:
    """Check if the database is reachable and responding."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("SELECT 1"))
            result.scalar()
            return {"status": "healthy", "database": settings.database_name}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}
