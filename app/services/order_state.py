"""Backend-controlled order state machine. Every status change in the system goes through transition()."""
from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Forbidden, Invalid
from app.core.security import now_utc
from app.models.delivery import RiderAssignment
from app.models.orders import Order, OrderStatusHistory
from app.services import events as ev
from app.services import emailing, jobs, offers as offer_svc

_COMMON: dict[str, set[str]] = {
    "payment_pending": {"payment_verified", "cancelled"},
    "placed": {"accepted", "rejected", "cancelled"},
    "payment_verified": {"accepted", "rejected", "cancelled"},
    "accepted": {"preparing", "cancelled"},
    "preparing": {"ready", "cancelled"},
    "ready": {"completed", "cancelled"},
    "cancelled": {"refund_pending"},
    "rejected": {"refund_pending"},
    "refund_pending": {"refunded"},
    "completed": set(),
    "refunded": set(),
}
_DELIVERY = {**_COMMON,
             "ready": {"rider_assigned", "cancelled"},
             "rider_assigned": {"picked_up", "ready", "cancelled"},
             "picked_up": {"out_for_delivery", "cancelled"},
             "out_for_delivery": {"delivered", "cancelled"},
             "delivered": set()}
TRANSITIONS = {"dine_in": _COMMON, "pickup": _COMMON, "walk_in": _COMMON, "delivery": _DELIVERY}
ACTIVE_STATUSES = ("placed", "payment_pending", "payment_verified", "accepted", "preparing", "ready", "rider_assigned", "picked_up", "out_for_delivery")
FINAL_OK = ("completed", "delivered")
CUSTOMER_CANCELLABLE = ("payment_pending", "placed", "payment_verified")

MESSAGES = {
    "accepted": ("Order accepted", "We have started working on order {n}."),
    "rejected": ("Order declined", "Sorry, order {n} could not be accepted. {r}"),
    "preparing": ("Preparing your order", "Order {n} is being prepared."),
    "ready": ("Order ready", "Order {n} is ready."),
    "rider_assigned": ("Rider assigned", "A rider has been assigned to order {n}."),
    "picked_up": ("Order picked up", "Your rider has picked up order {n}."),
    "out_for_delivery": ("Out for delivery", "Order {n} is on its way. Keep your delivery code handy."),
    "delivered": ("Delivered", "Order {n} was delivered. Enjoy!"),
    "completed": ("Order completed", "Thank you for visiting Crescent Cafe!"),
    "cancelled": ("Order cancelled", "Order {n} was cancelled. {r}"),
    "refund_pending": ("Refund initiated", "A refund for order {n} is being processed."),
    "refunded": ("Refund completed", "Your refund for order {n} has been processed."),
}


def customer_recipients(order: Order) -> list[dict]:
    return [{"role": "customer", "id": str(order.customer_id)}] if order.customer_id else []


def order_topics(order: Order, *, admin: bool = True) -> list[str]:
    t = [f"order:{order.id}"]
    if admin:
        t.append("admin")
    if order.customer_id:
        t.append(f"account:customer:{order.customer_id}")
    return t


async def lock_order(db: AsyncSession, order_id: uuid.UUID) -> Order:
    return (await db.execute(select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True))).scalar_one()


async def add_history(db: AsyncSession, order: Order, from_status: str | None, to: str, actor_role: str, actor_id: uuid.UUID | None,
                      reason: str | None = None, meta: dict | None = None) -> OrderStatusHistory:
    h = OrderStatusHistory(order_id=order.id, from_status=from_status, to_status=to, actor_role=actor_role, actor_id=actor_id,
                           reason=(reason or None) and reason[:300], meta=meta)
    db.add(h)
    await db.flush()
    return h


async def transition(db: AsyncSession, order: Order | uuid.UUID, to: str, *, actor_role: str, actor_id: uuid.UUID | None = None,
                     reason: str | None = None, meta: dict | None = None, _via_otp: bool = False, notify_customer: bool = True) -> Order:
    oid = order.id if isinstance(order, Order) else order
    order = await lock_order(db, oid)
    if to == "delivered" and not _via_otp:
        raise Forbidden("Delivery can only be completed by verifying the customer's OTP", code="otp_required")
    cur = order.status
    if cur == to:
        raise Conflict(f"Order is already {to}", code="already_in_status")
    if to not in TRANSITIONS[order.order_type].get(cur, set()):
        raise Conflict(f"An order cannot move from '{cur}' to '{to}'", code="invalid_transition",
                       details={"from": cur, "to": to, "allowed": sorted(TRANSITIONS[order.order_type].get(cur, set()))})
    if to in ("rejected", "cancelled") and not (reason and reason.strip()):
        raise Invalid("A reason is required", code="reason_required")
    now = now_utc()
    order.status = to
    if to in ("accepted", "rejected"):
        order.decided_at = now
    if to == "rejected":
        order.rejection_reason = reason[:300]
    if to == "cancelled":
        order.cancellation_reason = reason[:300]
    if to in FINAL_OK:
        order.completed_at = now
    h = await add_history(db, order, cur, to, actor_role, actor_id, reason, meta)

    if to in ("rejected", "cancelled"):
        await offer_svc.release_for_order(db, order.id)
        await _close_assignment(db, order, reason or to)
    if to in FINAL_OK:
        await jobs.enqueue(db, "generate_invoice", {"order_id": str(order.id)}, key=f"invoice:{order.id}")
        if order.table_session_id is None:
            pass

    title, body = MESSAGES.get(to, (None, None))
    if title:
        body = body.format(n=order.order_number, r=(reason or "")).strip()
    await ev.emit(db, type="order.status_changed", topics=order_topics(order), dedupe_key=f"order.status:{order.id}:{h.id}",
                  data={"order_id": order.id, "order_number": order.order_number, "status": to, "previous_status": cur,
                        "payment_status": order.payment_status, "reason": reason},
                  recipients=customer_recipients(order) if (notify_customer and title) else [], title=title, body=body, branch_id=order.branch_id)
    if to in ("accepted", "rejected"):
        await ev.emit(db, type="order.alarm_stop", topics=["admin"], dedupe_key=f"order.alarm_stop:{order.id}", recipients=[{"role": "admin"}],
                      data={"order_id": order.id, "decision": to, "alarm": "stop"}, high_priority=True, data_only=True, branch_id=order.branch_id)
    if to == "ready":  # tell the rider who already holds this delivery that the food can be collected
        act = (await db.execute(select(RiderAssignment).where(RiderAssignment.order_id == order.id,
                                                              RiderAssignment.status.in_(("offered", "accepted"))))).scalars().first()
        if act is not None:
            await ev.emit(db, type="delivery.order_ready", topics=[f"account:rider:{act.rider_id}"], dedupe_key=f"delivery.ready:{order.id}:{act.id}",
                          recipients=[{"role": "rider", "id": str(act.rider_id)}], title="Order ready for pickup",
                          body=f"Order {order.order_number} is ready for pickup.",
                          data={"assignment_id": act.id, "order_id": order.id, "order_number": order.order_number}, high_priority=True, branch_id=order.branch_id)
    if to == "cancelled" and actor_role == "customer":
        await ev.emit(db, type="order.cancelled_by_customer", topics=["admin"], dedupe_key=f"order.cancel_notice:{order.id}", recipients=[{"role": "admin"}],
                      title="Order cancelled by customer", body=f"Order {order.order_number}: {reason}", data={"order_id": order.id}, branch_id=order.branch_id)
        await ev.emit(db, type="order.alarm_stop", topics=["admin"], dedupe_key=f"order.alarm_stop:{order.id}", recipients=[{"role": "admin"}],
                      data={"order_id": order.id, "decision": "cancelled", "alarm": "stop"}, high_priority=True, data_only=True, branch_id=order.branch_id)
    email_template = {"payment_verified": "order_confirmation", "accepted": "order_accepted", "out_for_delivery": "order_on_the_way",
                      "delivered": "order_delivered", "rejected": "order_rejected"}.get(to)
    if email_template and order.source != "admin":
        await emailing.queue_email(db, email_template, order_id=order.id)
    if to in ("rejected", "cancelled"):
        from app.services import payments  # local import to avoid a cycle

        await payments.on_order_closed_unfulfilled(db, order, actor_role, actor_id)
    return order


async def _close_assignment(db: AsyncSession, order: Order, reason: str) -> None:
    rows = (await db.execute(select(RiderAssignment).where(RiderAssignment.order_id == order.id,
                                                          RiderAssignment.status.in_(("offered", "accepted", "picked_up"))))).scalars().all()
    for a in rows:
        a.status, a.responded_at = "cancelled", now_utc()
        await ev.emit(db, type="assignment.cancelled", topics=[f"account:rider:{a.rider_id}", "admin"], dedupe_key=f"assignment.cancel:{a.id}",
                      recipients=[{"role": "rider", "id": str(a.rider_id)}], title="Delivery cancelled",
                      body=f"Order {order.order_number} was cancelled.", data={"assignment_id": a.id, "order_id": order.id, "alarm": "stop"},
                      high_priority=True, data_only=True, branch_id=order.branch_id)


async def emit_new_order_alarm(db: AsyncSession, order: Order, notify: bool = True) -> None:
    """Pending-order event for the Admin app. Deduplicated per order so retries never create a second alarm."""
    data = {"order_id": order.id, "order_number": order.order_number, "order_type": order.order_type, "total_paise": order.total_paise,
            "table_label": order.table_label, "customer_name": order.customer_name, "payment_method": order.payment_method,
            "payment_status": order.payment_status, "alarm": "start"}
    await ev.emit(db, type="order.new", topics=["admin"], dedupe_key=f"order.new:{order.id}", recipients=[{"role": "admin"}] if notify else [],
                  title="New order", body=f"{order.order_number} · {order.order_type.replace('_', ' ')}", data=data,
                  high_priority=True, data_only=True, branch_id=order.branch_id)
