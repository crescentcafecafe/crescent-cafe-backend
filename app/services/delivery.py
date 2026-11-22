"""Manual rider assignment, rider workflow, delivery OTP and live tracking."""
from __future__ import annotations

import logging
import math
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, Conflict, Forbidden, Invalid, NotFound, TooMany
from app.core.security import consteq, hash_otp, new_numeric_otp, new_token, now_utc
from app.models.core import Rider
from app.models.delivery import ACTIVE_ASSIGNMENT, DeliveryOtp, RiderAssignment, RiderLocation
from app.integrations import registry
from app.integrations.base import GatewayError
from app.models.orders import Order
from app.models.payments import Payment
from app.services import events as ev
from app.services import order_state as st
from app.services import payments
from app.services import settings as cfg
from app.services.audit import audit

log = logging.getLogger(__name__)


def _rupees(paise: int) -> str:
    return f"₹{paise / 100:,.2f}".replace(".00", "")


async def _stop_rider_alarm(db: AsyncSession, a: RiderAssignment, why: str, branch_id) -> None:
    """Silences the new-delivery alarm on the rider's OTHER phones too (the phone that tapped Accept/Reject stops its own)."""
    await ev.emit(db, type="assignment.alarm_stop", topics=[f"account:rider:{a.rider_id}"], dedupe_key=f"assignment.alarm_stop:{a.id}:{why}",
                  recipients=[{"role": "rider", "id": str(a.rider_id)}], data={"assignment_id": a.id, "order_id": a.order_id, "alarm": "stop"},
                  high_priority=True, data_only=True, branch_id=branch_id)


async def _active_count(db: AsyncSession, rider_id: uuid.UUID, statuses: tuple[str, ...], exclude: uuid.UUID | None = None) -> int:
    q = select(func.count()).select_from(RiderAssignment).where(RiderAssignment.rider_id == rider_id, RiderAssignment.status.in_(statuses))
    if exclude:
        q = q.where(RiderAssignment.id != exclude)
    return (await db.execute(q)).scalar_one()


async def assign(db: AsyncSession, order_id: uuid.UUID, rider_id: uuid.UUID, admin_id: uuid.UUID) -> RiderAssignment:
    """Assignment is ALWAYS manual: only this admin-triggered path creates assignments."""
    order = await st.lock_order(db, order_id)
    if order.order_type != "delivery":
        raise Conflict("Only delivery orders can be assigned to a rider", code="not_delivery")
    if order.status != "ready":
        raise Conflict("The order must be 'ready' before a rider can be assigned", code="invalid_transition", details={"status": order.status})
    rider = await db.get(Rider, rider_id)
    if rider is None or rider.branch_id != order.branch_id:
        raise NotFound("Rider not found")
    if not rider.is_active:
        raise Conflict("Rider account is deactivated", code="rider_inactive")
    if not rider.is_available:
        raise Conflict("Rider is not available (off duty)", code="rider_unavailable")
    cs = await cfg.get_cafe_settings(db, order.branch_id)
    if cs.rider_single_active_assignment and await _active_count(db, rider.id, ACTIVE_ASSIGNMENT):
        raise Conflict("Rider already has an active delivery", code="rider_busy")
    a = RiderAssignment(order_id=order.id, rider_id=rider.id, assigned_by=admin_id)
    try:
        async with db.begin_nested():
            db.add(a)
            await db.flush()
    except IntegrityError:
        raise Conflict("This order already has an active assignment. Cancel it first.", code="already_assigned")
    await audit(db, actor_role="admin", actor_id=admin_id, action="delivery.assigned", entity_type="order", entity_id=order.id,
                after={"rider_id": str(rider.id)}, branch_id=order.branch_id)
    await ev.emit(db, type="assignment.offered", topics=[f"account:rider:{rider.id}", "admin"], dedupe_key=f"assignment.offered:{a.id}",
                  recipients=[{"role": "rider", "id": str(rider.id)}], title="New delivery assignment", body=f"Order {order.order_number}",
                  data={"assignment_id": a.id, "order_id": order.id, "order_number": order.order_number, "alarm": "start"},
                  high_priority=True, data_only=True, branch_id=order.branch_id)
    return a


async def admin_cancel_assignment(db: AsyncSession, assignment_id: uuid.UUID, admin_id: uuid.UUID, reason: str) -> RiderAssignment:
    a = (await db.execute(select(RiderAssignment).where(RiderAssignment.id == assignment_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if a is None:
        raise NotFound("Assignment not found")
    if a.status not in ("offered", "accepted"):
        raise Conflict("Only offered or accepted (not yet picked up) assignments can be cancelled", code="not_cancellable")
    order = await st.lock_order(db, a.order_id)
    a.status, a.responded_at = "cancelled", now_utc()
    if order.status == "rider_assigned":
        await st.transition(db, order, "ready", actor_role="admin", actor_id=admin_id, reason=f"assignment cancelled: {reason}"[:300], notify_customer=False)
    await ev.emit(db, type="assignment.cancelled", topics=[f"account:rider:{a.rider_id}", "admin"], dedupe_key=f"assignment.cancel:{a.id}",
                  recipients=[{"role": "rider", "id": str(a.rider_id)}], title="Delivery unassigned", body=f"Order {order.order_number} was unassigned.",
                  data={"assignment_id": a.id, "order_id": order.id, "alarm": "stop"}, high_priority=True, data_only=True, branch_id=order.branch_id)
    await audit(db, actor_role="admin", actor_id=admin_id, action="delivery.assignment_cancelled", entity_type="assignment", entity_id=a.id, after={"reason": reason})
    return a


async def _own_assignment(db: AsyncSession, assignment_id: uuid.UUID, rider_id: uuid.UUID) -> RiderAssignment:
    a = (await db.execute(select(RiderAssignment).where(RiderAssignment.id == assignment_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if a is None or a.rider_id != rider_id:
        raise NotFound("Assignment not found")  # never reveal other riders' assignments
    return a


async def rider_accept(db: AsyncSession, assignment_id: uuid.UUID, rider: Rider) -> RiderAssignment:
    a = await _own_assignment(db, assignment_id, rider.id)
    if a.status != "offered":
        raise Conflict(f"Assignment is already {a.status}", code="invalid_state")
    order = await st.lock_order(db, a.order_id)
    cs = await cfg.get_cafe_settings(db, order.branch_id)
    if cs.rider_single_active_assignment and await _active_count(db, rider.id, ("accepted", "picked_up"), exclude=a.id):
        raise Conflict("Finish your current delivery before accepting another", code="rider_busy")
    a.status, a.responded_at = "accepted", now_utc()
    await st.transition(db, order, "rider_assigned", actor_role="rider", actor_id=rider.id)
    await ev.emit(db, type="assignment.accepted", topics=["admin", f"order:{order.id}", f"account:rider:{rider.id}"], dedupe_key=f"assignment.accepted:{a.id}",
                  recipients=[{"role": "admin"}], title="Rider accepted", body=f"{rider.name} accepted {order.order_number}",
                  data={"assignment_id": a.id, "order_id": order.id, "rider_id": rider.id, "alarm": "stop"}, branch_id=order.branch_id)
    await _stop_rider_alarm(db, a, "accepted", order.branch_id)
    # Offers are only made for orders that are already ready, so say so once the rider has taken the job.
    await ev.emit(db, type="delivery.order_ready", topics=[f"account:rider:{rider.id}"], dedupe_key=f"delivery.ready:{order.id}:{a.id}",
                  recipients=[{"role": "rider", "id": str(rider.id)}], title="Order ready for pickup",
                  body=f"Order {order.order_number} is ready for pickup at the cafe.", data={"assignment_id": a.id, "order_id": order.id, "order_number": order.order_number},
                  high_priority=True, branch_id=order.branch_id)
    return a


async def rider_reject(db: AsyncSession, assignment_id: uuid.UUID, rider: Rider, reason: str) -> RiderAssignment:
    a = await _own_assignment(db, assignment_id, rider.id)
    if a.status != "offered":
        raise Conflict(f"Assignment is already {a.status}", code="invalid_state")
    a.status, a.responded_at, a.reject_reason = "rejected", now_utc(), reason[:300]
    order = await db.get(Order, a.order_id)
    await ev.emit(db, type="assignment.rejected", topics=["admin", f"account:rider:{rider.id}"], dedupe_key=f"assignment.rejected:{a.id}",
                  recipients=[{"role": "admin"}], title="Rider rejected delivery", body=f"{rider.name} rejected {order.order_number}: {reason}",
                  data={"assignment_id": a.id, "order_id": order.id, "rider_id": rider.id, "reason": reason, "alarm": "stop"},
                  high_priority=True, branch_id=order.branch_id)
    await _stop_rider_alarm(db, a, "rejected", order.branch_id)
    return a


async def rider_picked_up(db: AsyncSession, assignment_id: uuid.UUID, rider: Rider) -> RiderAssignment:
    a = await _own_assignment(db, assignment_id, rider.id)
    if a.status != "accepted":
        raise Conflict("Accept the delivery first (or it was already picked up)", code="invalid_state")
    a.status, a.picked_up_at = "picked_up", now_utc()
    await st.transition(db, a.order_id, "picked_up", actor_role="rider", actor_id=rider.id)
    return a


async def rider_out_for_delivery(db: AsyncSession, assignment_id: uuid.UUID, rider: Rider) -> tuple[RiderAssignment, str]:
    a = await _own_assignment(db, assignment_id, rider.id)
    if a.status != "picked_up":
        raise Conflict("Mark the order as picked up first", code="invalid_state")
    order = await st.transition(db, a.order_id, "out_for_delivery", actor_role="rider", actor_id=rider.id)
    otp = await issue_otp(db, order, a.id, notify=True)
    if order.payment_method == "cash" and order.payment_status not in ("paid", "partially_refunded"):
        await ev.emit(db, type="cash.collect_due", topics=[f"account:rider:{rider.id}"], dedupe_key=f"cash.due:{a.id}",
                      recipients=[{"role": "rider", "id": str(rider.id)}], title="Collect cash at the door",
                      body=f"Collect {_rupees(order.total_paise)} in cash for order {order.order_number}, then confirm it in the app.",
                      data={"assignment_id": a.id, "order_id": order.id, "order_number": order.order_number}, branch_id=order.branch_id)
    return a, otp


# ---------------------------------------------------------------- OTP
async def issue_otp(db: AsyncSession, order: Order, assignment_id: uuid.UUID | None, notify: bool) -> str:
    """Generate a new random OTP, invalidate any previous one, store ONLY a keyed hash. Plaintext is returned to the caller
    (customer-facing endpoint / push) and never persisted."""
    cs = await cfg.get_cafe_settings(db, order.branch_id)
    now = now_utc()
    old = (await db.execute(select(DeliveryOtp).where(DeliveryOtp.order_id == order.id, DeliveryOtp.verified_at.is_(None),
                                                      DeliveryOtp.invalidated_at.is_(None)).with_for_update())).scalars().all()
    for o in old:
        o.invalidated_at = now
    await db.flush()
    otp = new_numeric_otp(6)
    salt = new_token(8)
    db.add(DeliveryOtp(order_id=order.id, assignment_id=assignment_id, otp_hash=hash_otp(otp, salt), salt=salt, max_attempts=cs.otp_max_attempts,
                       expires_at=now + timedelta(minutes=cs.otp_ttl_minutes)))
    await db.flush()
    # WS/DB event deliberately carries NO otp. The code itself reaches the customer via a direct push (not persisted) and the
    # authenticated customer endpoint.
    await ev.emit(db, type="delivery.otp_issued", topics=st.order_topics(order, admin=False), dedupe_key=f"otp.issued:{order.id}:{now.timestamp()}",
                  recipients=[], data={"order_id": order.id, "expires_at": now + timedelta(minutes=cs.otp_ttl_minutes)}, push=False, branch_id=order.branch_id)
    if notify and order.customer_id:
        from app.db.session import add_post_commit

        cid, num = order.customer_id, order.order_number

        async def _push() -> None:
            from app.services.push import send_direct

            await send_direct("customer", cid, title="Your delivery code", body=f"Share code {otp} with the rider only on delivery of order {num}.",
                              data={"type": "delivery.otp", "order_id": str(order.id)}, high_priority=True)

        add_post_commit(db, _push)
    return otp


async def customer_reveal_otp(db: AsyncSession, order: Order) -> dict:
    if order.order_type != "delivery" or order.status != "out_for_delivery":
        raise Conflict("A delivery code is available only while your order is out for delivery", code="otp_not_available")
    a = (await db.execute(select(RiderAssignment).where(RiderAssignment.order_id == order.id, RiderAssignment.status == "picked_up"))).scalar_one_or_none()
    otp = await issue_otp(db, order, a.id if a else None, notify=False)
    return {"otp": otp, "valid_minutes": (await cfg.get_cafe_settings(db, order.branch_id)).otp_ttl_minutes}


async def verify_otp_and_deliver(db: AsyncSession, assignment_id: uuid.UUID, rider: Rider, otp: str) -> Order:
    a = await _own_assignment(db, assignment_id, rider.id)
    if a.status != "picked_up":
        raise Conflict("This delivery is not in progress", code="invalid_state")
    order = await st.lock_order(db, a.order_id)  # serialises concurrent attempts on the same order
    if order.status != "out_for_delivery":
        raise Conflict(f"Order is {order.status}; mark it 'out for delivery' first" if order.status == "picked_up" else f"Order is {order.status}", code="invalid_state")
    if get_settings().require_payment_before_delivery and order.payment_status not in ("paid", "partially_refunded"):
        # checked before the OTP so a premature attempt neither uses up a code try nor completes an unpaid delivery
        raise Conflict("Collect the payment before completing the delivery", code="payment_not_collected", details={"amount_due_paise": order.total_paise})
    rec = (await db.execute(select(DeliveryOtp).where(DeliveryOtp.order_id == order.id, DeliveryOtp.verified_at.is_(None),
                                                      DeliveryOtp.invalidated_at.is_(None)).with_for_update())).scalar_one_or_none()
    if rec is None:
        raise Conflict("No active delivery code. Ask the customer to refresh their code.", code="otp_missing")
    if rec.expires_at < now_utc():
        raise Conflict("The delivery code has expired. The customer must generate a new one.", code="otp_expired", status_code=410)
    if rec.attempts >= rec.max_attempts:
        raise TooMany("Too many wrong attempts. The customer must generate a new code.", code="otp_locked", status_code=423)
    if not consteq(hash_otp(otp, rec.salt), rec.otp_hash):
        rec.attempts += 1
        left = rec.max_attempts - rec.attempts
        await db.commit()  # persist the failed attempt even though we raise
        raise Invalid("Incorrect delivery code", code="otp_incorrect", details={"attempts_left": max(left, 0)})
    now = now_utc()
    rec.verified_at = now
    a.status, a.completed_at = "completed", now
    order = await st.transition(db, order, "delivered", actor_role="rider", actor_id=rider.id, _via_otp=True, meta={"otp_verified": True})
    await ev.emit(db, type="delivery.completed", topics=["admin", f"order:{order.id}"], dedupe_key=f"delivery.done:{order.id}", recipients=[{"role": "admin"}],
                  title="Delivery completed", body=f"{rider.name} delivered {order.order_number}", data={"order_id": order.id, "rider_id": rider.id}, branch_id=order.branch_id)
    await ev.emit(db, type="delivery.rider_done", topics=[f"account:rider:{rider.id}"], dedupe_key=f"delivery.rider_done:{order.id}",
                  recipients=[{"role": "rider", "id": str(rider.id)}], title="Delivery completed",
                  body=f"Order {order.order_number} was delivered successfully. Thank you!", data={"order_id": order.id, "assignment_id": a.id}, branch_id=order.branch_id)
    return order


# ---------------------------------------------------------------- tracking
def _valid_coord(lat: float, lng: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lng <= 180 and not (lat == 0 and lng == 0)


async def post_location(db: AsyncSession, rider: Rider, *, assignment_id: uuid.UUID, lat: float, lng: float, accuracy: float | None, recorded_at) -> RiderLocation:
    if not _valid_coord(lat, lng):
        raise Invalid("Invalid coordinates", code="invalid_coordinates")
    if accuracy is not None and not (0 <= accuracy <= 10000):
        raise Invalid("Invalid accuracy", code="invalid_accuracy")
    a = await db.get(RiderAssignment, assignment_id)
    if a is None or a.rider_id != rider.id:
        raise NotFound("Assignment not found")
    if a.status not in ("accepted", "picked_up"):
        raise Conflict("Location tracking is only accepted during an active delivery", code="tracking_inactive")
    order = await db.get(Order, a.order_id)
    if order.status not in ("rider_assigned", "picked_up", "out_for_delivery"):
        raise Conflict("Delivery is not active", code="tracking_inactive")
    cs = await cfg.get_cafe_settings(db, order.branch_id)
    now = now_utc()
    last = (await db.execute(select(func.max(RiderLocation.received_at)).where(RiderLocation.assignment_id == a.id))).scalar_one()
    if last and (now - last).total_seconds() < cs.location_min_interval_seconds:
        raise TooMany(f"Send location at most every {cs.location_min_interval_seconds}s", code="location_too_frequent")
    rec_at = recorded_at if (recorded_at and now - timedelta(minutes=5) <= recorded_at <= now + timedelta(seconds=60)) else now
    loc = RiderLocation(rider_id=rider.id, assignment_id=a.id, order_id=order.id, latitude=lat, longitude=lng, accuracy_m=accuracy, recorded_at=rec_at)
    db.add(loc)
    await db.flush()
    payload = {"order_id": order.id, "latitude": lat, "longitude": lng, "accuracy_m": accuracy, "recorded_at": rec_at}
    ev.publish_ephemeral(db, type="tracking.location", topics=[f"tracking:{order.id}", "admin"], data=payload)
    await _maybe_notify_arrival(db, a, order, lat, lng, accuracy)
    return loc


async def latest_location(db: AsyncSession, order: Order) -> dict:
    cs = await cfg.get_cafe_settings(db, order.branch_id)
    active = order.status in ("rider_assigned", "picked_up", "out_for_delivery")
    base = {"order_id": order.id, "tracking_active": active, "update_interval_seconds": cs.location_update_interval_seconds, "location": None}
    if not active:
        return base
    loc = (await db.execute(select(RiderLocation).where(RiderLocation.order_id == order.id).order_by(RiderLocation.id.desc()).limit(1))).scalar_one_or_none()
    if loc is None:
        return base
    age = (now_utc() - loc.recorded_at).total_seconds()
    base["location"] = {"latitude": loc.latitude, "longitude": loc.longitude, "accuracy_m": loc.accuracy_m, "recorded_at": loc.recorded_at,
                        "age_seconds": int(age), "is_stale": age > cs.location_stale_after_seconds}  # stale => last known, not live
    return base


# ---------------------------------------------------------------- "rider is nearby" alert
def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def _maybe_notify_arrival(db: AsyncSession, a: RiderAssignment, order: Order, lat: float, lng: float, accuracy: float | None) -> bool:
    """Calculated on the server from the customer's pinned delivery location. Sent once per assignment, only on the final leg."""
    if a.arrival_notified_at is not None or order.status != "out_for_delivery":
        return False
    addr = order.delivery_address or {}
    dlat, dlng = addr.get("latitude"), addr.get("longitude")
    if dlat is None or dlng is None:
        return False
    threshold = get_settings().arrival_notify_distance_m
    if accuracy is not None and accuracy > max(threshold * 3, 50):
        return False  # too imprecise to claim "nearby"
    if haversine_m(lat, lng, float(dlat), float(dlng)) > threshold:
        return False
    a.arrival_notified_at = now_utc()
    await ev.emit(db, type="delivery.rider_nearby", topics=st.order_topics(order, admin=False), dedupe_key=f"delivery.nearby:{a.id}",
                  recipients=st.customer_recipients(order), title="Your rider is nearby!", body="Your Crescent Cafe delivery rider is nearby!",
                  data={"order_id": order.id, "order_number": order.order_number}, high_priority=True, branch_id=order.branch_id)
    return True


# ---------------------------------------------------------------- collecting payment at the door
async def _payable_order(db: AsyncSession, assignment_id: uuid.UUID, rider: Rider) -> tuple[RiderAssignment, Order]:
    a = await _own_assignment(db, assignment_id, rider.id)
    if a.status != "picked_up":
        raise Conflict("This delivery is not in progress", code="invalid_state")
    order = await st.lock_order(db, a.order_id)
    if order.status != "out_for_delivery":
        raise Conflict(f"Start the final delivery first (order is {order.status})", code="invalid_state")
    return a, order


async def rider_collect_cash(db: AsyncSession, assignment_id: uuid.UUID, rider: Rider, amount_received_paise: int) -> dict:
    _a, order = await _payable_order(db, assignment_id, rider)
    cash = (await db.execute(select(Payment).where(Payment.order_id == order.id, Payment.method == "cash").order_by(Payment.created_at.desc()).limit(1))).scalar_one_or_none()
    if cash is None:
        raise Invalid("This is not a cash order", code="not_cash")
    pay, already = await payments.record_rider_cash(db, cash, rider, amount_received_paise)
    if not already:
        await ev.emit(db, type="cash.collected", topics=[f"account:rider:{rider.id}"], dedupe_key=f"cash.collected:{pay.id}",
                      recipients=[{"role": "rider", "id": str(rider.id)}], title="Cash recorded",
                      body=f"{_rupees(pay.amount_paise)} for order {order.order_number} is recorded. Hand it over to the cafe.",
                      data={"order_id": order.id, "order_number": order.order_number, "amount_paise": pay.amount_paise}, branch_id=order.branch_id)
    return {"payment_id": pay.id, "payment_status": "paid", "amount_paise": pay.amount_paise, "tendered_paise": pay.tendered_paise,
            "change_due_paise": pay.change_paise, "already_recorded": already}


def _qr_view(pay: Payment) -> dict:
    qr = (pay.last_gateway_payload or {}).get("qr") or {}
    return {"payment_id": pay.id, "gateway": pay.gateway, "status": pay.status, "amount_paise": pay.amount_paise,
            "qr_image_url": qr.get("image_url"), "expires_at": pay.expires_at}


async def rider_start_qr(db: AsyncSession, assignment_id: uuid.UUID, rider: Rider) -> dict:
    """Creates (or re-uses) a single-use Razorpay UPI QR for the full amount. Only a verified webhook / server fetch can mark it paid."""
    _a, order = await _payable_order(db, assignment_id, rider)
    if order.payment_status in ("paid", "partially_refunded"):
        raise Conflict("This order is already paid", code="already_paid")
    cs = await cfg.get_cafe_settings(db, order.branch_id)
    if "razorpay" not in cfg.available_gateways(cs):
        raise Invalid("Online QR payment is not available right now", code="qr_unavailable")
    now = now_utc()
    existing = (await db.execute(select(Payment).where(Payment.order_id == order.id, Payment.collection_channel == "rider_upi_qr",
                                                       Payment.status.in_(("created", "pending")), Payment.expires_at > now)
                                 .order_by(Payment.created_at.desc()).limit(1))).scalar_one_or_none()
    if existing is not None and existing.gateway_order_id:
        return _qr_view(existing)
    pay = Payment(order_id=order.id, branch_id=order.branch_id, method="online", gateway="razorpay", amount_paise=order.total_paise, currency=order.currency,
                  status="created", expires_at=now + timedelta(minutes=max(3, get_settings().rider_qr_minutes)), collection_channel="rider_upi_qr",
                  collected_by_rider_id=rider.id)
    db.add(pay)
    await db.flush()
    try:
        qr = await registry.get_gateway("razorpay").create_qr(reference=pay.id.hex, amount_paise=order.total_paise,
                                                              description=f"Crescent Cafe order {order.order_number}", expires_at_epoch=int(pay.expires_at.timestamp()))
    except GatewayError as e:
        log.warning("QR creation failed: %s", e)
        raise AppError("Could not create the QR code right now. Try again, or take cash.", code="qr_unavailable", status_code=502)
    pay.gateway_order_id, pay.status = qr.gateway_qr_id, "pending"
    pay.last_gateway_payload = {"qr": {"id": qr.gateway_qr_id, "image_url": qr.image_url}}
    await audit(db, actor_role="rider", actor_id=rider.id, action="payment.qr_created", entity_type="payment", entity_id=pay.id,
                after={"order_id": str(order.id), "amount_paise": pay.amount_paise}, branch_id=order.branch_id)
    return _qr_view(pay)


async def collection_status(db: AsyncSession, assignment_id: uuid.UUID, rider: Rider) -> dict:
    """What the rider's screen polls. If a QR is open we ask the gateway ourselves, so a missed webhook cannot leave the rider stuck."""
    a = await db.get(RiderAssignment, assignment_id)
    if a is None or a.rider_id != rider.id:
        raise NotFound("Assignment not found")
    order = await db.get(Order, a.order_id)
    qr = (await db.execute(select(Payment).where(Payment.order_id == order.id, Payment.collection_channel == "rider_upi_qr")
                           .order_by(Payment.created_at.desc()).limit(1))).scalar_one_or_none()
    if qr is not None and qr.status in ("created", "pending") and qr.gateway_order_id and order.payment_status not in ("paid", "partially_refunded"):
        try:
            await payments.sync_payment(db, qr, "rider_poll")
            await db.refresh(order)
        except AppError:
            pass  # gateway briefly unreachable: report what we know
    paid = order.payment_status in ("paid", "partially_refunded")
    return {"order_id": order.id, "payment_method": order.payment_method, "payment_status": order.payment_status,
            "amount_due_paise": 0 if paid else order.total_paise, "qr": _qr_view(qr) if qr is not None and not paid and qr.status in ("created", "pending") else None}
