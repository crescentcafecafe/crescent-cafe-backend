import asyncio
import re
import uuid
from datetime import timedelta

import pytest

from tests.conftest import IS_PG


def ready_delivery(cafe, h=None, name="Cust", phone="9876543210"):
    h = h or cafe.customer(name, phone=phone)
    cafe.post("/notifications/devices", {"install_id": f"cust-{uuid.uuid4().hex[:6]}", "fcm_token": "C" * 30 + uuid.uuid4().hex}, h=h)
    o = cafe.place("delivery", h=h).json()["order"]
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.advance(o["id"], "preparing", "ready")
    return o, h


def go_out(cafe, aid, rh):
    assert cafe.post(f"/deliveries/{aid}/accept", h=rh).status_code == 200
    assert cafe.post(f"/deliveries/{aid}/picked-up", h=rh).status_code == 200
    r = cafe.post(f"/deliveries/{aid}/out-for-delivery", h=rh)
    assert r.status_code == 200, r.text
    return r


def customer_otp(cafe, oid, h):
    r = cafe.post(f"/deliveries/orders/{oid}/otp", h=h)
    assert r.status_code == 200, r.text
    return r.json()["otp"]


def detail(cafe, oid):
    return cafe.get(f"/admin/orders/{oid}", h=cafe.admin).json()


def test_full_manual_delivery_flow_with_otp(cafe):
    rid, rh = cafe.rider()
    o, ch = ready_delivery(cafe)
    cafe.post("/notifications/devices", {"install_id": "rider-phone", "fcm_token": "R" * 40}, h=rh)
    assign = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin)
    assert assign.status_code == 200 and assign.json()["status"] == "offered"
    aid = assign.json()["assignment_id"]
    cafe.run_jobs()
    offer = [m for m in cafe.push.sent if m["data"]["type"] == "assignment.offered"]
    assert len(offer) == 1 and offer[0]["high_priority"] and offer[0]["data_only"] and offer[0]["data"]["alarm"] == "start"
    assert detail(cafe, o["id"])["status"] == "ready"  # offering alone doesn't change the order
    go_out(cafe, aid, rh)
    assert detail(cafe, o["id"])["status"] == "out_for_delivery"
    # the code is pushed to the CUSTOMER's device; the rider API never contains it
    pushed = [m for m in cafe.push.sent if m["data"]["type"] == "delivery.otp"]
    assert len(pushed) == 1
    otp = re.search(r"\b(\d{6})\b", pushed[0]["body"]).group(1)
    assert "otp" not in str(cafe.get("/deliveries/mine", h=rh).json()).lower().replace("verify-otp", "")
    wrong = "000000" if otp != "000000" else "111111"
    bad = cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": wrong}, h=rh)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "otp_incorrect" and bad.json()["error"]["details"]["attempts_left"] == 4
    assert detail(cafe, o["id"])["status"] == "out_for_delivery"
    ok = cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": otp}, h=rh)
    assert ok.status_code == 200 and ok.json()["status"] == "delivered"
    d = detail(cafe, o["id"])
    assert d["status"] == "delivered" and [h["to"] for h in d["history"]][-4:] == ["rider_assigned", "picked_up", "out_for_delivery", "delivered"]
    # OTP is single use
    again = cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": otp}, h=rh)
    assert again.status_code == 409
    cafe.run_jobs()
    assert [m for m in cafe.push.sent if m["data"]["type"] == "order.status_changed" and m["data"]["status"] == "delivered"]
    inv = cafe.get(f"/invoices/order/{o['id']}", h=ch)
    assert inv.status_code == 200 and inv.json()["invoice_number"].startswith("CC-")


def test_otp_stored_only_as_hash(cafe):
    rid, rh = cafe.rider()
    o, ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    go_out(cafe, aid, rh)
    otp = customer_otp(cafe, o["id"], ch)
    from sqlalchemy import select
    from app.db.session import session_scope
    from app.models.delivery import DeliveryOtp
    from app.models.notifications import NotificationEvent

    async def dump():
        async with session_scope() as db:
            rows = (await db.execute(select(DeliveryOtp))).scalars().all()
            evs = (await db.execute(select(NotificationEvent))).scalars().all()
            return [str(r.__dict__) for r in rows], [str(e.data) + str(e.body) for e in evs]
    otps, evs = cafe.call(dump)
    assert not any(otp in s for s in otps) and not any(otp in s for s in evs)
    assert sum(1 for s in otps if "invalidated_at" in s) == len(otps)


def test_no_rider_endpoint_can_mark_delivered_without_otp(cafe):
    spec = cafe.client.get("/openapi.json").json()["paths"]
    mutating = {p: v for p, v in spec.items() if p.startswith("/api/v1/deliveries") or p.startswith("/api/v1/riders")}
    assert not [p for p in mutating if "deliver" in p.split("/")[-1] and "out-for-delivery" not in p]
    rid, rh = cafe.rider()
    o, ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    go_out(cafe, aid, rh)
    for path in (f"/deliveries/{aid}/delivered", f"/deliveries/{aid}/complete", f"/deliveries/{aid}/mark-delivered"):
        assert cafe.post(path, {}, h=rh).status_code in (404, 405)
    assert cafe.post(f"/admin/orders/{o['id']}/status", {"status": "completed"}, h=cafe.admin).json()["error"]["code"] == "otp_required"


def test_otp_attempt_limit_then_regenerate(cafe):
    rid, rh = cafe.rider()
    o, ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    go_out(cafe, aid, rh)
    first = customer_otp(cafe, o["id"], ch)
    wrong = "123456" if first != "123456" else "654321"
    for _ in range(5):
        assert cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": wrong}, h=rh).status_code == 422
    locked = cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": first}, h=rh)
    assert locked.status_code == 423 and locked.json()["error"]["code"] == "otp_locked"  # even the right code is refused once locked
    second = customer_otp(cafe, o["id"], ch)  # secure regeneration invalidates the previous code
    assert cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": second}, h=rh).json()["status"] == "delivered"


def test_otp_expiry(cafe):
    rid, rh = cafe.rider()
    o, ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    go_out(cafe, aid, rh)
    otp = customer_otp(cafe, o["id"], ch)
    from sqlalchemy import update
    from app.core.security import now_utc
    from app.db.session import session_scope
    from app.models.delivery import DeliveryOtp

    async def expire():
        async with session_scope() as db:
            await db.execute(update(DeliveryOtp).values(expires_at=now_utc() - timedelta(minutes=1)))
    cafe.call(expire)
    r = cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": otp}, h=rh)
    assert r.status_code == 410 and r.json()["error"]["code"] == "otp_expired"
    assert detail(cafe, o["id"])["status"] == "out_for_delivery"
    fresh = customer_otp(cafe, o["id"], ch)
    assert cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": fresh}, h=rh).status_code == 200


def test_otp_reveal_only_for_owner_and_right_state(cafe):
    rid, rh = cafe.rider()
    o, ch = ready_delivery(cafe)
    assert cafe.post(f"/deliveries/orders/{o['id']}/otp", h=ch).json()["error"]["code"] == "otp_not_available"
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    go_out(cafe, aid, rh)
    assert cafe.post(f"/deliveries/orders/{o['id']}/otp", h=rh).status_code == 404       # rider can never fetch it
    assert cafe.post(f"/deliveries/orders/{o['id']}/otp", h=cafe.admin).status_code == 404  # nor can the admin
    assert cafe.post(f"/deliveries/orders/{o['id']}/otp", h=cafe.customer("X", phone="9111111111")).status_code == 404
    assert cafe.post(f"/admin/orders/{o['id']}/delivery-otp/resend", h=cafe.admin).status_code == 200


def test_guest_gets_otp_with_token(cafe):
    rid, rh = cafe.rider()
    r = cafe.place("delivery", customer_name="Guest", customer_phone="9800099999").json()
    o, gt = r["order"], {"X-Guest-Token": r["guest_token"]}
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.advance(o["id"], "preparing", "ready")
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    go_out(cafe, aid, rh)
    otp = customer_otp(cafe, o["id"], gt)
    assert cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": otp}, h=rh).json()["status"] == "delivered"


@pytest.mark.skipif(not IS_PG, reason="row-level locking needs PostgreSQL")
def test_concurrent_otp_attempts_complete_delivery_once(cafe):
    rid, rh = cafe.rider()
    o, ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    go_out(cafe, aid, rh)
    otp = customer_otp(cafe, o["id"], ch)
    from app.db.session import session_scope
    from app.models.core import Rider
    from app.services import delivery as svc

    async def attempt():
        async with session_scope() as db:
            r = await db.get(Rider, uuid.UUID(rid))
            return await svc.verify_otp_and_deliver(db, uuid.UUID(aid), r, otp)

    async def both():
        return await asyncio.gather(attempt(), attempt(), attempt(), return_exceptions=True)
    res = cafe.call(both)
    assert sum(1 for x in res if not isinstance(x, Exception)) == 1
    assert detail(cafe, o["id"])["status"] == "delivered"
    assert [h["to"] for h in detail(cafe, o["id"])["history"]].count("delivered") == 1


def test_rider_rejects_then_admin_reassigns(cafe):
    r1, h1 = cafe.rider("R1", "9000000001")
    r2, h2 = cafe.rider("R2", "9000000002")
    o, ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": r1}, h=cafe.admin).json()["assignment_id"]
    assert cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": r2}, h=cafe.admin).json()["error"]["code"] == "already_assigned"
    assert cafe.post(f"/deliveries/{aid}/reject", {"reason": "bike broke down"}, h=h2).status_code == 404  # not R2's assignment
    assert cafe.post(f"/deliveries/{aid}/accept", h=h2).status_code == 404
    assert cafe.post(f"/deliveries/{aid}/reject", {"reason": "bike broke down"}, h=h1).json()["status"] == "rejected"
    assert detail(cafe, o["id"])["status"] == "ready"
    cafe.run_jobs()
    assert cafe.get("/deliveries/mine", h=h1).json() == []
    rejected = cafe.get("/admin/deliveries?state=done", h=cafe.admin).json()
    assert rejected[0]["status"] == "rejected" and rejected[0]["reject_reason"] == "bike broke down"
    aid2 = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": r2}, h=cafe.admin).json()["assignment_id"]
    assert cafe.post(f"/deliveries/{aid2}/accept", h=h2).status_code == 200
    assert cafe.post(f"/deliveries/{aid}/accept", h=h1).status_code == 409  # old offer is dead
    from sqlalchemy import select
    from app.db.session import session_scope
    from app.models.notifications import NotificationEvent

    async def evs():
        async with session_scope() as db:
            return [e.type for e in (await db.execute(select(NotificationEvent))).scalars().all()]
    assert "assignment.rejected" in cafe.call(evs)


def test_admin_unassign_returns_order_to_ready(cafe):
    r1, h1 = cafe.rider()
    o, ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": r1}, h=cafe.admin).json()["assignment_id"]
    cafe.post(f"/deliveries/{aid}/accept", h=h1)
    assert detail(cafe, o["id"])["status"] == "rider_assigned"
    assert cafe.post(f"/admin/assignments/{aid}/cancel", {"reason": "rider unwell"}, h=cafe.admin).status_code == 200
    assert detail(cafe, o["id"])["status"] == "ready"
    assert cafe.get("/deliveries/mine", h=h1).json() == []


def test_assignment_rules(cafe):
    r1, h1 = cafe.rider()
    pickup = cafe.place("pickup", h=cafe.customer("P", phone="9111111111")).json()["order"]
    cafe.post(f"/admin/orders/{pickup['id']}/accept", h=cafe.admin)
    cafe.advance(pickup["id"], "preparing", "ready")
    assert cafe.post(f"/admin/orders/{pickup['id']}/assign", {"rider_id": r1}, h=cafe.admin).json()["error"]["code"] == "not_delivery"
    o, ch = ready_delivery(cafe)
    cafe.advance(o["id"], "completed") if False else None
    # order must be ready
    d2 = cafe.place("delivery", h=ch).json()["order"]
    assert cafe.post(f"/admin/orders/{d2['id']}/assign", {"rider_id": r1}, h=cafe.admin).json()["error"]["code"] == "invalid_transition"
    # offline rider cannot be assigned
    cafe.patch("/riders/me/availability", {"available": False}, h=h1)
    assert cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": r1}, h=cafe.admin).json()["error"]["code"] == "rider_unavailable"
    cafe.patch("/riders/me/availability", {"available": True}, h=h1)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": r1}, h=cafe.admin).json()["assignment_id"]
    # conflicting active assignments are blocked while the rule is on
    o2, _ = ready_delivery(cafe, ch)
    assert cafe.post(f"/admin/orders/{o2['id']}/assign", {"rider_id": r1}, h=cafe.admin).json()["error"]["code"] == "rider_busy"
    assert cafe.patch("/riders/me/availability", {"available": False}, h=h1).json()["error"]["code"] == "rider_busy"
    cafe.patch("/admin/settings/cafe", {"rider_single_active_assignment": False}, h=cafe.admin)
    aid2 = cafe.post(f"/admin/orders/{o2['id']}/assign", {"rider_id": r1}, h=cafe.admin).json()["assignment_id"]
    assert cafe.post(f"/deliveries/{aid}/accept", h=h1).status_code == 200
    assert cafe.post(f"/deliveries/{aid2}/accept", h=h1).status_code == 200  # allowed once the rule is off
    # customers/riders cannot assign
    assert cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": r1}, h=h1).status_code == 403


def test_cancelling_order_closes_rider_assignment(cafe):
    r1, h1 = cafe.rider()
    o, ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": r1}, h=cafe.admin).json()["assignment_id"]
    cafe.post(f"/deliveries/{aid}/accept", h=h1)
    assert cafe.post(f"/admin/orders/{o['id']}/cancel", {"reason": "customer unreachable"}, h=cafe.admin).status_code == 200
    assert cafe.get("/deliveries/mine", h=h1).json() == []
    assert cafe.post(f"/deliveries/{aid}/picked-up", h=h1).status_code == 409


def test_rider_sees_only_own_deliveries_and_needed_details(cafe):
    r1, h1 = cafe.rider("R1", "9000000001")
    r2, h2 = cafe.rider("R2", "9000000002")
    o, ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": r1}, h=cafe.admin).json()["assignment_id"]
    mine = cafe.get("/deliveries/mine", h=h1).json()
    assert len(mine) == 1 and mine[0]["order"]["delivery_address"]["line1"] == "12 MG Road" and mine[0]["order"]["customer_phone"]
    assert mine[0]["order"]["collect_cash_paise"] == o["total_paise"]  # cash on delivery amount
    assert "payments" not in mine[0]["order"] and "total_paise" not in mine[0]["order"]
    assert cafe.get("/deliveries/mine", h=h2).json() == []
    assert cafe.get(f"/deliveries/{aid}", h=h2).status_code == 404
    assert cafe.get(f"/orders/{o['id']}", h=h2).status_code == 404
    assert cafe.get(f"/orders/{o['id']}", h=h1).status_code == 200  # assigned rider may view the order
    assert cafe.get(f"/admin/orders/{o['id']}", h=h1).status_code == 403


# ------------------------------------------------------------------ tracking
def active_delivery(cafe):
    rid, rh = cafe.rider()
    o, ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    cafe.post(f"/deliveries/{aid}/accept", h=rh)
    return rid, rh, o, ch, aid


def loc(aid, lat=12.9716, lng=77.5946, **kw):
    return {"assignment_id": aid, "latitude": lat, "longitude": lng, "accuracy_m": 8.5, **kw}


def test_location_tracking_authorization_and_validation(cafe):
    rid, rh, o, ch, aid = active_delivery(cafe)
    other_rider = cafe.rider("R2", "9000000002")[1]
    assert cafe.post("/tracking/location", loc(aid), h=other_rider).status_code == 404            # someone else's assignment
    assert cafe.post("/tracking/location", loc(aid), h=ch).status_code == 403                      # customers cannot post
    assert cafe.post("/tracking/location", loc(aid)).status_code == 401
    assert cafe.post("/tracking/location", loc(aid, lat=95), h=rh).json()["error"]["code"] == "invalid_coordinates"
    assert cafe.post("/tracking/location", loc(aid, lat=0, lng=0), h=rh).status_code == 422
    assert cafe.post("/tracking/location", {**loc(aid), "accuracy_m": -4}, h=rh).status_code == 422
    assert cafe.post("/tracking/location", loc(aid), h=rh).status_code == 200
    # too-frequent updates are rate limited (configurable)
    assert cafe.post("/tracking/location", loc(aid), h=rh).json()["error"]["code"] == "location_too_frequent"
    t = cafe.get(f"/tracking/orders/{o['id']}", h=ch).json()
    assert t["tracking_active"] and t["location"]["latitude"] == 12.9716 and t["location"]["is_stale"] is False and t["update_interval_seconds"] == 10
    assert cafe.get(f"/tracking/orders/{o['id']}", h=cafe.admin).status_code == 200
    stranger = cafe.customer("Z", phone="9111111112")
    assert cafe.get(f"/tracking/orders/{o['id']}", h=stranger).status_code == 404                 # another customer
    assert cafe.get(f"/tracking/orders/{o['id']}", h=rh).status_code == 404                      # riders use their own app state
    live = cafe.get("/tracking/active", h=cafe.admin).json()
    assert live[0]["order_id"] == o["id"] and live[0]["tracking"]["location"]["longitude"] == 77.5946
    assert cafe.get("/tracking/active", h=ch).status_code == 403


def test_location_requires_active_delivery_and_stops_after_completion(cafe):
    rid, rh = cafe.rider()
    o, ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    assert cafe.post("/tracking/location", loc(aid), h=rh).json()["error"]["code"] == "tracking_inactive"  # only offered, not accepted
    cafe.post(f"/deliveries/{aid}/accept", h=rh)
    cafe.post(f"/deliveries/{aid}/picked-up", h=rh)
    cafe.post(f"/deliveries/{aid}/out-for-delivery", h=rh)
    assert cafe.post("/tracking/location", loc(aid), h=rh).status_code == 200
    otp = customer_otp(cafe, o["id"], ch)
    cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": otp}, h=rh)
    assert cafe.post("/tracking/location", loc(aid, lat=12.98), h=rh).json()["error"]["code"] == "tracking_inactive"
    t = cafe.get(f"/tracking/orders/{o['id']}", h=ch).json()
    assert t["tracking_active"] is False and t["location"] is None  # nothing about the rider is exposed after delivery


def test_stale_location_is_flagged_not_presented_as_live(cafe):
    rid, rh, o, ch, aid = active_delivery(cafe)
    cafe.post("/tracking/location", loc(aid), h=rh)
    from sqlalchemy import update
    from app.core.security import now_utc
    from app.db.session import session_scope
    from app.models.delivery import RiderLocation

    async def age():
        async with session_scope() as db:
            await db.execute(update(RiderLocation).values(recorded_at=now_utc() - timedelta(minutes=10)))
    cafe.call(age)
    t = cafe.get(f"/tracking/orders/{o['id']}", h=ch).json()["location"]
    assert t["is_stale"] is True and t["age_seconds"] >= 590 and t["latitude"] == 12.9716  # last known + its timestamp


def test_guest_can_track_own_delivery(cafe):
    rid, rh = cafe.rider()
    r = cafe.place("delivery", customer_name="Guest", customer_phone="9800099999").json()
    o, gt = r["order"], {"X-Guest-Token": r["guest_token"]}
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.advance(o["id"], "preparing", "ready")
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    cafe.post(f"/deliveries/{aid}/accept", h=rh)
    cafe.post("/tracking/location", loc(aid), h=rh)
    assert cafe.get(f"/tracking/orders/{o['id']}", h=gt).json()["location"]["latitude"] == 12.9716
    assert cafe.get(f"/tracking/orders/{o['id']}", h={"X-Guest-Token": "nope"}).status_code == 404


def test_location_retention_job(cafe):
    rid, rh, o, ch, aid = active_delivery(cafe)
    cafe.post("/tracking/location", loc(aid), h=rh)
    cafe.patch("/admin/settings/cafe", {"location_retention_days": 1}, h=cafe.admin)
    from sqlalchemy import func, select, update
    from app.core.security import now_utc
    from app.db.session import session_scope
    from app.models.delivery import RiderLocation
    from app.workers.handlers import _cleanup_locations

    async def run():
        async with session_scope() as db:
            await db.execute(update(RiderLocation).values(received_at=now_utc() - timedelta(days=3)))
        async with session_scope() as db:
            await _cleanup_locations(db, {})
        async with session_scope() as db:
            return (await db.execute(select(func.count()).select_from(RiderLocation))).scalar_one()
    assert cafe.call(run) == 0
