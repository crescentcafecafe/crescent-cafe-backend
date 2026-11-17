"""Background job handlers. All are idempotent: safe to run twice."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import now_utc
from app.models.core import AuthSession
from app.models.delivery import DeliveryOtp, RiderLocation
from app.models.notifications import NotificationDeliveryLog
from app.models.offers import CouponRedemption, Offer
from app.models.orders import Order
from app.models.payments import Payment, PaymentWebhookEvent, Refund
from app.models.system import Job
from app.services import events as ev
from app.services import invoices, order_state as st, payments, push
from app.services.jobs import job_handler
from app.services.settings import get_cafe_settings

log = logging.getLogger(__name__)


@job_handler("send_push")
async def _send_push(db: AsyncSession, p: dict) -> None:
    await push.deliver_event(db, int(p["event_id"]))


@job_handler("process_refund")
async def _process_refund(db: AsyncSession, p: dict) -> None:
    await payments.process_refund(db, uuid.UUID(p["refund_id"]))


@job_handler("generate_invoice")
async def _gen_invoice(db: AsyncSession, p: dict) -> None:
    order = await db.get(Order, uuid.UUID(p["order_id"]))
    if order:
        await invoices.ensure_invoice(db, order)


@job_handler("reconcile_payments")
async def _reconcile(db: AsyncSession, p: dict) -> None:
    now = now_utc()
    pend = (await db.execute(select(Payment).where(Payment.method == "online", Payment.status.in_(("created", "pending", "failed")),
                                                   Payment.gateway_order_id.is_not(None), Payment.created_at < now - timedelta(seconds=90),
                                                   Payment.created_at > now - timedelta(days=3)).limit(50))).scalars().all()
    for pay in pend:
        try:
            async with db.begin_nested():
                await payments.sync_payment(db, pay, "reconcile")
        except Exception:
            log.exception("reconcile failed for payment %s", pay.id)
    refunds = (await db.execute(select(Refund).where(Refund.method == "online", Refund.status == "pending", Refund.gateway_refund_id.is_not(None),
                                                     Refund.created_at < now - timedelta(minutes=2)).limit(50))).scalars().all()
    for r in refunds:
        try:
            async with db.begin_nested():
                await payments.process_refund(db, r.id)
        except Exception:
            log.exception("refund reconcile failed %s", r.id)


@job_handler("expire_unpaid_orders")
async def _expire_unpaid(db: AsyncSession, p: dict) -> None:
    cutoff = now_utc() - timedelta(minutes=get_settings().payment_timeout_minutes)
    rows = (await db.execute(select(Order).where(Order.status == "payment_pending", Order.created_at < cutoff).limit(50))).scalars().all()
    for o in rows:
        try:
            async with db.begin_nested():
                for pay in (await db.execute(select(Payment).where(Payment.order_id == o.id, Payment.method == "online"))).scalars().all():
                    if pay.gateway_order_id and pay.status in ("created", "pending", "failed"):
                        try:
                            await payments.sync_payment(db, pay, "expiry_check")  # last look: it may have been paid
                        except Exception:
                            log.warning("expiry sync failed for %s", pay.id)
                await db.refresh(o)
                if o.status == "payment_pending":
                    for pay in (await db.execute(select(Payment).where(Payment.order_id == o.id, Payment.status.in_(("created", "pending", "failed"))))).scalars().all():
                        pay.status = "expired"
                    o.payment_status = "expired"
                    await st.transition(db, o, "cancelled", actor_role="system", reason="Payment not completed in time")
        except Exception:
            log.exception("failed to expire order %s", o.id)


@job_handler("expire_coupon_reservations")
async def _expire_reservations(db: AsyncSession, p: dict) -> None:
    now = now_utc()
    await db.execute(update(CouponRedemption).where(CouponRedemption.status == "reserved", CouponRedemption.reserved_until < now)
                     .values(status="released", released_at=now))  # abandoned/failed checkouts never consume a coupon


@job_handler("retry_webhooks")
async def _retry_webhooks(db: AsyncSession, p: dict) -> None:
    from app.integrations import registry

    rows = (await db.execute(select(PaymentWebhookEvent).where(PaymentWebhookEvent.status == "failed").limit(20))).scalars().all()
    for row in rows:
        gw = registry.get_gateway(row.gateway)
        raw = json.dumps(row.payload).encode()
        parsed = gw.parse_webhook(raw, {"x-razorpay-event-id": row.event_id, "x-idempotency-key": row.event_id})
        try:
            async with db.begin_nested():
                await payments._process_webhook(db, row.gateway, parsed, row)
            row.status = row.status if row.status == "ignored" else "processed"
            row.processed_at, row.error = now_utc(), None
        except Exception as e:  # noqa: BLE001
            row.error = f"{type(e).__name__}: {e}"[:1000]
            row.status = "failed"


@job_handler("cleanup_otps")
async def _cleanup_otps(db: AsyncSession, p: dict) -> None:
    now = now_utc()
    await db.execute(update(DeliveryOtp).where(DeliveryOtp.verified_at.is_(None), DeliveryOtp.invalidated_at.is_(None), DeliveryOtp.expires_at < now)
                     .values(invalidated_at=now))
    await db.execute(delete(DeliveryOtp).where(DeliveryOtp.created_at < now - timedelta(days=30)))


@job_handler("cleanup_locations")
async def _cleanup_locations(db: AsyncSession, p: dict) -> None:
    from app.models.core import Branch

    for b in (await db.execute(select(Branch))).scalars().all():
        cs = await get_cafe_settings(db, b.id)
        await db.execute(delete(RiderLocation).where(RiderLocation.received_at < now_utc() - timedelta(days=cs.location_retention_days)))


@job_handler("cleanup_sessions")
async def _cleanup_sessions(db: AsyncSession, p: dict) -> None:
    cut = now_utc() - timedelta(days=30)
    await db.execute(delete(AuthSession).where((AuthSession.expires_at < cut) | (AuthSession.revoked_at < cut)))
    await db.execute(delete(NotificationDeliveryLog).where(NotificationDeliveryLog.created_at < now_utc() - timedelta(days=60)))


@job_handler("cleanup_jobs")
async def _cleanup_jobs(db: AsyncSession, p: dict) -> None:
    await db.execute(delete(Job).where(Job.status == "done", Job.finished_at < now_utc() - timedelta(days=7)))


@job_handler("expire_offers")
async def _expire_offers(db: AsyncSession, p: dict) -> None:
    await db.execute(update(Offer).where(Offer.status == "active", Offer.ends_at.is_not(None), Offer.ends_at < now_utc()).values(status="expired"))


@job_handler("remind_pending_orders")
async def _remind(db: AsyncSession, p: dict) -> None:
    """Server-side backup nudge while an order awaits a decision (max 10 reminders, 1/minute)."""
    now = now_utc()
    rows = (await db.execute(select(Order).where(Order.status.in_(("placed", "payment_verified")), Order.created_at < now - timedelta(minutes=1),
                                                  Order.created_at > now - timedelta(minutes=15)))).scalars().all()
    for o in rows:
        n = int((now - o.created_at).total_seconds() // 60)
        await ev.emit(db, type="order.reminder", topics=["admin"], dedupe_key=f"order.reminder:{o.id}:{n}", recipients=[{"role": "admin"}],
                      title="Order waiting", body=f"{o.order_number} still needs a decision", high_priority=True, data_only=True,
                      data={"order_id": o.id, "alarm": "start", "reminder": n}, branch_id=o.branch_id)
