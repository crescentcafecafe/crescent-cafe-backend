"""Rider/customer notification events and the in-app notification centre (inbox)."""
import re
import uuid
from datetime import timedelta

from sqlalchemy import select, update

from app.core.security import now_utc
from app.db.session import session_scope
from app.models.delivery import RiderAssignment
from app.models.notifications import NotificationEvent
from tests.test_delivery import customer_otp, go_out, ready_delivery


def event_types(cafe):
    async def q():
        async with session_scope() as db:
            return [e.type for e in (await db.execute(select(NotificationEvent).order_by(NotificationEvent.id))).scalars().all()]
    return cafe.call(q)


def rider_with_phone(cafe):
    rid, rh = cafe.rider()
    cafe.post("/notifications/devices", {"install_id": "rider-phone", "fcm_token": "R" * 40}, h=rh)
    return rid, rh


def test_rider_alarm_stops_on_every_phone_when_answered(cafe):
    rid, rh = rider_with_phone(cafe)
    o, _ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    cafe.run_jobs()
    offer = [m for m in cafe.push.sent if m["data"]["type"] == "assignment.offered"]
    assert offer and offer[0]["data_only"] and offer[0]["data"]["alarm"] == "start"
    assert offer[0]["data"]["title"] == "New delivery assignment"          # data-only pushes now carry their text
    cafe.push.sent.clear()
    assert cafe.post(f"/deliveries/{aid}/accept", h=rh).status_code == 200
    cafe.run_jobs()
    stop = [m for m in cafe.push.sent if m["data"]["type"] == "assignment.alarm_stop"]
    assert len(stop) == 1 and stop[0]["data"]["alarm"] == "stop" and stop[0]["data_only"]


def test_rejecting_also_stops_the_alarm(cafe):
    rid, rh = rider_with_phone(cafe)
    o, _ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    cafe.push.sent.clear()
    assert cafe.post(f"/deliveries/{aid}/reject", {"reason": "too far"}, h=rh).status_code == 200
    cafe.run_jobs()
    assert [m for m in cafe.push.sent if m["data"]["type"] == "assignment.alarm_stop"]


def test_unanswered_offer_rearms_the_rider_alarm(cafe):
    from app.workers.handlers import _remind_assignments
    rid, rh = rider_with_phone(cafe)
    o, _ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]

    async def age_and_run():
        async with session_scope() as db:
            await db.execute(update(RiderAssignment).where(RiderAssignment.id == uuid.UUID(aid)).values(offered_at=now_utc() - timedelta(minutes=3)))
        async with session_scope() as db:
            await _remind_assignments(db, {})
            await _remind_assignments(db, {})   # running twice must not create a second reminder
    cafe.call(age_and_run)
    assert event_types(cafe).count("assignment.reminder") == 1
    cafe.run_jobs()
    rem = [m for m in cafe.push.sent if m["data"]["type"] == "assignment.reminder"]
    assert rem and rem[0]["data"]["alarm"] == "start" and rem[0]["data_only"]


def test_rider_gets_cash_and_completion_notifications(cafe):
    rid, rh = rider_with_phone(cafe)
    o, ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    cafe.push.sent.clear()
    go_out(cafe, aid, rh)
    otp = re.search(r"\b(\d{6})\b", [m for m in cafe.push.sent if m["data"]["type"] == "delivery.otp"][0]["body"]).group(1)
    assert cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": otp}, h=rh).status_code == 200
    amount = cafe.get(f"/admin/riders/{rid}/cash", h=cafe.admin).json()["outstanding_paise"] if cafe.get(f"/admin/riders/{rid}/cash", h=cafe.admin).status_code == 200 else None
    types = event_types(cafe)
    for t in ("cash.collect_due", "cash.collected", "delivery.rider_done", "delivery.completed"):
        assert t in types, t
    h = cafe.post(f"/admin/riders/{rid}/cash-handover", {"amount_paise": 100, "note": "t"}, h=cafe.admin)
    assert h.status_code in (200, 409), h.text
    if h.status_code == 200:
        assert "cash.handover_confirmed" in event_types(cafe)
    cafe.run_jobs()
    sent = {m["data"]["type"] for m in cafe.push.sent if m["token"].startswith("R")}
    assert {"cash.collect_due", "cash.collected", "delivery.rider_done"} <= sent
    del amount


def test_rider_is_told_the_order_is_ready_for_pickup(cafe):
    rid, rh = rider_with_phone(cafe)
    o, _ch = ready_delivery(cafe)
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    assert "delivery.order_ready" not in event_types(cafe)
    assert cafe.post(f"/deliveries/{aid}/accept", h=rh).status_code == 200
    assert event_types(cafe).count("delivery.order_ready") == 1
    items = cafe.get("/notifications/inbox", h=rh).json()["items"]
    assert any(i["title"] == "Order ready for pickup" and i["category"] == "delivery" for i in items)


# ------------------------------------------------------------------ the notification centre
def test_customer_inbox_lists_marks_and_clears(cafe):
    ch = cafe.customer("Inbox", phone="9811111111")
    cafe.post("/notifications/devices", {"install_id": "c1", "fcm_token": "C" * 40}, h=ch)
    o = cafe.place("pickup", h=ch).json()["order"]
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.advance(o["id"], "preparing", "ready")
    inbox = cafe.get("/notifications/inbox", h=ch).json()
    assert inbox["items"] and all(not i["read"] for i in inbox["items"])
    assert {i["category"] for i in inbox["items"]} == {"orders"}
    assert all(i["type"] not in ("order.alarm_stop", "order.alarm_ack") for i in inbox["items"])
    n = cafe.get("/notifications/inbox/unread-count", h=ch).json()
    assert n["unread"] == len(inbox["items"]) and n["by_category"]["orders"] == n["unread"]
    first = inbox["items"][0]["id"]
    assert cafe.post("/notifications/inbox/read", {"ids": [first]}, h=ch).json()["updated"] == 1
    assert cafe.get("/notifications/inbox/unread-count", h=ch).json()["unread"] == n["unread"] - 1
    unread_only = cafe.get("/notifications/inbox?category=unread", h=ch).json()["items"]
    assert first not in [i["id"] for i in unread_only]
    assert cafe.post("/notifications/inbox/read-all", h=ch).status_code == 200
    assert cafe.get("/notifications/inbox/unread-count", h=ch).json()["unread"] == 0
    assert cafe.post("/notifications/inbox/clear", {"ids": [first]}, h=ch).json()["updated"] == 1
    assert first not in [i["id"] for i in cafe.get("/notifications/inbox", h=ch).json()["items"]]
    assert cafe.post("/notifications/inbox/clear-all", h=ch).status_code == 200
    assert cafe.get("/notifications/inbox", h=ch).json()["items"] == []


def test_inbox_is_private_and_cannot_be_marked_by_others(cafe):
    a = cafe.customer("Alice", phone="9822222222")
    b = cafe.customer("Bob", phone="9833333333")
    o = cafe.place("pickup", h=a).json()["order"]
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    mine = cafe.get("/notifications/inbox", h=a).json()["items"]
    assert mine and cafe.get("/notifications/inbox", h=b).json()["items"] == []
    assert cafe.post("/notifications/inbox/read", {"ids": [mine[0]["id"]]}, h=b).json()["updated"] == 0
    assert cafe.post("/notifications/inbox/clear", {"ids": [mine[0]["id"]]}, h=b).json()["updated"] == 0
    assert not cafe.get("/notifications/inbox", h=a).json()["items"][0]["read"]
    assert cafe.get("/notifications/inbox").status_code == 401


def test_rider_and_admin_inbox_and_categories(cafe):
    rid, rh = rider_with_phone(cafe)
    o, _ch = ready_delivery(cafe)
    cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin)
    r_items = cafe.get("/notifications/inbox", h=rh).json()["items"]
    assert [i["type"] for i in r_items] == ["assignment.offered"] and r_items[0]["category"] == "delivery"
    assert cafe.get("/notifications/inbox?category=orders", h=rh).json()["items"] == []
    admin_items = cafe.get("/notifications/inbox", h=cafe.admin).json()["items"]
    assert any(i["type"] == "order.new" for i in admin_items)
    assert not any(i["type"] in ("order.alarm_stop", "assignment.alarm_stop") for i in admin_items)


def test_promotions_reach_only_customers_who_opted_in(cafe):
    yes, no = cafe.customer("Yes", phone="9844444444"), cafe.customer("No", phone="9855555555")
    assert cafe.patch("/customers/me", {"marketing_opt_in": True}, h=yes).status_code == 200
    r = cafe.post("/admin/notifications/broadcast", {"title": "Free brownie", "body": "Today only"}, h=cafe.admin)
    assert r.status_code in (200, 201, 202), r.text
    assert [i["title"] for i in cafe.get("/notifications/inbox?category=system", h=yes).json()["items"]] == ["Free brownie"]
    assert cafe.get("/notifications/inbox", h=no).json()["items"] == []
    cafe.patch("/customers/me", {"marketing_opt_in": False}, h=yes)   # opting out hides it again
    assert cafe.get("/notifications/inbox", h=yes).json()["items"] == []
