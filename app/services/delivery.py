"""Manual rider assignment, rider workflow, delivery OTP and live tracking."""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Forbidden, Invalid, NotFound, TooMany
from app.core.security import consteq, hash_otp, new_numeric_otp, new_token, now_utc
from app.models.core import Rider
from app.models.delivery import ACTIVE_ASSIGNMENT, DeliveryOtp, RiderAssignment, RiderLocation
from app.models.orders import Order
from app.services import events as ev
from app.services import order_state as st
from app.services import settings as cfg
from app.services.audit import audit

log = logging.getLogger(__name__)


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
