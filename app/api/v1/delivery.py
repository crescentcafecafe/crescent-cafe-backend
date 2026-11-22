"""Rider self-service (/riders), delivery workflow (/deliveries) and tracking (/tracking)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.core.permissions import Principal, current_admin, current_rider, guest_token_header, optional_principal
from app.core.ratelimit import limit, limit_key
from app.db.session import get_db
from app.models.core import Rider
from app.models.delivery import ACTIVE_ASSIGNMENT, RiderAssignment, RiderLocation
from app.models.orders import Order
from app.schemas.admin import AvailabilityToggle, CollectCashIn, LocationIn, OtpVerifyIn, RejectAssignment
from app.services import delivery as svc, orders as order_svc

riders_router = APIRouter(prefix="/riders", tags=["riders"])
deliveries_router = APIRouter(prefix="/deliveries", tags=["deliveries"])
tracking_router = APIRouter(prefix="/tracking", tags=["tracking"])


def _rider_me(r: Rider) -> dict:
    return {"id": r.id, "name": r.name, "phone": r.phone, "vehicle_info": r.vehicle_info, "is_available": r.is_available, "must_change_password": r.must_change_password}


@riders_router.get("/me")
async def me(p: Principal = Depends(current_rider)):
    return _rider_me(p.user)


@riders_router.patch("/me/availability")
async def availability(body: AvailabilityToggle, p: Principal = Depends(current_rider), db: AsyncSession = Depends(get_db)):
    r: Rider = p.user
    if not body.available:
        n = (await db.execute(select(RiderAssignment).where(RiderAssignment.rider_id == r.id, RiderAssignment.status.in_(ACTIVE_ASSIGNMENT)).limit(1))).scalar_one_or_none()
        if n:
            raise Conflict("Finish or hand back your active delivery before going off duty", code="rider_busy")
    r.is_available = body.available
    return _rider_me(r)


async def _assignment_view(db: AsyncSession, a: RiderAssignment) -> dict:
    order = await db.get(Order, a.order_id)
    view = await order_svc.order_view(db, order, "rider")
    return {"assignment_id": a.id, "status": a.status, "offered_at": a.offered_at, "responded_at": a.responded_at, "order": view}


@deliveries_router.get("/mine")
async def mine(active: bool = True, p: Principal = Depends(current_rider), db: AsyncSession = Depends(get_db)):
    q = select(RiderAssignment).where(RiderAssignment.rider_id == p.id)
    q = q.where(RiderAssignment.status.in_(ACTIVE_ASSIGNMENT)) if active else q.where(RiderAssignment.status.in_(("completed", "rejected", "cancelled")))
    rows = (await db.execute(q.order_by(RiderAssignment.offered_at.desc()).limit(100))).scalars().all()
    return [await _assignment_view(db, a) for a in rows]


@deliveries_router.get("/{assignment_id}")
async def one(assignment_id: uuid.UUID, p: Principal = Depends(current_rider), db: AsyncSession = Depends(get_db)):
    a = await db.get(RiderAssignment, assignment_id)
    if a is None or a.rider_id != p.id:
        raise NotFound("Assignment not found")
    return await _assignment_view(db, a)


@deliveries_router.post("/{assignment_id}/accept")
async def accept(assignment_id: uuid.UUID, p: Principal = Depends(current_rider), db: AsyncSession = Depends(get_db)):
    return await _assignment_view(db, await svc.rider_accept(db, assignment_id, p.user))


@deliveries_router.post("/{assignment_id}/reject")
async def reject(assignment_id: uuid.UUID, body: RejectAssignment, p: Principal = Depends(current_rider), db: AsyncSession = Depends(get_db)):
    return await _assignment_view(db, await svc.rider_reject(db, assignment_id, p.user, body.reason))


@deliveries_router.post("/{assignment_id}/picked-up")
async def picked_up(assignment_id: uuid.UUID, p: Principal = Depends(current_rider), db: AsyncSession = Depends(get_db)):
    return await _assignment_view(db, await svc.rider_picked_up(db, assignment_id, p.user))


@deliveries_router.post("/{assignment_id}/out-for-delivery")
async def out_for_delivery(assignment_id: uuid.UUID, p: Principal = Depends(current_rider), db: AsyncSession = Depends(get_db)):
    """Starts the final leg and sends the delivery OTP to the customer (the rider never sees the code)."""
    a, _otp = await svc.rider_out_for_delivery(db, assignment_id, p.user)
    return await _assignment_view(db, a)


@deliveries_router.post("/{assignment_id}/verify-otp", dependencies=[Depends(limit("otp-verify", 20, 60))])
async def verify_otp(assignment_id: uuid.UUID, body: OtpVerifyIn, p: Principal = Depends(current_rider), db: AsyncSession = Depends(get_db)):
    """The ONLY way to complete a delivery. Wrong codes are counted; the code expires and locks after too many attempts."""
    await limit_key("otp-rider", str(p.id), 15, 300)
    order = await svc.verify_otp_and_deliver(db, assignment_id, p.user, body.otp)
    return {"order_id": order.id, "status": order.status, "delivered_at": order.completed_at}


@deliveries_router.post("/{assignment_id}/collect/cash", dependencies=[Depends(limit("collect", 30, 60))])
async def collect_cash(assignment_id: uuid.UUID, body: CollectCashIn, p: Principal = Depends(current_rider), db: AsyncSession = Depends(get_db)):
    """Rider confirms the customer handed over the cash. Safe to repeat: a second call returns the same result."""
    return await svc.rider_collect_cash(db, assignment_id, p.user, body.amount_received_paise)


@deliveries_router.post("/{assignment_id}/collect/upi-qr", dependencies=[Depends(limit("collect", 30, 60))])
async def collect_qr(assignment_id: uuid.UUID, p: Principal = Depends(current_rider), db: AsyncSession = Depends(get_db)):
    """Creates (or returns the open) single-use payment QR for this order. Payment is confirmed only by the gateway, never by the scan."""
    return await svc.rider_start_qr(db, assignment_id, p.user)


@deliveries_router.get("/{assignment_id}/collect/status", dependencies=[Depends(limit("collect-status", 90, 60))])
async def collect_status(assignment_id: uuid.UUID, p: Principal = Depends(current_rider), db: AsyncSession = Depends(get_db)):
    return await svc.collection_status(db, assignment_id, p.user)


@deliveries_router.post("/orders/{order_id}/otp", dependencies=[Depends(limit("otp-reveal", 10, 600))])
async def reveal_otp(order_id: uuid.UUID, p: Principal | None = Depends(optional_principal), guest_token: str | None = Depends(guest_token_header),
                     db: AsyncSession = Depends(get_db)):
    """Customer / guest asks for a (new) delivery code. Generating a new one invalidates the previous code."""
    order, role = await order_svc.authorize_order(db, order_id, p, guest_token)
    if role not in ("customer", "guest"):
        raise NotFound("Order not found")
    return await svc.customer_reveal_otp(db, order)


@tracking_router.post("/location", dependencies=[Depends(limit("loc", 120, 60))])
async def post_location(body: LocationIn, p: Principal = Depends(current_rider), db: AsyncSession = Depends(get_db)):
    loc = await svc.post_location(db, p.user, assignment_id=body.assignment_id, lat=body.latitude, lng=body.longitude, accuracy=body.accuracy_m,
                                  recorded_at=body.recorded_at)
    return {"id": loc.id, "recorded_at": loc.recorded_at}


@tracking_router.get("/orders/{order_id}")
async def track(order_id: uuid.UUID, p: Principal | None = Depends(optional_principal), guest_token: str | None = Depends(guest_token_header),
                db: AsyncSession = Depends(get_db)):
    """Customer (own order), guest (token) or admin. Returns last known location + staleness flag; null once delivery ended."""
    order, role = await order_svc.authorize_order(db, order_id, p, guest_token)
    if role == "rider":
        raise NotFound("Order not found")
    return await svc.latest_location(db, order)


@tracking_router.get("/orders/{order_id}/history")
async def history(order_id: uuid.UUID, limit_: int = 200, _: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(RiderLocation).where(RiderLocation.order_id == order_id).order_by(RiderLocation.id.desc()).limit(max(1, min(limit_, 1000))))).scalars().all()
    return [{"latitude": r.latitude, "longitude": r.longitude, "accuracy_m": r.accuracy_m, "recorded_at": r.recorded_at} for r in rows]


@tracking_router.get("/active")
async def active_locations(_: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Latest location of every active delivery (admin live map)."""
    rows = (await db.execute(select(RiderAssignment).where(RiderAssignment.status.in_(("accepted", "picked_up"))))).scalars().all()
    out = []
    for a in rows:
        order = await db.get(Order, a.order_id)
        rider = await db.get(Rider, a.rider_id)
        info = await svc.latest_location(db, order)
        out.append({"assignment_id": a.id, "order_id": order.id, "order_number": order.order_number, "order_status": order.status,
                    "rider": {"id": rider.id, "name": rider.name, "phone": rider.phone}, "tracking": info})
    return out
