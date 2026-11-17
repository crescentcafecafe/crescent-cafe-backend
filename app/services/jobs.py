"""Postgres-backed job queue: enqueue helper + worker loop with exponential backoff."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta
from typing import Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import now_utc
from app.db.session import session_scope
from app.models.system import Job

log = logging.getLogger(__name__)
Handler = Callable[[AsyncSession, dict], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}


class PermanentFailure(Exception):
    """Raise from a handler to fail a job without retrying."""


def job_handler(kind: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


async def enqueue(db: AsyncSession, kind: str, payload: dict | None = None, *, delay_seconds: float = 0,
                  key: str | None = None) -> Job | None:
    """Enqueue inside the caller's transaction (so a job exists iff the business change commits)."""
    job = Job(kind=kind, payload=payload or {}, idempotency_key=key, max_attempts=get_settings().job_max_attempts,
              run_at=now_utc() + timedelta(seconds=delay_seconds))
    try:
        async with db.begin_nested():
            db.add(job)
            await db.flush()
    except IntegrityError:
        return None  # duplicate idempotency key
    return job


async def _claim(limit: int = 10) -> list[tuple[str, str, dict, int, int]]:
    async with session_scope() as db:
        rows = (await db.execute(
            select(Job).where(Job.status == "queued", Job.run_at <= now_utc()).order_by(Job.run_at).limit(limit)
            .with_for_update(skip_locked=True))).scalars().all()
        out = []
        for j in rows:
            j.status, j.locked_at = "running", now_utc()
            j.attempts += 1
            out.append((str(j.id), j.kind, j.payload, j.attempts, j.max_attempts))
        return out


async def _finish(job_id: str, error: str | None, attempts: int, max_attempts: int, permanent: bool = False) -> None:
    import uuid

    async with session_scope() as db:
        j = await db.get(Job, uuid.UUID(job_id))
        if j is None:
            return
        if error is None:
            j.status, j.finished_at, j.last_error = "done", now_utc(), None
        elif permanent or attempts >= max_attempts:
            j.status, j.finished_at, j.last_error = "failed", now_utc(), error[:2000]
        else:
            backoff = min(5 * (2 ** (attempts - 1)), 3600) * (0.8 + random.random() * 0.4)
            j.status, j.run_at, j.last_error = "queued", now_utc() + timedelta(seconds=backoff), error[:2000]


async def run_once(limit: int = 10) -> int:
    claimed = await _claim(limit)
    for job_id, kind, payload, attempts, max_attempts in claimed:
        handler = HANDLERS.get(kind)
        if handler is None:
            await _finish(job_id, f"no handler for {kind}", attempts, max_attempts, permanent=True)
            continue
        try:
            async with session_scope() as db:
                await handler(db, payload)
            await _finish(job_id, None, attempts, max_attempts)
        except PermanentFailure as e:
            log.error("job %s %s failed permanently: %s", kind, job_id, e)
            await _finish(job_id, str(e), attempts, max_attempts, permanent=True)
        except Exception as e:  # noqa: BLE001
            log.exception("job %s %s failed (attempt %s/%s)", kind, job_id, attempts, max_attempts)
            await _finish(job_id, f"{type(e).__name__}: {e}", attempts, max_attempts)
    return len(claimed)


async def requeue_stuck(minutes: int = 10) -> int:
    async with session_scope() as db:
        res = await db.execute(update(Job).where(Job.status == "running", Job.locked_at < now_utc() - timedelta(minutes=minutes))
                               .values(status="queued", run_at=now_utc()))
        return res.rowcount or 0


PERIODIC: list[tuple[str, int]] = [  # (job kind, every N seconds)
    ("reconcile_payments", 60), ("expire_unpaid_orders", 60), ("expire_coupon_reservations", 60),
    ("retry_webhooks", 120), ("cleanup_otps", 600), ("cleanup_locations", 3600),
    ("cleanup_sessions", 3600), ("cleanup_jobs", 86400), ("expire_offers", 300), ("remind_pending_orders", 60),
]


async def schedule_periodic() -> None:
    now = int(now_utc().timestamp())
    async with session_scope() as db:
        for kind, every in PERIODIC:
            await enqueue(db, kind, {}, key=f"periodic:{kind}:{now // every}")


async def worker_loop(stop: asyncio.Event | None = None) -> None:
    import app.workers.handlers  # noqa: F401  (registers handlers)

    s = get_settings()
    last_sched = 0.0
    log.info("worker started")
    while stop is None or not stop.is_set():
        try:
            loop_now = asyncio.get_event_loop().time()
            if loop_now - last_sched > 30:
                await schedule_periodic()
                await requeue_stuck()
                last_sched = loop_now
            n = await run_once()
            if n == 0:
                await asyncio.sleep(s.job_poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worker loop error")
            await asyncio.sleep(5)
