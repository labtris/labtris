from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from labtris_api.config import settings

#: `pool_recycle=1800` is the belt-and-suspenders for connection leaks —
#: any pooled connection older than 30 min is discarded and reopened, so
#: a session that a background task quietly held past its useful life
#: does not sit in the pool forever. `pool_pre_ping=True` catches
#: connections whose Postgres side has already gone (network blip,
#: pg restart).
#:
#: pool_size and max_overflow used to be SQLAlchemy's defaults, 5 and 10,
#: with a comment claiming that needing more than 15 concurrent sessions
#: was "a leak worth finding, not a pool worth growing". Starting a
#: 3267-node fabric disproved it. A wave of nodes starting at once holds
#: one session each for as long as a container create takes, the interface
#: polls diagnostics the whole time, and the pool ran dry — after which
#: every request waited the full 30s pool_timeout and returned
#:
#:   TimeoutError: QueuePool limit of size 5 overflow 10 reached
#:
#: and nginx turned the backlog into 502s. Nothing was leaking; the pool
#: was simply smaller than the real workload. Both are settable now
#: (LABTRIS_DB_POOL_SIZE, LABTRIS_DB_MAX_OVERFLOW) because the right size
#: depends on how large a lab the host is asked to start.
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
