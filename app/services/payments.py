"""Payments: Razorpay / Cashfree behind one interface, cash at counter, webhooks, reconciliation and refunds.
Rule: only server-verified gateway data (signed webhook or server-to-server fetch) can mark a payment as paid."""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, Conflict, Forbidden, Invalid, NotFound, Unauthorized
from app.core.security import now_utc
from app.integrations import registry
from app.integrations.base import GatewayError, GatewayPaymentInfo, RefundInfo
from app.models.orders import Order
from app.models.payments import Payment, PaymentWebhookEvent, Refund, RiderCashLedger
from app.services import events as ev
from app.services import jobs, offers as offer_svc, order_state as st, settings as cfg
from app.services.audit import audit

log = logging.getLogger(__name__)
PAID = ("paid", "partially_refunded", "refunded")


# ------------------------------------------------------------------ initiate
async def initiate_online(db: AsyncSession, order: Order, gateway_name: str) -> dict:
    if order.status != "payment_pending":
        raise Conflict("This order is not awaiting payment", code="not_payable", details={"status": order.status})
    cs = await cfg.get_cafe_settings(db, order.branch_id)
    if gateway_name not in cfg.available_gateways(cs):
        raise Invalid(f"{gateway_name} is not available", code="gateway_unavailable", details={"available": cfg.available_gateways(cs)})
    now = now_utc()
    existing = (await db.execute(select(Payment).where(Payment.order_id == order.id, Payment.gateway == gateway_name,
                                                       Payment.status.in_(("created", "pending", "failed")),
                                                       Payment.gateway_order_id.is_not(None)).order_by(Payment.created_at.desc()).limit(1))).scalar_one_or_none()
    if existing and (existing.expires_at is None or existing.expires_at > now) and (existing.last_gateway_payload or {}).get("client_payload"):
        order.payment_method = gateway_name
        return _init_response(existing)
    pay = Payment(order_id=order.id, branch_id=order.branch_id, method="online", gateway=gateway_name, amount_paise=order.total_paise,
                  currency=order.currency, status="created", expires_at=now + timedelta(minutes=get_settings().payment_timeout_minutes))
    db.add(pay)
    await db.flush()
    ref = pay.id.hex if gateway_name == "razorpay" else f"cf{pay.id.hex}"
    gw = registry.get_gateway(gateway_name)
    try:
        go = await gw.create_order(reference=ref, amount_paise=order.total_paise, currency=order.currency,
                                   customer={"id": str(order.customer_id) if order.customer_id else None, "order_id": str(order.id),
                                             "name": order.customer_name, "phone": order.customer_phone, "email": order.customer_email},
                                   description=f"Crescent Cafe order {order.order_number}")
    except GatewayError as e:
        log.warning("gateway order creation failed: %s", e)
        raise AppError("Payment provider is unavailable right now. Please try again or choose another method.",
                       code="payment_gateway_unavailable", status_code=502)
    pay.gateway_order_id, pay.gateway_session_id, pay.status = go.gateway_order_id, go.session_id, "pending"
    pay.last_gateway_payload = {"client_payload": go.client_payload}
    order.payment_method, order.payment_status = gateway_name, "pending"
    return _init_response(pay)


def _init_response(p: Payment) -> dict:
    return {"payment_id": p.id, "gateway": p.gateway, "amount_paise": p.amount_paise, "currency": p.currency,
            "client_payload": (p.last_gateway_payload or {}).get("client_payload"), "expires_at": p.expires_at}


# ------------------------------------------------------------------ apply gateway result (idempotent)
async def _lock_payment(db: AsyncSession, payment_id: uuid.UUID) -> Payment:
    return (await db.execute(select(Payment).where(Payment.id == payment_id).with_for_update().execution_options(populate_existing=True))).scalar_one()


async def apply_result(db: AsyncSession, payment_id: uuid.UUID, info: GatewayPaymentInfo, source: str) -> Payment:
    pay = await _lock_payment(db, payment_id)
    if info.raw:
        pay.last_gateway_payload = {**(pay.last_gateway_payload or {}), "last": info.raw}
    if info.status == "paid":
        if pay.status in PAID:
            return pay  # duplicate confirmation: no-op
        if (info.amount_paise is not None and info.amount_paise != pay.amount_paise) or (info.currency and info.currency != pay.currency):
            pay.verification_status = "mismatch"
            log.error("payment amount/currency mismatch payment=%s expected=%s/%s got=%s/%s", pay.id, pay.amount_paise, pay.currency, info.amount_paise, info.currency)
            await ev.emit(db, type="payment.mismatch", topics=["admin"], dedupe_key=f"payment.mismatch:{pay.id}", recipients=[{"role": "admin"}],
                          title="Payment amount mismatch", body="A gateway payment did not match the order total. Check manually.", data={"payment_id": pay.id, "order_id": pay.order_id},
                          high_priority=True, branch_id=pay.branch_id)
            await audit(db, actor_role="gateway", actor_id=None, action="payment.mismatch", entity_type="payment", entity_id=pay.id,
                        after={"expected": pay.amount_paise, "got": info.amount_paise, "currency": info.currency}, branch_id=pay.branch_id)
            return pay
        now = now_utc()
        pay.status, pay.verification_status, pay.paid_at, pay.verified_at = "paid", "verified", now, now
        pay.gateway_payment_id = info.gateway_payment_id or pay.gateway_payment_id
        order = await st.lock_order(db, pay.order_id)
        other_paid = (await db.execute(select(func.count()).select_from(Payment).where(Payment.order_id == order.id, Payment.id != pay.id,
                                                                                       Payment.status.in_(PAID)))).scalar_one() > 0
        if other_paid:  # customer paid twice (e.g. via both gateways): refund the duplicate automatically
            await create_refund(db, pay, pay.amount_paise, "Duplicate payment", None, f"dup:{pay.id}", actor_role="system")
        elif order.status in ("cancelled", "rejected"):  # late payment after the order was closed
            order.payment_status = "paid"
            await ev.emit(db, type="payment.late", topics=["admin"], dedupe_key=f"payment.late:{pay.id}", recipients=[{"role": "admin"}],
                          title="Late payment received", body=f"Order {order.order_number} was closed but payment arrived. Refund started.",
                          data={"order_id": order.id}, branch_id=order.branch_id)
            await on_order_closed_unfulfilled(db, order, "system", None)
        else:
            order.payment_status = "paid"
            if pay.collection_channel == "rider_upi_qr":
                pay.collected_at = now
                order.payment_method = pay.gateway or order.payment_method
                for cash in (await db.execute(select(Payment).where(Payment.order_id == order.id, Payment.method == "cash",
                                                                    Payment.status.in_(("created", "pending"))))).scalars().all():
                    cash.status = "cancelled"  # customer chose to pay by QR instead of cash
            if order.status == "payment_pending":
                await st.transition(db, order, "payment_verified", actor_role="gateway", meta={"payment_id": str(pay.id), "source": source},
                                    notify_customer=False)
                await offer_svc.consume_for_order(db, order.id)
                if order.order_type == "walk_in":
                    await st.transition(db, order, "accepted", actor_role="system", reason="auto-accepted walk-in", notify_customer=False)
                else:
                    cs = await cfg.get_cafe_settings(db, order.branch_id)
                    await st.emit_new_order_alarm(db, order, notify=cs.notify_admin_new_orders)
        await ev.emit(db, type="payment.succeeded", topics=st.order_topics(order), dedupe_key=f"payment.ok:{pay.id}",
                      recipients=st.customer_recipients(order), title="Payment received", body=f"Payment for order {order.order_number} was successful.",
                      data={"order_id": order.id, "payment_id": pay.id, "amount_paise": pay.amount_paise, "payment_status": "paid"}, branch_id=order.branch_id)
        return pay
    if pay.status in PAID:
        return pay  # a stale 'failed'/'pending' report can never downgrade a paid payment
    if info.status == "failed":
        pay.status, pay.failure_reason = "failed", (info.failure_reason or "Payment failed")[:300]
        order = await db.get(Order, pay.order_id)
        if order.status == "payment_pending":
            order.payment_status = "failed"
        await ev.emit(db, type="payment.failed", topics=st.order_topics(order, admin=False), dedupe_key=f"payment.fail:{pay.id}:{hashlib.md5((info.gateway_payment_id or '').encode()).hexdigest()[:8]}",
                      recipients=st.customer_recipients(order), title="Payment failed", body=f"Payment for order {order.order_number} failed. You can try again.",
                      data={"order_id": order.id, "payment_id": pay.id, "payment_status": "failed", "reason": pay.failure_reason}, branch_id=order.branch_id)
    elif info.status == "expired":
        pay.status = "expired"
    else:
        pay.status = "pending"
    return pay


async def sync_payment(db: AsyncSession, payment: Payment, source: str = "sync") -> Payment:
    if payment.method != "online" or not payment.gateway_order_id:
        return payment
    gw = registry.get_gateway(payment.gateway)
    try:
        info = await gw.fetch_payment_status(payment.gateway_order_id)
    except GatewayError as e:
        raise AppError(f"Could not reach the payment provider: {e}", code="payment_gateway_unavailable", status_code=502)
    return await apply_result(db, payment.id, info, source)


async def verify_razorpay_callback(db: AsyncSession, order: Order, *, razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str) -> Payment:
    """Client callback is only a hint: we verify the signature AND re-fetch the status server-side before trusting anything."""
    pay = (await db.execute(select(Payment).where(Payment.gateway == "razorpay", Payment.gateway_order_id == razorpay_order_id,
                                                  Payment.order_id == order.id))).scalar_one_or_none()
    if pay is None:
        raise NotFound("Payment not found", code="payment_not_found")
    gw = registry.get_gateway("razorpay")
    if not gw.verify_checkout_signature(razorpay_order_id=razorpay_order_id, razorpay_payment_id=razorpay_payment_id, razorpay_signature=razorpay_signature):
        pay.verification_status = "mismatch"
        await db.commit()
        raise Unauthorized("Payment signature verification failed", code="invalid_signature")
    return await sync_payment(db, pay, "client_callback")


# ------------------------------------------------------------------ webhooks
async def handle_webhook(db: AsyncSession, gateway_name: str, raw: bytes, headers: dict[str, str]) -> dict:
    gw = registry.get_gateway(gateway_name)
    if not gw.verify_webhook_signature(raw, headers):
        log.warning("invalid %s webhook signature", gateway_name)
        raise Unauthorized("Invalid webhook signature", code="invalid_signature")
    try:
        parsed = gw.parse_webhook(raw, headers)
    except Exception:
        raise Invalid("Malformed webhook payload", code="bad_webhook")
    import json

    row = PaymentWebhookEvent(gateway=gateway_name, event_id=parsed.event_id, event_type=parsed.event_type, signature_valid=True,
                              payload=json.loads(raw))
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        existing = (await db.execute(select(PaymentWebhookEvent).where(PaymentWebhookEvent.gateway == gateway_name,
                                                                      PaymentWebhookEvent.event_id == parsed.event_id))).scalar_one()
        if existing.status in ("processed", "ignored"):
            return {"status": "duplicate"}
        row = existing  # previous attempt failed: retry processing
    try:
        async with db.begin_nested():
            await _process_webhook(db, gateway_name, parsed, row)
        row.status, row.processed_at, row.error = row.status if row.status == "ignored" else "processed", now_utc(), None
    except Exception as e:  # noqa: BLE001
        log.exception("webhook processing failed")
        row.status, row.error = "failed", f"{type(e).__name__}: {e}"[:1000]
        await jobs.enqueue(db, "retry_webhooks", {}, delay_seconds=30, key=f"retry_webhooks:{row.id}")
    return {"status": row.status}


async def _process_webhook(db: AsyncSession, gateway_name: str, parsed, row: PaymentWebhookEvent) -> None:
    if parsed.payment is not None and parsed.gateway_order_id:
        pay = (await db.execute(select(Payment).where(Payment.gateway == gateway_name, Payment.gateway_order_id == parsed.gateway_order_id))).scalar_one_or_none()
        if pay is None:
            row.status = "ignored"
            return
        row.payment_id = pay.id
        await apply_result(db, pay.id, parsed.payment, "webhook")
    if parsed.refund is not None:
        q = select(Refund)
        refund = None
        if parsed.refund_ref:
            try:
                refund = await db.get(Refund, uuid.UUID(parsed.refund_ref))
            except ValueError:
                refund = None
        if refund is None and parsed.refund.gateway_refund_id:
            refund = (await db.execute(q.where(Refund.gateway_refund_id == parsed.refund.gateway_refund_id))).scalar_one_or_none()
        if refund is None:
            row.status = "ignored"
            return
        await apply_refund_info(db, refund.id, parsed.refund)
    if parsed.payment is None and parsed.refund is None:
        row.status = "ignored"


# ------------------------------------------------------------------ cash at counter
async def confirm_cash(db: AsyncSession, payment_id: uuid.UUID, admin_id: uuid.UUID, tendered_paise: int) -> Payment:
    pay = await _lock_payment(db, payment_id)
    if pay.method != "cash":
        raise Invalid("Only cash payments can be confirmed manually", code="not_cash")
    if pay.status in PAID:
        raise Conflict("This cash payment was already confirmed", code="already_confirmed")
    order = await st.lock_order(db, pay.order_id)
    if order.status in ("cancelled", "rejected", "refund_pending", "refunded"):
        raise Conflict("This order is closed", code="order_closed")
    if tendered_paise < pay.amount_paise:
        raise Invalid(f"Amount received is less than the payable ₹{pay.amount_paise / 100:.2f}", code="insufficient_amount")
    now = now_utc()
    pay.status, pay.verification_status, pay.paid_at, pay.verified_at = "paid", "verified", now, now
    pay.confirmed_by_admin_id, pay.tendered_paise, pay.change_paise = admin_id, tendered_paise, tendered_paise - pay.amount_paise
    order.payment_status = "paid"
    await audit(db, actor_role="admin", actor_id=admin_id, action="payment.cash_confirmed", entity_type="payment", entity_id=pay.id,
                after={"order_id": str(order.id), "amount_paise": pay.amount_paise, "tendered_paise": tendered_paise}, branch_id=pay.branch_id)
    await ev.emit(db, type="payment.succeeded", topics=st.order_topics(order), dedupe_key=f"payment.ok:{pay.id}", recipients=st.customer_recipients(order),
                  title="Payment received", body=f"Cash payment for order {order.order_number} received. Thank you!",
                  data={"order_id": order.id, "payment_id": pay.id, "payment_status": "paid"}, branch_id=order.branch_id)
    return pay


async def reverse_cash(db: AsyncSession, payment_id: uuid.UUID, admin_id: uuid.UUID, reason: str) -> Payment:
    if not reason or not reason.strip():
        raise Invalid("A reason is required", code="reason_required")
    pay = await _lock_payment(db, payment_id)
    if pay.method != "cash" or pay.status != "paid" or pay.refunded_paise:
        raise Conflict("Only a confirmed, un-refunded cash payment can be reversed", code="not_reversible")
    before = {"status": pay.status, "tendered_paise": pay.tendered_paise, "paid_at": str(pay.paid_at), "confirmed_by": str(pay.confirmed_by_admin_id)}
    order = await st.lock_order(db, pay.order_id)
    if pay.collected_by_rider_id and pay.collection_channel == "rider_cash":
        db.add(RiderCashLedger(branch_id=pay.branch_id, rider_id=pay.collected_by_rider_id, order_id=order.id, payment_id=pay.id, entry_type="reversal",
                               amount_paise=pay.amount_paise, note=f"Reversed: {reason.strip()}"[:300], created_by_role="admin", created_by_id=admin_id))
    pay.status, pay.verification_status, pay.paid_at, pay.verified_at = "pending", "unverified", None, None
    pay.tendered_paise = pay.change_paise = pay.confirmed_by_admin_id = None
    pay.collected_by_rider_id = pay.collected_at = pay.collection_channel = None
    order.payment_status = "unpaid"
    await audit(db, actor_role="admin", actor_id=admin_id, action="payment.cash_reversed", entity_type="payment", entity_id=pay.id,
                before=before, after={"status": "pending", "reason": reason.strip()}, branch_id=pay.branch_id)
    await ev.emit(db, type="payment.reversed", topics=["admin", f"order:{order.id}"], dedupe_key=f"payment.rev:{pay.id}:{now_utc().timestamp()}",
                  data={"order_id": order.id, "payment_id": pay.id, "payment_status": "unpaid"}, push=False, branch_id=order.branch_id)
    return pay


# ------------------------------------------------------------------ cash taken by a rider at the door
async def record_rider_cash(db: AsyncSession, payment: Payment, rider, tendered_paise: int) -> tuple[Payment, bool]:
    """Rider says "cash received". Returns (payment, already_recorded). Safe to call twice: the second call changes nothing."""
    pay = await _lock_payment(db, payment.id)
    if pay.status in PAID:
        if pay.collected_by_rider_id == rider.id:
            return pay, True
        raise Conflict("This order is already paid", code="already_paid")
    if pay.method != "cash" or pay.status == "cancelled":
        raise Conflict("This order is no longer payable in cash", code="not_cash")
    order = await st.lock_order(db, pay.order_id)
    if order.payment_status in PAID:
        raise Conflict("This order is already paid online", code="already_paid")
    if tendered_paise < pay.amount_paise:
        raise Invalid(f"Amount received is less than the payable ₹{pay.amount_paise / 100:.2f}", code="insufficient_amount",
                      details={"amount_due_paise": pay.amount_paise})
    now = now_utc()
    pay.status, pay.verification_status, pay.paid_at, pay.verified_at = "paid", "verified", now, now
    pay.tendered_paise, pay.change_paise = tendered_paise, tendered_paise - pay.amount_paise
    pay.collected_by_rider_id, pay.collected_at, pay.collection_channel = rider.id, now, "rider_cash"
    order.payment_status = "paid"
    db.add(RiderCashLedger(branch_id=pay.branch_id, rider_id=rider.id, order_id=order.id, payment_id=pay.id, entry_type="collected",
                           amount_paise=pay.amount_paise, note=f"Order {order.order_number}", created_by_role="rider", created_by_id=rider.id))
    await audit(db, actor_role="rider", actor_id=rider.id, action="payment.cash_collected_by_rider", entity_type="payment", entity_id=pay.id,
                after={"order_id": str(order.id), "amount_paise": pay.amount_paise, "tendered_paise": tendered_paise}, branch_id=pay.branch_id)
    await ev.emit(db, type="payment.succeeded", topics=st.order_topics(order), dedupe_key=f"payment.ok:{pay.id}", recipients=st.customer_recipients(order),
                  title="Payment received", body=f"Cash payment for order {order.order_number} received. Thank you!",
                  data={"order_id": order.id, "payment_id": pay.id, "amount_paise": pay.amount_paise, "payment_status": "paid", "collected_by": "rider"}, branch_id=order.branch_id)
    return pay, False


async def rider_cash_outstanding(db: AsyncSession, rider_id: uuid.UUID) -> int:
    rows = (await db.execute(select(RiderCashLedger.entry_type, func.coalesce(func.sum(RiderCashLedger.amount_paise), 0))
                             .where(RiderCashLedger.rider_id == rider_id).group_by(RiderCashLedger.entry_type))).all()
    t = {k: int(v) for k, v in rows}
    return t.get("collected", 0) - t.get("handover", 0) - t.get("reversal", 0)


# ------------------------------------------------------------------ refunds
async def refundable_remaining(db: AsyncSession, pay: Payment) -> int:
    pending = (await db.execute(select(func.coalesce(func.sum(Refund.amount_paise), 0)).where(Refund.payment_id == pay.id, Refund.status == "pending"))).scalar_one()
    return pay.amount_paise - pay.refunded_paise - int(pending)


async def create_refund(db: AsyncSession, pay: Payment, amount_paise: int, reason: str, admin_id: uuid.UUID | None, idempotency_key: str | None,
                        actor_role: str = "admin") -> Refund:
    if idempotency_key:
        ex = (await db.execute(select(Refund).where(Refund.idempotency_key == idempotency_key))).scalar_one_or_none()
        if ex:
            return ex
    pay = await _lock_payment(db, pay.id)
    if pay.status not in ("paid", "partially_refunded"):
        raise Conflict("Only paid payments can be refunded", code="not_refundable")
    if amount_paise <= 0:
        raise Invalid("Refund amount must be positive", code="invalid_amount")
    remaining = await refundable_remaining(db, pay)
    if amount_paise > remaining:
        raise Conflict(f"Refundable balance is ₹{remaining / 100:.2f}", code="refund_exceeds_balance")
    r = Refund(payment_id=pay.id, order_id=pay.order_id, amount_paise=amount_paise, reason=reason[:300], status="pending", method=pay.method,
               requested_by_admin_id=admin_id, idempotency_key=idempotency_key)
    db.add(r)
    await db.flush()
    if pay.method == "online":
        await jobs.enqueue(db, "process_refund", {"refund_id": str(r.id)}, key=f"refund:{r.id}")
    await audit(db, actor_role=actor_role, actor_id=admin_id, action="refund.requested", entity_type="refund", entity_id=r.id,
                after={"payment_id": str(pay.id), "amount_paise": amount_paise, "reason": reason}, branch_id=pay.branch_id)
    order = await db.get(Order, pay.order_id)
    if order.status in ("cancelled", "rejected"):
        await st.transition(db, order, "refund_pending", actor_role=actor_role, actor_id=admin_id, reason="refund started")
    return r


async def on_order_closed_unfulfilled(db: AsyncSession, order: Order, actor_role: str, actor_id: uuid.UUID | None) -> None:
    pays = (await db.execute(select(Payment).where(Payment.order_id == order.id))).scalars().all()
    for p in pays:
        if p.status in ("created", "pending", "failed"):
            p.status = "cancelled" if p.method == "cash" else p.status  # online attempts stay open: a late success triggers a refund
    paid = [p for p in pays if p.status in ("paid", "partially_refunded")]
    refunded_any = False
    for p in paid:
        remaining = await refundable_remaining(db, p)
        if remaining > 0:
            await create_refund(db, p, remaining, f"Order {order.status}: {order.rejection_reason or order.cancellation_reason or ''}".strip(),
                                actor_id if actor_role == "admin" else None, f"auto:{order.id}:{p.id}", actor_role=actor_role)
            refunded_any = True
    if not refunded_any and not paid and order.payment_status in ("unpaid", "pending", "failed"):
        pass


async def _refresh_after_refund(db: AsyncSession, refund: Refund) -> None:
    pay = await _lock_payment(db, refund.payment_id)
    order = await st.lock_order(db, refund.order_id)
    pay.refunded_paise = (await db.execute(select(func.coalesce(func.sum(Refund.amount_paise), 0)).where(Refund.payment_id == pay.id, Refund.status == "succeeded"))).scalar_one()
    if pay.refunded_paise >= pay.amount_paise:
        pay.status, order.payment_status = "refunded", "refunded"
    elif pay.refunded_paise > 0:
        pay.status, order.payment_status = "partially_refunded", "partially_refunded"
    open_refunds = (await db.execute(select(func.count()).select_from(Refund).where(Refund.order_id == order.id, Refund.status == "pending"))).scalar_one()
    if order.status == "refund_pending" and open_refunds == 0 and pay.status == "refunded":
        await st.transition(db, order, "refunded", actor_role="system", reason="refund confirmed")
    await ev.emit(db, type="refund.succeeded", topics=st.order_topics(order), dedupe_key=f"refund.ok:{refund.id}", recipients=st.customer_recipients(order),
                  title="Refund processed", body=f"₹{refund.amount_paise / 100:.2f} refunded for order {order.order_number}.",
                  data={"order_id": order.id, "refund_id": refund.id, "amount_paise": refund.amount_paise}, branch_id=order.branch_id)


async def apply_refund_info(db: AsyncSession, refund_id: uuid.UUID, info: RefundInfo) -> Refund:
    refund = (await db.execute(select(Refund).where(Refund.id == refund_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
    if info.gateway_refund_id:
        refund.gateway_refund_id = info.gateway_refund_id
    if refund.status == "succeeded":
        return refund
    if info.status == "succeeded":
        refund.status, refund.processed_at = "succeeded", now_utc()
        await _refresh_after_refund(db, refund)
    elif info.status == "failed":
        refund.status, refund.failure_reason = "failed", (info.failure_reason or "Refund failed at provider")[:300]
        await ev.emit(db, type="refund.failed", topics=["admin"], dedupe_key=f"refund.fail:{refund.id}", recipients=[{"role": "admin"}], title="Refund failed",
                      body="A refund failed at the payment provider. Review and retry.", data={"refund_id": refund.id, "order_id": refund.order_id}, branch_id=None)
    return refund


async def process_refund(db: AsyncSession, refund_id: uuid.UUID) -> None:
    refund = (await db.execute(select(Refund).where(Refund.id == refund_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if refund is None or refund.status != "pending" or refund.method != "online":
        return
    pay = await db.get(Payment, refund.payment_id)
    gw = registry.get_gateway(pay.gateway)
    try:
        if refund.gateway_refund_id:
            info = await gw.fetch_refund(gateway_order_id=pay.gateway_order_id, gateway_payment_id=pay.gateway_payment_id,
                                         refund_ref=refund.id.hex, gateway_refund_id=refund.gateway_refund_id)
        else:
            info = await gw.create_refund(gateway_order_id=pay.gateway_order_id, gateway_payment_id=pay.gateway_payment_id,
                                          amount_paise=refund.amount_paise, refund_ref=str(refund.id) if pay.gateway == "razorpay" else refund.id.hex,
                                          reason=refund.reason)
    except GatewayError as e:
        if e.transient:
            raise  # job retry with backoff
        info = RefundInfo(status="failed", failure_reason=str(e))
    await apply_refund_info(db, refund.id, info)


async def confirm_cash_refund(db: AsyncSession, refund_id: uuid.UUID, admin_id: uuid.UUID) -> Refund:
    refund = (await db.execute(select(Refund).where(Refund.id == refund_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if refund is None:
        raise NotFound("Refund not found")
    if refund.method != "cash":
        raise Invalid("Online refunds are confirmed by the payment provider", code="not_cash")
    if refund.status == "succeeded":
        raise Conflict("Already confirmed", code="already_confirmed")
    refund.status, refund.processed_at, refund.failure_reason = "succeeded", now_utc(), None
    await audit(db, actor_role="admin", actor_id=admin_id, action="refund.cash_confirmed", entity_type="refund", entity_id=refund.id,
                after={"amount_paise": refund.amount_paise})
    await _refresh_after_refund(db, refund)
    return refund


async def retry_refund(db: AsyncSession, refund_id: uuid.UUID, admin_id: uuid.UUID) -> Refund:
    refund = await db.get(Refund, refund_id)
    if refund is None:
        raise NotFound("Refund not found")
    if refund.status != "failed":
        raise Conflict("Only failed refunds can be retried", code="not_failed")
    refund.status, refund.failure_reason = "pending", None
    if refund.method == "online":
        await jobs.enqueue(db, "process_refund", {"refund_id": str(refund.id)}, key=f"refund:{refund.id}:retry:{int(now_utc().timestamp())}")
    await audit(db, actor_role="admin", actor_id=admin_id, action="refund.retry", entity_type="refund", entity_id=refund.id)
    return refund
