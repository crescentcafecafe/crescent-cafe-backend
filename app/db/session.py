from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

log = logging.getLogger(__name__)

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine, _sessionmaker
    if _engine is None:
        s = get_settings()
        kwargs: dict = {"pool_pre_ping": True}
        connect_args: dict = {}
        if s.database_url.startswith("postgresql+asyncpg"):
            kwargs.update(pool_size=s.db_pool_size, max_overflow=s.db_max_overflow)
            if s.db_use_pgbouncer:
                connect_args["statement_cache_size"] = 0  # Supabase pooler (pgbouncer) friendly
                connect_args["prepared_statement_cache_size"] = 0
            if s.db_ssl_require:
                connect_args["ssl"] = "require"
        if connect_args:
            kwargs["connect_args"] = connect_args
        _engine = create_async_engine(s.database_url, **kwargs)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


PostCommit = Callable[[], Awaitable[None]]


def add_post_commit(session: AsyncSession, cb: PostCommit) -> None:
    """Register a coroutine to run only after the surrounding transaction commits (e.g. WebSocket broadcasts)."""
    session.info.setdefault("post_commit", []).append(cb)


async def run_post_commit(session: AsyncSession) -> None:
    for cb in session.info.pop("post_commit", []):
        try:
            await cb()
        except Exception:  # never fail a committed request because a broadcast failed
            log.exception("post-commit callback failed")


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for workers/scripts."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    await run_post_commit(session)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transaction per request, committed on success."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    await run_post_commit(session)
