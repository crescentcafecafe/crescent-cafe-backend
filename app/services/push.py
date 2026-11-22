"""FCM delivery: durable events via the job queue, plus fire-and-forget direct pushes for sensitive one-off messages (OTP)."""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import session_scope
from app.integrations import registry
from app.models.core import AdminUser, Customer, Device, FcmToken
from app.models.notifications import NotificationDeliveryLog, NotificationEvent
from app.services.jobs import PermanentFailure

log = logging.getLogger(__name__)

# Alarm events are sent to every valid phone of the account, even if the phone's saved notification flag is stale.
ALARM_TYPES = {"order.new", "order.reminder", "order.alarm_stop", "assignment.offered", "assignment.reminder", "assignment.alarm_stop", "assignment.cancelled"}


def android_channel_for(event_type: str | None, s) -> str:
    """Android notification channel for an event. new_order/assignment.offered/order.reminder keep their original channel;
    the rest map to the channels the native app creates. Unknown types fall back to the default channel."""
    t = event_type or ""
    if t in ("order.new", "assignment.offered", "order.reminder", "assignment.reminder"):
        return s.fcm_android_channel_new_order
    if t == "chat.message":
        return s.fcm_android_channel_messages
    if t == "promotion":
        return s.fcm_android_channel_promotions
    if t.startswith(("payment.", "refund.")):
        return s.fcm_android_channel_payments
    if t.startswith(("assignment.", "delivery.", "cash.")):
        return s.fcm_android_channel_delivery
    if t.startswith("order."):
        return s.fcm_android_channel_order_updates
    return s.fcm_android_channel_default


async def tokens_for(db: AsyncSession, recipients: list[dict], ignore_flag: bool = False) -> list[FcmToken]:
    out: dict[uuid.UUID, FcmToken] = {}
    for r in recipients:
        role, rid = r.get("role"), r.get("id")
        q = select(FcmToken).join(Device, Device.id == FcmToken.device_id).where(FcmToken.is_valid.is_(True), Device.is_active.is_(True))
        if not ignore_flag:
            q = q.where(Device.notifications_enabled.is_(True))
        if role == "admin":
            ids = [a for (a,) in (await db.execute(select(AdminUser.id).where(AdminUser.is_active.is_(True)))).all()]
            q = q.where(Device.owner_role == "admin", Device.owner_id.in_(ids))
        elif role == "all_customers_opt_in":
            ids = [c for (c,) in (await db.execute(select(Customer.id).where(Customer.marketing_opt_in.is_(True), Customer.is_active.is_(True)))).all()]
            q = q.where(Device.owner_role == "customer", Device.owner_id.in_(ids))
        elif rid:
            q = q.where(Device.owner_role == role, Device.owner_id == uuid.UUID(str(rid)))
        else:
            continue
        for t in (await db.execute(q)).scalars().all():
            out[t.id] = t
    return list(out.values())


async def deliver_event(db: AsyncSession, event_id: int) -> None:
    """Idempotent: tokens that already have a 'sent' log for this event are skipped, so retries never double-notify."""
    ev = await db.get(NotificationEvent, event_id)
    if ev is None or not ev.push:
        return
    toks = await tokens_for(db, ev.recipients, ignore_flag=(ev.type in ALARM_TYPES))
    sent = {t for (t,) in (await db.execute(select(NotificationDeliveryLog.fcm_token_id).where(NotificationDeliveryLog.event_id == ev.id,
                                                                                         NotificationDeliveryLog.status == "sent"))).all()}
    toks = [t for t in toks if t.id not in sent]
    if not toks:
        return
    attempt = (await db.execute(select(func.count()).select_from(NotificationDeliveryLog).where(NotificationDeliveryLog.event_id == ev.id))).scalar_one() // max(len(toks), 1) + 1
    s = get_settings()
    channel = android_channel_for(ev.type, s)
    data = {**(ev.data or {}), "type": ev.type, "event_id": ev.id}
    if ev.data_only:  # no system notification is attached, so the app needs the text to build its own
        data.setdefault("title", ev.title or "")
        data.setdefault("body", ev.body or "")
    results = await registry.get_push_client().send([t.token for t in toks], title=ev.title, body=ev.body, data=data,
                                                    high_priority=ev.high_priority, data_only=ev.data_only, channel_id=channel)
    transient = 0
    for tok, res in zip(toks, results):
        status = "sent" if res.ok else ("invalid_token" if res.invalid_token else "failed")
        db.add(NotificationDeliveryLog(event_id=ev.id, fcm_token_id=tok.id, status=status, fcm_message_id=res.message_id, error=res.error, attempt=attempt))
        if res.invalid_token:
            tok.is_valid, tok.last_error = False, (res.error or "")[:500]  # invalid token cleanup
        elif not res.ok and not res.permanent:
            transient += 1
    if transient:
        raise RuntimeError(f"{transient} FCM deliveries failed transiently")  # job retried with backoff
    if results and all((not r.ok) and r.permanent and not r.invalid_token for r in results):
        raise PermanentFailure(results[0].error or "push failed permanently")


async def send_direct(role: str, user_id: uuid.UUID, *, title: str, body: str, data: dict, high_priority: bool = False) -> None:
    """Best-effort push that is NOT stored (used for the delivery OTP so plaintext never lands in the database)."""
    try:
        async with session_scope() as db:
            toks = await tokens_for(db, [{"role": role, "id": str(user_id)}])
            if not toks:
                return
            results = await registry.get_push_client().send([t.token for t in toks], title=title, body=body, data=data, high_priority=high_priority,
                                                            data_only=False, channel_id=get_settings().fcm_android_channel_default)
            for tok, res in zip(toks, results):
                if res.invalid_token:
                    tok.is_valid = False
    except Exception:
        log.exception("direct push failed")
