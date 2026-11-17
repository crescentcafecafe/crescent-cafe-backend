"""Sliding-window rate limiting. In-memory by default, Redis when REDIS_URL is set (shared across instances)."""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request

from app.core.config import get_settings
from app.core.errors import TooMany

_hits: dict[str, deque] = defaultdict(deque)
_redis = None


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _get_redis():
    global _redis
    s = get_settings()
    if not s.redis_url:
        return None
    if _redis is None:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(s.redis_url, decode_responses=True)
    return _redis


async def check(key: str, limit: int, window: int) -> None:
    if not get_settings().rate_limit_enabled:
        return
    r = await _get_redis()
    if r is not None:
        try:
            k = f"rl:{key}:{int(time.time() // window)}"
            n = await r.incr(k)
            if n == 1:
                await r.expire(k, window + 1)
            if n > limit:
                raise TooMany("Too many requests, please slow down", details={"retry_after": window})
            return
        except TooMany:
            raise
        except Exception:
            pass  # fall back to local limiter if redis is down
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise TooMany("Too many requests, please slow down", details={"retry_after": window})
    q.append(now)


def reset_local() -> None:
    _hits.clear()


def limit(name: str, max_requests: int, window_seconds: int):
    """FastAPI dependency factory limiting per client IP."""

    async def dep(request: Request) -> None:
        await check(f"{name}:{client_ip(request)}", max_requests, window_seconds)

    return dep


async def limit_key(name: str, key: str, max_requests: int, window_seconds: int) -> None:
    await check(f"{name}:{key}", max_requests, window_seconds)
