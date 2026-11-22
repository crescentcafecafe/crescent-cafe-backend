"""Durable business events -> WebSocket broadcast (post-commit) + FCM push job (in-transaction)."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import now_utc
from app.db.session import add_post_commit
from app.models.notifications import NotificationEvent
from app.services import jobs
from app.websockets.manager import broker


def jsonable(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: jsonable(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [jsonable(v) for v in d]
    if isinstance(d, uuid.UUID):
        return str(d)
    if hasattr(d, "isoformat"):
        return d.isoformat()
    return d


async def emit(db: AsyncSession, *, type: str, topics: list[str], dedupe_key: str, data: dict | None = None,
               recipients: list[dict] | None = None, title: str | None = None, body: str | None = None,
               high_priority: bool = False, data_only: bool = False, push: bool = True,
               branch_id: uuid.UUID | None = None) -> NotificationEvent | None:
    """Persist an event (idempotent by dedupe_key). Returns None if this event already existed."""
    data = jsonable(data or {})
    recipients = jsonable(recipients or [])
    ev = NotificationEvent(type=type, dedupe_key=dedupe_key, recipients=recipients, title=title, body=body, data=data,
                           high_priority=high_priority, data_only=data_only, push=bool(push and recipients), branch_id=branch_id)
    try:
        async with db.begin_nested():
            db.add(ev)
            await db.flush()
    except IntegrityError:
        return None
    if ev.push:
        await jobs.enqueue(db, "send_push", {"event_id": ev.id}, key=f"push:{ev.id}")
    envelope = {"id": ev.id, "type": type, "ts": (ev.created_at or now_utc()).isoformat(), "data": data}

    async def _broadcast() -> None:
        for t in topics:
            await broker.publish(t, {**envelope, "topic": t})

    add_post_commit(db, _broadcast)
    return ev


def publish_ephemeral(db: AsyncSession, *, type: str, topics: list[str], data: dict) -> None:
    """Realtime-only event (not stored) e.g. rider location ticks, typing indicators."""
    envelope = {"id": None, "type": type, "ts": now_utc().isoformat(), "data": jsonable(data)}

    async def _b() -> None:
        for t in topics:
            await broker.publish(t, {**envelope, "topic": t})

    add_post_commit(db, _b)
