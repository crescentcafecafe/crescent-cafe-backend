"""Admin: dashboard, orders (kitchen workflow), payments & refunds, riders & deliveries."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, Header
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import page
from app.core.errors import Conflict, Invalid, NotFound
from app.core.permissions import Principal, current_admin
from app.core.security import hash_password, now_utc, validate_password_strength
from app.db.session import get_db
from app.models.chat import ConversationMember, Message
from app.models.core import Customer, Rider
from app.models.delivery import ACTIVE_ASSIGNMENT, RiderAssignment
from app.models.orders import Order
from app.models.payments import Payment, PaymentWebhookEvent, Refund
from app.models.system import AuditLog, Job
from app.models.tables import DiningTable
from app.schemas.admin import (AssignIn, CashConfirmIn, CashHandoverIn, CashReverseIn, RefundIn, ResetPasswordIn, RiderCreate, RiderPatch,
                               BroadcastIn)
from app.schemas.common import OK
from app.schemas.orders import AdminStatusChange, CancelRequest, OrderCreate
from app.services import cash as cash_svc, delivery as delivery_svc, events as ev, orders as svc, order_state as st, payments as pay_svc, settings as cfg
from app.services.audit import audit
from app.utils.phone import normalize_phone
from app.utils.timeutil import local_day_bounds, to_local

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(current_admin)])


@router.get("/me")
async def me(a: Principal = Depends(current_admin)):
    u = a.user
    return {"id": u.id, "email": u.email, "name": u.name, "branch_id": u.branch_id, "last_login_at": u.last_login_at}


@router.get("/dashboard")
async def dashboard(a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    bid = a.user.branch_id
    cs = await cfg.get_cafe_settings(db, bid)
    lo, hi = local_day_bounds(to_local(now_utc(), cs.timezone).date(), cs.timezone)
    by_status = dict((await db.execute(select(Order.status, func.count()).where(Order.branch_id == bid, Order.status.in_(st.ACTIVE_STATUSES)).group_by(Order.status))).all())
    placed_today = (await db.execute(select(func.count()).select_from(Order).where(Order.branch_id == bid, Order.created_at >= lo, Order.created_at < hi))).scalar_one()
    sales_today = (await db.execute(select(func.coalesce(func.sum(Order.total_paise), 0)).where(Order.branch_id == bid, Order.status.in_(st.FINAL_OK),
                                                                                                   Order.completed_at >= lo, Order.completed_at < hi))).scalar_one()
    pending_cash = (await db.execute(select(func.count()).select_from(Payment).where(Payment.branch_id == bid, Payment.method == "cash", Payment.status == "pending"))).scalar_one()
    active_deliv = (await db.execute(select(func.count()).select_from(RiderAssignment).where(RiderAssignment.status.in_(ACTIVE_ASSIGNMENT)))).scalar_one()
    avail = (await db.execute(select(func.count()).select_from(Rider).where(Rider.branch_id == bid, Rider.is_active.is_(True), Rider.is_available.is_(True)))).scalar_one()
    unread = (await db.execute(select(func.count()).select_from(Message).join(ConversationMember, ConversationMember.conversation_id == Message.conversation_id).where(
        ConversationMember.role == "admin", ConversationMember.user_id == a.id, Message.id > ConversationMember.last_read_message_id, Message.sender_role != "admin",
        Message.is_internal.is_(False)))).scalar_one()
    from app.models.settings import TaxSetting

    warnings = []
    tax = (await db.execute(select(TaxSetting).where(TaxSetting.branch_id == bid, TaxSetting.is_active.is_(True)))).scalars().all()
    if not tax or not any(t.rate_bps > 0 for t in tax):
        warnings.append("No tax rate configured: orders are currently taxed at 0%. The owner must verify and set tax rates before going live.")
    if not cs.razorpay_enabled and not cs.cashfree_enabled:
        warnings.append("No online payment gateway is enabled; only pay-at-counter is offered.")
    if not cs.opening_hours:
        warnings.append("Opening hours are not set (the cafe is treated as always open).")
    return {"pending_decisions": by_status.get("placed", 0) + by_status.get("payment_verified", 0), "active_orders_by_status": by_status,
            "orders_placed_today": placed_today, "sales_today_paise": int(sales_today), "pending_counter_payments": pending_cash,
            "active_deliveries": active_deliv, "available_riders": avail, "unread_messages": unread,
            "accepting_orders": cs.accepting_orders and not cs.temporarily_closed, "setup_warnings": warnings, "server_time": now_utc()}


# ------------------------------------------------------------------ orders
@router.get("/orders")
async def list_orders(status: str | None = None, order_type: str | None = None, payment_status: str | None = None, table_id: uuid.UUID | None = None,
                      table_session_id: uuid.UUID | None = None, phone: str | None = None, q: str | None = None, date_from: date | None = None,
                      date_to: date | None = None, limit: int = 50, offset: int = 0, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    limit, offset = page(limit, offset)
    cs = await cfg.get_cafe_settings(db, a.user.branch_id)
    stmt = select(Order).where(Order.branch_id == a.user.branch_id)
    if status:
        stmt = stmt.where(Order.status.in_(status.split(",")))
    if order_type:
        stmt = stmt.where(Order.order_type == order_type)
    if payment_status:
        stmt = stmt.where(Order.payment_status == payment_status)
    if table_id:
        stmt = stmt.where(Order.table_id == table_id)
    if table_session_id:
        stmt = stmt.where(Order.table_session_id == table_session_id)
    if phone:
        stmt = stmt.where(Order.customer_phone == normalize_phone(phone))
    if q:
        stmt = stmt.where(or_(Order.order_number.ilike(f"%{q}%"), Order.customer_name.ilike(f"%{q}%")))
    if date_from:
        stmt = stmt.where(Order.created_at >= local_day_bounds(date_from, cs.timezone)[0])
    if date_to:
        stmt = stmt.where(Order.created_at < local_day_bounds(date_to, cs.timezone)[1])
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(Order.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [svc.order_summary(o) for o in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/orders/pending")
async def pending_orders(a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Orders waiting for the Admin's accept/reject decision (drives the alarm; also use to resync after reconnecting)."""
    rows = (await db.execute(select(Order).where(Order.branch_id == a.user.branch_id, Order.status.in_(("placed", "payment_verified"))).order_by(Order.created_at))).scalars().all()
    return [await svc.order_view(db, o, "admin") for o in rows]


@router.get("/orders/{order_id}")
async def order_detail(order_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    o = await db.get(Order, order_id)
    if o is None or o.branch_id != a.user.branch_id:
        raise NotFound("Order not found")
    return await svc.order_view(db, o, "admin", with_history=True)


@router.post("/orders", status_code=201)
async def create_walk_in(body: OrderCreate, a: Principal = Depends(current_admin), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100),
                         db: AsyncSession = Depends(get_db)):
    """Walk-in order created by the Admin. Same validation, pricing, tax and history rules as customer orders."""
    body.order_type = "walk_in"
    order, _tok, dup = await svc.create_order(db, branch_id=a.user.branch_id, data=body, customer=None, admin_id=a.id, idempotency_key=idempotency_key)
    return {"order": await svc.order_view(db, order, "admin"), "duplicate": dup}


async def _own(db: AsyncSession, a: Principal, order_id: uuid.UUID) -> Order:
    o = await db.get(Order, order_id)
    if o is None or o.branch_id != a.user.branch_id:
        raise NotFound("Order not found")
    return o


@router.post("/orders/{order_id}/accept")
async def accept(order_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    o = await _own(db, a, order_id)
    o = await st.transition(db, o, "accepted", actor_role="admin", actor_id=a.id)
    return await svc.order_view(db, o, "admin")


@router.post("/orders/{order_id}/reject")
async def reject(order_id: uuid.UUID, body: CancelRequest, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    o = await _own(db, a, order_id)
    o = await st.transition(db, o, "rejected", actor_role="admin", actor_id=a.id, reason=body.reason)
    await audit(db, actor_role="admin", actor_id=a.id, action="order.rejected", entity_type="order", entity_id=o.id, after={"reason": body.reason}, branch_id=o.branch_id)
    return await svc.order_view(db, o, "admin")


@router.post("/orders/{order_id}/cancel")
async def cancel(order_id: uuid.UUID, body: CancelRequest, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    o = await _own(db, a, order_id)
    o = await st.transition(db, o, "cancelled", actor_role="admin", actor_id=a.id, reason=body.reason)
    await audit(db, actor_role="admin", actor_id=a.id, action="order.cancelled", entity_type="order", entity_id=o.id, after={"reason": body.reason}, branch_id=o.branch_id)
    return await svc.order_view(db, o, "admin")


@router.post("/orders/{order_id}/status")
async def kitchen_status(order_id: uuid.UUID, body: AdminStatusChange, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Kitchen workflow: preparing -> ready -> completed (completed = served / collected; delivery orders finish via rider OTP only)."""
    o = await _own(db, a, order_id)
    if body.status == "completed" and o.order_type == "delivery":
        raise Conflict("Delivery orders are completed by the rider verifying the customer's OTP", code="otp_required")
    o = await st.transition(db, o, body.status, actor_role="admin", actor_id=a.id)
    return await svc.order_view(db, o, "admin")


@router.post("/orders/{order_id}/alarm/ack", response_model=OK)
async def alarm_ack(order_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Admin app confirms it received/seen the new-order alarm. The alarm keeps ringing client-side until accept/reject unless the app chooses to silence it."""
    o = await _own(db, a, order_id)
    if o.admin_alarm_acked_at is None:
        o.admin_alarm_acked_at = now_utc()
        await ev.emit(db, type="order.alarm_ack", topics=["admin"], dedupe_key=f"order.alarm_ack:{o.id}", data={"order_id": o.id}, push=False, branch_id=o.branch_id)
    return OK()


@router.post("/orders/{order_id}/assign")
async def assign_rider(order_id: uuid.UUID, body: AssignIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Manual rider assignment (the system never auto-assigns). Order must be 'ready'."""
    await _own(db, a, order_id)
    asg = await delivery_svc.assign(db, order_id, body.rider_id, a.id)
    return {"assignment_id": asg.id, "status": asg.status, "rider_id": asg.rider_id, "order_id": asg.order_id}


@router.post("/assignments/{assignment_id}/cancel")
async def cancel_assignment(assignment_id: uuid.UUID, body: CancelRequest, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    asg = await delivery_svc.admin_cancel_assignment(db, assignment_id, a.id, body.reason)
    return {"assignment_id": asg.id, "status": asg.status}


@router.post("/orders/{order_id}/delivery-otp/resend", response_model=OK)
async def resend_otp(order_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Issue a fresh delivery OTP and push it to the customer. The Admin never sees the code."""
    o = await _own(db, a, order_id)
    if o.status != "out_for_delivery":
        raise Conflict("Order is not out for delivery", code="invalid_state")
    asg = (await db.execute(select(RiderAssignment).where(RiderAssignment.order_id == o.id, RiderAssignment.status == "picked_up"))).scalar_one_or_none()
    await delivery_svc.issue_otp(db, o, asg.id if asg else None, notify=True)
    await audit(db, actor_role="admin", actor_id=a.id, action="delivery.otp_resent", entity_type="order", entity_id=o.id)
    return OK()


@router.get("/deliveries")
async def deliveries(state: str = "active", limit: int = 50, offset: int = 0, db: AsyncSession = Depends(get_db)):
    limit, offset = page(limit, offset)
    sts = ACTIVE_ASSIGNMENT if state == "active" else ("completed", "rejected", "cancelled")
    rows = (await db.execute(select(RiderAssignment).where(RiderAssignment.status.in_(sts)).order_by(RiderAssignment.offered_at.desc()).limit(limit).offset(offset))).scalars().all()
    out = []
    for r in rows:
        o, rd = await db.get(Order, r.order_id), await db.get(Rider, r.rider_id)
        out.append({"assignment_id": r.id, "status": r.status, "reject_reason": r.reject_reason, "offered_at": r.offered_at, "picked_up_at": r.picked_up_at,
                    "completed_at": r.completed_at, "order": svc.order_summary(o), "rider": {"id": rd.id, "name": rd.name, "phone": rd.phone}})
    return out


# ------------------------------------------------------------------ riders
def _rider(r: Rider, busy: bool | None = None) -> dict:
    d = {"id": r.id, "name": r.name, "phone": r.phone, "email": r.email, "vehicle_info": r.vehicle_info, "is_active": r.is_active,
         "is_available": r.is_available, "must_change_password": r.must_change_password, "created_at": r.created_at}
    if busy is not None:
        d["has_active_delivery"] = busy
    return d


@router.get("/riders")
async def riders(active: bool | None = None, available: bool | None = None, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    q = select(Rider).where(Rider.branch_id == a.user.branch_id)
    if active is not None:
        q = q.where(Rider.is_active.is_(active))
    if available is not None:
        q = q.where(Rider.is_available.is_(available))
    rows = (await db.execute(q.order_by(Rider.name))).scalars().all()
    busy_ids = {r for (r,) in (await db.execute(select(RiderAssignment.rider_id).where(RiderAssignment.status.in_(ACTIVE_ASSIGNMENT)))).all()}
    return [_rider(r, r.id in busy_ids) for r in rows]


@router.post("/riders", status_code=201)
async def create_rider(body: RiderCreate, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    err = validate_password_strength(body.password)
    if err:
        raise Invalid(err, code="weak_password")
    phone = normalize_phone(body.phone)
    if (await db.execute(select(Rider).where(Rider.phone == phone))).scalar_one_or_none():
        raise Conflict("A rider with this phone already exists", code="phone_taken")
    r = Rider(branch_id=a.user.branch_id, name=body.name.strip(), phone=phone, email=body.email, vehicle_info=body.vehicle_info,
              password_hash=hash_password(body.password), must_change_password=True)
    db.add(r)
    await db.flush()
    await audit(db, actor_role="admin", actor_id=a.id, action="rider.created", entity_type="rider", entity_id=r.id, branch_id=r.branch_id)
    return _rider(r)


async def _rider_of(db, a, rider_id) -> Rider:
    r = await db.get(Rider, rider_id)
    if r is None or r.branch_id != a.user.branch_id:
        raise NotFound("Rider not found")
    return r


@router.get("/riders/{rider_id}")
async def get_rider(rider_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return _rider(await _rider_of(db, a, rider_id))


@router.patch("/riders/{rider_id}")
async def patch_rider(rider_id: uuid.UUID, body: RiderPatch, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    from app.services.auth import revoke_all_sessions

    r = await _rider_of(db, a, rider_id)
    data = body.model_dump(exclude_unset=True)
    if "phone" in data and data["phone"]:
        phone = normalize_phone(data.pop("phone"))
        if phone != r.phone and (await db.execute(select(Rider).where(Rider.phone == phone))).scalar_one_or_none():
            raise Conflict("A rider with this phone already exists", code="phone_taken")
        r.phone = phone
    if data.get("is_active") is False:
        busy = (await db.execute(select(RiderAssignment).where(RiderAssignment.rider_id == r.id, RiderAssignment.status.in_(ACTIVE_ASSIGNMENT)).limit(1))).scalar_one_or_none()
        if busy:
            raise Conflict("Rider has an active delivery. Reassign it first.", code="rider_busy")
        r.is_available = False
        await revoke_all_sessions(db, "rider", r.id)
    for k, v in data.items():
        setattr(r, k, v)
    await audit(db, actor_role="admin", actor_id=a.id, action="rider.updated", entity_type="rider", entity_id=r.id, after=data and {k: str(v) for k, v in data.items()}, branch_id=r.branch_id)
    return _rider(r)


@router.post("/riders/{rider_id}/reset-password", response_model=OK)
async def reset_rider_password(rider_id: uuid.UUID, body: ResetPasswordIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    from app.services.auth import revoke_all_sessions

    err = validate_password_strength(body.new_password)
    if err:
        raise Invalid(err, code="weak_password")
    r = await _rider_of(db, a, rider_id)
    r.password_hash, r.must_change_password, r.failed_logins, r.locked_until = hash_password(body.new_password), True, 0, None
    await revoke_all_sessions(db, "rider", r.id)
    await audit(db, actor_role="admin", actor_id=a.id, action="rider.password_reset", entity_type="rider", entity_id=r.id, branch_id=r.branch_id)
    return OK()


# ------------------------------------------------------------------ payments & refunds
def _pay(p: Payment, o: Order | None = None) -> dict:
    d = {"id": p.id, "order_id": p.order_id, "method": p.method, "gateway": p.gateway, "gateway_order_id": p.gateway_order_id,
         "gateway_payment_id": p.gateway_payment_id, "amount_paise": p.amount_paise, "currency": p.currency, "status": p.status,
         "verification_status": p.verification_status, "refunded_paise": p.refunded_paise, "tendered_paise": p.tendered_paise, "change_paise": p.change_paise,
         "confirmed_by_admin_id": p.confirmed_by_admin_id, "paid_at": p.paid_at, "verified_at": p.verified_at, "created_at": p.created_at,
         "failure_reason": p.failure_reason}
    if o:
        d["order_number"], d["order_status"], d["table_label"], d["customer_name"] = o.order_number, o.status, o.table_label, o.customer_name
    return d


@router.get("/payments")
async def payments(status: str | None = None, method: str | None = None, gateway: str | None = None, order_id: uuid.UUID | None = None, limit: int = 50, offset: int = 0,
                   a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    limit, offset = page(limit, offset)
    q = select(Payment, Order).join(Order, Order.id == Payment.order_id).where(Payment.branch_id == a.user.branch_id)
    for col, v in ((Payment.status, status), (Payment.method, method), (Payment.gateway, gateway), (Payment.order_id, order_id)):
        if v:
            q = q.where(col == v)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(Payment.created_at.desc()).limit(limit).offset(offset))).all()
    return {"items": [_pay(p, o) for p, o in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/payments/counter-pending")
async def counter_pending(a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Cash / pay-at-counter payments awaiting the Admin's confirmation (excludes closed orders)."""
    rows = (await db.execute(select(Payment, Order).join(Order, Order.id == Payment.order_id).where(
        Payment.branch_id == a.user.branch_id, Payment.method == "cash", Payment.status == "pending",
        Order.status.not_in(("rejected", "cancelled", "refund_pending", "refunded"))).order_by(Payment.created_at))).all()
    return [_pay(p, o) for p, o in rows]


@router.post("/payments/{payment_id}/confirm-cash")
async def confirm_cash(payment_id: uuid.UUID, body: CashConfirmIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    p = await pay_svc.confirm_cash(db, payment_id, a.id, body.amount_received_paise)
    return _pay(p)


@router.post("/payments/{payment_id}/reverse-cash")
async def reverse_cash(payment_id: uuid.UUID, body: CashReverseIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Audited correction for a cash payment confirmed by mistake."""
    return _pay(await pay_svc.reverse_cash(db, payment_id, a.id, body.reason))


@router.post("/payments/{payment_id}/sync")
async def sync_payment(payment_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    p = await db.get(Payment, payment_id)
    if p is None:
        raise NotFound("Payment not found")
    return _pay(await pay_svc.sync_payment(db, p, "admin_sync"))


@router.post("/payments/{payment_id}/refunds", status_code=201)
async def create_refund(payment_id: uuid.UUID, body: RefundIn, a: Principal = Depends(current_admin),
                        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100), db: AsyncSession = Depends(get_db)):
    """Starts a refund. Online refunds stay 'pending' until the provider confirms (webhook or reconciliation); nothing is promised before that."""
    p = await db.get(Payment, payment_id)
    if p is None:
        raise NotFound("Payment not found")
    amount = body.amount_paise or await pay_svc.refundable_remaining(db, p)
    r = await pay_svc.create_refund(db, p, amount, body.reason, a.id, idempotency_key)
    return _refund(r)


def _refund(r: Refund) -> dict:
    return {"id": r.id, "payment_id": r.payment_id, "order_id": r.order_id, "amount_paise": r.amount_paise, "reason": r.reason, "status": r.status,
            "method": r.method, "gateway_refund_id": r.gateway_refund_id, "failure_reason": r.failure_reason, "created_at": r.created_at, "processed_at": r.processed_at}


@router.get("/refunds")
async def refunds(status: str | None = None, limit: int = 50, offset: int = 0, db: AsyncSession = Depends(get_db)):
    limit, offset = page(limit, offset)
    q = select(Refund)
    if status:
        q = q.where(Refund.status == status)
    rows = (await db.execute(q.order_by(Refund.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return [_refund(r) for r in rows]


@router.post("/refunds/{refund_id}/retry")
async def retry_refund(refund_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return _refund(await pay_svc.retry_refund(db, refund_id, a.id))


@router.post("/refunds/{refund_id}/confirm-cash")
async def confirm_cash_refund(refund_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """For cash payments: Admin confirms the money was handed back."""
    return _refund(await pay_svc.confirm_cash_refund(db, refund_id, a.id))


@router.get("/webhook-events")
async def webhook_events(status: str | None = None, limit: int = 50, db: AsyncSession = Depends(get_db)):
    q = select(PaymentWebhookEvent)
    if status:
        q = q.where(PaymentWebhookEvent.status == status)
    rows = (await db.execute(q.order_by(PaymentWebhookEvent.created_at.desc()).limit(max(1, min(limit, 200))))).scalars().all()
    return [{"id": r.id, "gateway": r.gateway, "event_id": r.event_id, "event_type": r.event_type, "status": r.status, "error": r.error, "created_at": r.created_at} for r in rows]


# ------------------------------------------------------------------ jobs / audit / broadcast
@router.get("/jobs")
async def jobs(status: str | None = None, limit: int = 50, db: AsyncSession = Depends(get_db)):
    """Inspect the background-job queue (failed jobs can be retried)."""
    q = select(Job)
    if status:
        q = q.where(Job.status == status)
    rows = (await db.execute(q.order_by(Job.created_at.desc()).limit(max(1, min(limit, 200))))).scalars().all()
    return [{"id": j.id, "kind": j.kind, "status": j.status, "attempts": j.attempts, "max_attempts": j.max_attempts, "run_at": j.run_at, "last_error": j.last_error, "created_at": j.created_at} for j in rows]


@router.post("/jobs/{job_id}/retry", response_model=OK)
async def retry_job(job_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    j = await db.get(Job, job_id)
    if j is None:
        raise NotFound("Job not found")
    if j.status != "failed":
        raise Conflict("Only failed jobs can be retried", code="not_failed")
    j.status, j.attempts, j.run_at, j.last_error = "queued", 0, now_utc(), None
    await audit(db, actor_role="admin", actor_id=a.id, action="job.retry", entity_type="job", entity_id=j.id)
    return OK()


@router.get("/audit-logs")
async def audit_logs(entity_type: str | None = None, entity_id: str | None = None, action: str | None = None, limit: int = 100, offset: int = 0, db: AsyncSession = Depends(get_db)):
    limit, offset = page(limit, offset)
    q = select(AuditLog)
    for col, v in ((AuditLog.entity_type, entity_type), (AuditLog.entity_id, entity_id), (AuditLog.action, action)):
        if v:
            q = q.where(col == v)
    rows = (await db.execute(q.order_by(AuditLog.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [{"id": r.id, "actor_role": r.actor_role, "actor_id": r.actor_id, "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id,
             "before": r.before, "after": r.after, "created_at": r.created_at} for r in rows]


@router.post("/notifications/broadcast", response_model=OK)
async def broadcast(body: BroadcastIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Promotional push to customers who opted in to marketing."""
    cs = await cfg.get_cafe_settings(db, a.user.branch_id)
    if not cs.notify_customers_promotions:
        raise Conflict("Promotional notifications are disabled in settings", code="promotions_disabled")
    await ev.emit(db, type="promotion", topics=[], dedupe_key=f"promo:{uuid.uuid4()}", recipients=[{"role": "all_customers_opt_in"}], title=body.title, body=body.body,
                  branch_id=a.user.branch_id)
    await audit(db, actor_role="admin", actor_id=a.id, action="notification.broadcast", entity_type="promotion", after={"title": body.title})
    return OK()


# ---------------------------------------------------------------- rider cash
@router.get("/cash/summary")
async def cash_summary(start: date | None = None, end: date | None = None, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Cash each rider holds (collected - handed over - reversed). start/end add a 'collected in period' column."""
    cs = await cfg.get_cafe_settings(db, a.user.branch_id)
    s_dt = local_day_bounds(start, cs.timezone)[0] if start else None
    e_dt = local_day_bounds(end, cs.timezone)[1] if end else None
    return await cash_svc.summary(db, a.user.branch_id, s_dt, e_dt)


@router.get("/riders/{rider_id}/cash-ledger")
async def rider_cash_ledger(rider_id: uuid.UUID, limit: int = 50, offset: int = 0, db: AsyncSession = Depends(get_db)):
    lim, off = page(limit, offset)
    return await cash_svc.ledger(db, rider_id, lim, off)


@router.post("/riders/{rider_id}/cash-handover")
async def rider_cash_handover(rider_id: uuid.UUID, body: CashHandoverIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """The rider gave cash to the cafe: record it so the outstanding balance goes down."""
    return await cash_svc.handover(db, a.id, rider_id, body.amount_paise, body.note)


# ---------------------------------------------------------------- customers (user management)
def _customer_row(c: Customer, orders: int, spent: int, last: datetime | None) -> dict:
    return {"id": c.id, "name": c.name, "email": c.email, "phone": c.phone, "is_active": c.is_active, "google_linked": c.google_sub is not None,
            "marketing_opt_in": c.marketing_opt_in, "created_at": c.created_at, "orders_count": orders, "total_spent_paise": spent, "last_order_at": last}


@router.get("/customers")
async def list_customers(q: str | None = None, limit: int = 50, offset: int = 0, db: AsyncSession = Depends(get_db)):
    lim, off = page(limit, offset)
    base = select(Customer).where(Customer.anonymized_at.is_(None))
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        base = base.where(or_(func.lower(Customer.name).like(like), func.lower(func.coalesce(Customer.email, "")).like(like), func.coalesce(Customer.phone, "").like(f"%{q.strip()}%")))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(Customer.created_at.desc()).limit(lim).offset(off))).scalars().all()
    stats = {}
    if rows:
        for cid, n, spent, last in (await db.execute(select(Order.customer_id, func.count(), func.coalesce(func.sum(Order.total_paise), 0), func.max(Order.created_at))
                                                    .where(Order.customer_id.in_([c.id for c in rows]), Order.status.in_(st.FINAL_OK))
                                                    .group_by(Order.customer_id))).all():
            stats[cid] = (n, int(spent), last)
    return {"total": total, "items": [_customer_row(c, *stats.get(c.id, (0, 0, None))) for c in rows]}


@router.get("/customers/{customer_id}")
async def customer_detail(customer_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    c = await db.get(Customer, customer_id)
    if c is None or c.anonymized_at is not None:
        raise NotFound("Customer not found")
    orders = (await db.execute(select(Order).where(Order.customer_id == c.id).order_by(Order.created_at.desc()).limit(25))).scalars().all()
    return {**_customer_row(c, len(orders), sum(o.total_paise for o in orders if o.status in st.FINAL_OK), orders[0].created_at if orders else None),
            "orders": [{"id": o.id, "order_number": o.order_number, "status": o.status, "payment_status": o.payment_status, "order_type": o.order_type,
                        "total_paise": o.total_paise, "created_at": o.created_at} for o in orders]}


@router.patch("/customers/{customer_id}/active", response_model=OK)
async def set_customer_active(customer_id: uuid.UUID, body: dict, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    c = await db.get(Customer, customer_id)
    if c is None or c.anonymized_at is not None:
        raise NotFound("Customer not found")
    active = bool(body.get("is_active"))
    c.is_active = active
    await audit(db, actor_role="admin", actor_id=a.id, action="customer.active_changed", entity_type="customer", entity_id=c.id, after={"is_active": active}, branch_id=a.user.branch_id)
    return OK()
