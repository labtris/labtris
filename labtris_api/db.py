from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from labtris_api.config import settings

#: `pool_recycle=1800` is the belt-and-suspenders for connection leaks —
#: any pooled connection older than 30 min is discarded and reopened, so
#: a session that a background task quietly held past its useful life
#: does not sit in the pool forever. `pool_pre_ping=True` catches
#: connections whose Postgres side has already gone (network blip,
#: pg restart). The base pool_size (5) and overflow (10) stay at
#: SQLAlchemy defaults — a workload that ever needs more than 15
#: concurrent sessions is a leak worth finding, not a pool worth
#: growing.
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
