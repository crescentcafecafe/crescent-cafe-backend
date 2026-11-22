"""Notification centre: the durable events addressed to one person, with per-person read / cleared state.

Events are shared rows (NotificationEvent); who may see one is decided by its `recipients`. A visible event is one that has text
and is not an internal signal (alarm stop, reminder, menu sync). Only the last RETENTION_DAYS days and at most SCAN_LIMIT events are looked at.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import now_utc
from app.models.core import Customer
from app.models.notifications import NotificationEvent, NotificationReceipt

RETENTION_DAYS = 30
SCAN_LIMIT = 2000
CATEGORIES = ("orders", "payments", "delivery", "messages", "system")

# Internal signals: never shown in the centre.
HIDDEN_TYPES = {"order.alarm_stop", "order.alarm_ack", "order.reminder", "assignment.alarm_stop", "assignment.reminder",
                "delivery.otp_issued", "menu.availability_changed", "delivery.reminder_internal"}


def category_of(event_type: str | None) -> str:
    t = event_type or ""
    if t.startswith(("payment.", "refund.")):
        return "payments"
    if t.startswith(("assignment.", "delivery.", "cash.")):
        return "delivery"
    if t == "chat.message":
        return "messages"
    if t.startswith("order."):
        return "orders"
    return "system"   # promotions, announcements, account and system alerts


def is_mine(ev: NotificationEvent, role: str, user_id: uuid.UUID, marketing_opt_in: bool) -> bool:
    for r in ev.recipients or []:
        rr = r.get("role")
        if rr == role and r.get("id") in (None, str(user_id)):
            return True
        if rr == "all_customers_opt_in" and role == "customer" and marketing_opt_in:
            return True
    return False


def _visible(ev: NotificationEvent) -> bool:
    return ev.type not in HIDDEN_TYPES and bool(ev.title or ev.body)


async def _opt_in(db: AsyncSession, role: str, user_id: uuid.UUID) -> bool:
    if role != "customer":
        return False
    c = await db.get(Customer, user_id)
    return bool(c and c.marketing_opt_in)


async def _scan(db: AsyncSession, role: str, user_id: uuid.UUID, *, before_id: int | None, want: int, limit_scan: int = SCAN_LIMIT) -> list[NotificationEvent]:
    """Newest-first visible events for this person, collected in pages until `want` are found or the scan limit is reached."""
    opt = await _opt_in(db, role, user_id)
    since = now_utc() - timedelta(days=RETENTION_DAYS)
    cursor, scanned, out = before_id, 0, []
    while scanned < limit_scan and len(out) < want:
        q = select(NotificationEvent).where(NotificationEvent.created_at > since).order_by(NotificationEvent.id.desc()).limit(500)
        if cursor is not None:
            q = q.where(NotificationEvent.id < cursor)
        rows = (await db.execute(q)).scalars().all()
        if not rows:
            break
        for e in rows:
            if _visible(e) and is_mine(e, role, user_id, opt):
                out.append(e)
        scanned += len(rows)
        cursor = rows[-1].id
    return out


async def _receipts(db: AsyncSession, role: str, user_id: uuid.UUID, ids: list[int]) -> dict[int, NotificationReceipt]:
    if not ids:
        return {}
    rows = (await db.execute(select(NotificationReceipt).where(NotificationReceipt.owner_role == role, NotificationReceipt.owner_id == user_id,
                                                              NotificationReceipt.event_id.in_(ids)))).scalars().all()
    return {r.event_id: r for r in rows}


def _item(e: NotificationEvent, r: NotificationReceipt | None) -> dict:
    d = e.data or {}
    keep = {k: d[k] for k in ("order_id", "order_number", "conversation_id", "assignment_id", "status") if k in d}
    return {"id": e.id, "type": e.type, "category": category_of(e.type), "title": e.title, "body": e.body, "created_at": e.created_at,
            "read": bool(r and r.read_at), "data": keep}


async def list_items(db: AsyncSession, role: str, user_id: uuid.UUID, *, category: str = "all", before_id: int | None = None, limit: int = 30) -> dict:
    limit = max(1, min(limit, 100))
    items: list[dict] = []
    cursor = before_id
    more = False
    while len(items) < limit:
        batch = await _scan(db, role, user_id, before_id=cursor, want=limit * 2)
        if not batch:
            break
        rec = await _receipts(db, role, user_id, [e.id for e in batch])
        for e in batch:
            r = rec.get(e.id)
            if r and r.cleared_at:
                continue
            it = _item(e, r)
            if category == "unread" and it["read"]:
                continue
            if category in CATEGORIES and it["category"] != category:
                continue
            items.append(it)
        cursor = batch[-1].id
        if len(batch) < limit * 2:
            break
    if len(items) > limit:
        more = True
    items = items[:limit]
    return {"items": items, "next_before_id": items[-1]["id"] if (items and more) else None}


async def unread_counts(db: AsyncSession, role: str, user_id: uuid.UUID) -> dict:
    batch = await _scan(db, role, user_id, before_id=None, want=200, limit_scan=600)
    rec = await _receipts(db, role, user_id, [e.id for e in batch])
    by: dict[str, int] = {c: 0 for c in CATEGORIES}
    total = 0
    for e in batch:
        r = rec.get(e.id)
        if r and (r.read_at or r.cleared_at):
            continue
        by[category_of(e.type)] += 1
        total += 1
    return {"unread": total, "by_category": by}


async def _mark(db: AsyncSession, role: str, user_id: uuid.UUID, ids: list[int], *, read: bool, clear: bool) -> int:
    ids = list({int(i) for i in ids})[:500]
    if not ids:
        return 0
    opt = await _opt_in(db, role, user_id)
    events = (await db.execute(select(NotificationEvent).where(NotificationEvent.id.in_(ids)))).scalars().all()
    mine = [e for e in events if is_mine(e, role, user_id, opt)]   # nobody can mark someone else's notification
    rec = await _receipts(db, role, user_id, [e.id for e in mine])
    now = now_utc()
    for e in mine:
        r = rec.get(e.id)
        if r is None:
            r = NotificationReceipt(event_id=e.id, owner_role=role, owner_id=user_id)
            db.add(r)
        if read and not r.read_at:
            r.read_at = now
        if clear:
            r.cleared_at = r.cleared_at or now
            r.read_at = r.read_at or now
    await db.flush()
    return len(mine)


async def mark_read(db: AsyncSession, role: str, user_id: uuid.UUID, ids: list[int]) -> int:
    return await _mark(db, role, user_id, ids, read=True, clear=False)


async def clear(db: AsyncSession, role: str, user_id: uuid.UUID, ids: list[int]) -> int:
    return await _mark(db, role, user_id, ids, read=False, clear=True)


async def _all_ids(db: AsyncSession, role: str, user_id: uuid.UUID) -> list[int]:
    return [e.id for e in (await _scan(db, role, user_id, before_id=None, want=500))]


async def mark_all_read(db: AsyncSession, role: str, user_id: uuid.UUID) -> int:
    return await _mark(db, role, user_id, await _all_ids(db, role, user_id), read=True, clear=False)


async def clear_all(db: AsyncSession, role: str, user_id: uuid.UUID) -> int:
    return await _mark(db, role, user_id, await _all_ids(db, role, user_id), read=False, clear=True)
