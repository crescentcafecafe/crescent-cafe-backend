import uuid

import pytest


def totals(o):
    return o["subtotal_paise"], o["discount_paise"], o["tax_paise"], o["delivery_charge_paise"], o["total_paise"]


def test_four_order_types(cafe):
    h = cafe.customer()
    pickup = cafe.place("pickup", h=h).json()["order"]
    dine = cafe.place("dine_in", h=h).json()["order"]
    deliv = cafe.place("delivery", h=h).json()["order"]
    walk = cafe.walk_in().json()["order"]
    assert [pickup["order_type"], dine["order_type"], deliv["order_type"], walk["order_type"]] == ["pickup", "dine_in", "delivery", "walk_in"]
    assert (pickup["status"], dine["status"], deliv["status"]) == ("placed", "placed", "placed")
    assert walk["status"] == "accepted" and walk["source"] == "admin"  # created by Admin: no approval step
    assert dine["table_label"].startswith("Table 1") and dine["source"] == "customer_app"
    d = cafe.get(f"/admin/orders/{walk['id']}", h=cafe.admin).json()
    assert d["created_by_admin_id"] and [x["to"] for x in d["history"]] == ["accepted"]


def test_customers_cannot_create_walk_in_and_admin_only_walk_in(cafe):
    h = cafe.customer()
    r = cafe.post("/orders", {"order_type": "walk_in", "payment_method": "cash", "items": cafe.cart()}, h=h)
    assert r.status_code == 403
    assert cafe.post("/admin/orders", {"order_type": "pickup", "payment_method": "cash", "items": cafe.cart()}, h=cafe.admin).status_code == 201  # coerced to walk_in
    assert cafe.post("/admin/orders", {"order_type": "walk_in", "payment_method": "cash", "items": cafe.cart()}, h=h).status_code == 403


def test_price_tax_exclusive_and_client_totals_ignored(cafe):
    h = cafe.customer()
    # Cake 100.00 + Extra cream 2 x 20.00 = 140.00 ; GST 5% exclusive = 7.00 ; total 147.00
    item = {"menu_item_id": cafe.ids["cake"], "quantity": 1, "addons": [{"addon_id": cafe.ids["addon"], "quantity": 2}]}
    r = cafe.post("/orders", {"order_type": "pickup", "payment_method": "cash", "items": [item], "total_paise": 1, "subtotal_paise": 1, "tax_paise": 0}, h=h)
    o = r.json()["order"]
    assert totals(o) == (14000, 0, 700, 0, 14700)  # client-supplied totals are simply ignored
    assert o["tax_breakdown"] == [{"name": "GST 5%", "rate_bps": 500, "taxable_paise": 14000, "tax_paise": 700}]
    assert o["items"][0]["customizations"][0]["name"] == "Extra cream"
    q = cafe.post("/orders/quote", {"order_type": "pickup", "items": [item]}, h=h).json()
    assert q["total_paise"] == 14700


def test_price_tax_inclusive_mode(cafe):
    cafe.patch("/admin/settings/cafe", {"prices_include_tax": True}, h=cafe.admin)
    o = cafe.place("pickup", h=cafe.customer(), kind="cake").json()["order"]
    # 100.00 incl. 5% -> tax part = 100 - 100/1.05 = 4.76 ; total unchanged
    assert totals(o) == (10000, 0, 476, 0, 10000)


def test_variant_and_addon_validation(cafe):
    h, i = cafe.customer(), cafe.ids
    def post(item): return cafe.post("/orders", {"order_type": "pickup", "payment_method": "cash", "items": [item]}, h=h)
    assert post({"menu_item_id": i["latte"], "quantity": 1}).json()["error"]["code"] == "variant_required"
    assert post({"menu_item_id": i["cake"], "quantity": 1, "variant_id": i["latte_small"]}).json()["error"]["code"] == "invalid_variant"
    assert post({"menu_item_id": i["latte"], "variant_id": i["latte_large"], "quantity": 2}).json()["order"]["subtotal_paise"] == 40000
    assert post({"menu_item_id": i["cake"], "quantity": 1, "addons": [{"addon_id": i["addon"], "quantity": 3}]}).json()["error"]["code"] == "invalid_addon"
    assert post({"menu_item_id": i["cake"], "quantity": 0}).status_code == 422
    assert post({"menu_item_id": str(uuid.uuid4()), "quantity": 1}).status_code == 404


def test_unavailable_items_cannot_be_ordered_and_recheck_on_submit(cafe):
    h, i = cafe.customer(), cafe.ids
    r = cafe.post("/orders", {"order_type": "pickup", "payment_method": "cash", "items": [{"menu_item_id": i["soup"], "quantity": 1}]}, h=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "item_unavailable"
    cafe.put(f"/admin/menu/items/{i['cake']}/availability", {"is_available": False}, h=cafe.admin)  # switched off AFTER the cart was built
    r = cafe.place("pickup", h=h, kind="cake")
    assert r.status_code == 409 and r.json()["error"]["code"] == "item_unavailable"
    cafe.put(f"/admin/menu/items/{i['cake']}/availability", {"is_available": True}, h=cafe.admin)
    assert cafe.place("pickup", h=h, kind="cake").status_code == 201


def test_delivery_charge_min_order_and_area(cafe):
    h = cafe.customer()
    o = cafe.place("delivery", h=h).json()["order"]
    assert o["delivery_charge_paise"] == 3000 and o["total_paise"] == 10000 + 500 + 3000
    big = cafe.place("delivery", h=h, qty=6).json()["order"]  # 600.00 >= free-delivery threshold
    assert big["delivery_charge_paise"] == 0
    bad = cafe.place("delivery", h=h, delivery_address={"line1": "Far away road", "postal_code": "999999"})
    assert bad.json()["error"]["code"] == "outside_delivery_area"
    cafe.patch("/admin/settings/delivery", {"min_order_paise": 20000}, h=cafe.admin)
    assert cafe.place("delivery", h=h).json()["error"]["code"] == "below_min_delivery_order"
    assert cafe.post("/orders", {"order_type": "delivery", "payment_method": "cash", "items": cafe.cart()}, h=h).status_code == 422  # address missing


def test_pickup_and_dine_in_toggles_and_closed_cafe(cafe):
    h = cafe.customer()
    cafe.patch("/admin/settings/cafe", {"pickup_enabled": False}, h=cafe.admin)
    assert cafe.place("pickup", h=h).json()["error"]["code"] == "pickup_disabled"
    cafe.patch("/admin/settings/cafe", {"pickup_enabled": True, "temporarily_closed": True, "temporary_closure_message": "Back at 5"}, h=cafe.admin)
    r = cafe.place("pickup", h=h)
    assert r.status_code == 409 and r.json()["error"]["message"] == "Back at 5"
    assert cafe.walk_in().status_code == 201  # Admin can still create walk-ins while closed


def test_opening_hours_enforced(cafe):
    from datetime import datetime
    from app.services.settings import is_open_now
    from app.models.settings import CafeSettings
    cs = CafeSettings(opening_hours={d: [{"open": "09:00", "close": "22:00"}] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}, closures=[{"date": "2026-12-25", "reason": "Christmas"}],
                      timezone="Asia/Kolkata", enforce_opening_hours=True, accepting_orders=True, temporarily_closed=False)
    from datetime import timezone
    assert is_open_now(cs, datetime(2026, 11, 12, 6, 30, tzinfo=timezone.utc))[0] is True   # 12:00 IST
    assert is_open_now(cs, datetime(2026, 11, 12, 20, 30, tzinfo=timezone.utc))[0] is False  # 02:00 IST
    ok, why = is_open_now(cs, datetime(2026, 12, 25, 6, 30, tzinfo=timezone.utc))
    assert ok is False and why == "Christmas"
    assert cafe.patch("/admin/settings/cafe", {"opening_hours": {"funday": []}}, h=cafe.admin).status_code == 422


def test_idempotent_order_creation(cafe):
    h = cafe.customer()
    body = {"order_type": "pickup", "payment_method": "cash", "items": cafe.cart()}
    a = cafe.post("/orders", body, h={**h, "Idempotency-Key": "abc-123"}).json()
    b = cafe.post("/orders", body, h={**h, "Idempotency-Key": "abc-123"}).json()
    assert a["order"]["id"] == b["order"]["id"] and b["duplicate"] is True
    c = cafe.post("/orders", body, h={**h, "Idempotency-Key": "other"}).json()
    assert c["order"]["id"] != a["order"]["id"]
    assert len(cafe.get("/orders", h=h).json()["items"]) == 2


def test_guest_checkout_and_secure_tracking(cafe):
    r = cafe.place("pickup", customer_name="Guest G", customer_phone="98000 11111").json()
    o, token = r["order"], r["guest_token"]
    assert token and o["customer_phone"] == "+919800011111"
    assert cafe.get(f"/orders/{o['id']}").status_code == 404                                  # no token -> not found
    assert cafe.get(f"/orders/{o['id']}", h={"X-Guest-Token": "wrong"}).status_code == 404
    assert cafe.get(f"/orders/{o['id']}", h={"X-Guest-Token": token}).status_code == 200
    other = cafe.place("pickup", customer_name="Guest H", customer_phone="9800022222").json()
    assert cafe.get(f"/orders/{other['order']['id']}", h={"X-Guest-Token": token}).status_code == 404  # token is per order
    from sqlalchemy import select
    from app.db.session import session_scope
    from app.models.orders import Order

    async def stored():
        async with session_scope() as db:
            return (await db.execute(select(Order.guest_token_hash).where(Order.id == uuid.UUID(o["id"])))).scalar_one()
    assert cafe.call(stored) != token  # only a hash is stored


def test_guest_requires_name_and_phone_and_can_be_disabled(cafe):
    assert cafe.place("pickup").json()["error"]["code"] == "phone_required"
    assert cafe.place("pickup", customer_name="X", customer_phone="123").json()["error"]["code"] == "invalid_phone"
    cafe.patch("/admin/settings/cafe", {"guest_checkout_enabled": False}, h=cafe.admin)
    assert cafe.place("pickup", customer_name="X", customer_phone="9800011111").status_code == 403


def test_logged_in_customer_without_phone_must_add_it(cafe):
    t = cafe.post("/auth/customer/google", {"id_token": "good-nophone"}).json()
    h = {"Authorization": f"Bearer {t['access_token']}"}
    r = cafe.place("pickup", h=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "phone_required"
    ok = cafe.place("pickup", h=h, customer_phone="9876501234")  # supplying it with the order saves it to the profile
    assert ok.status_code == 201
    assert cafe.get("/customers/me", h=h).json()["phone"] == "+919876501234"


def test_customer_order_isolation(cafe):
    h1, h2 = cafe.customer("A"), cafe.customer("B", phone="9111111111")
    o = cafe.place("pickup", h=h1).json()["order"]
    assert cafe.get(f"/orders/{o['id']}", h=h2).status_code == 404
    assert cafe.post(f"/orders/{o['id']}/cancel", {"reason": "mine now"}, h=h2).status_code == 404
    assert cafe.get(f"/payments/orders/{o['id']}", h=h2).status_code == 404
    assert cafe.get(f"/invoices/order/{o['id']}", h=h2).status_code == 404
    assert cafe.get("/orders", h=h2).json()["total"] == 0


# ------------------------------------------------------------------ state machine
def test_lifecycle_pickup_and_dine_in(cafe):
    a = cafe.admin
    for kind in ("pickup", "dine_in"):
        o = cafe.place(kind, h=cafe.customer(f"C{kind}")).json()["order"]
        assert cafe.post(f"/admin/orders/{o['id']}/accept", h=a).json()["status"] == "accepted"
        cafe.advance(o["id"], "preparing", "ready", "completed")
        d = cafe.get(f"/admin/orders/{o['id']}", h=a).json()
        assert [x["to"] for x in d["history"]] == ["placed", "accepted", "preparing", "ready", "completed"]
        assert all(x["actor_role"] for x in d["history"])


def test_invalid_and_repeated_transitions_rejected(cafe):
    a = cafe.admin
    o = cafe.place("pickup", h=cafe.customer()).json()["order"]
    r = cafe.post(f"/admin/orders/{o['id']}/status", {"status": "ready"}, h=a)
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_transition"
    assert cafe.post(f"/admin/orders/{o['id']}/accept", h=a).status_code == 200
    again = cafe.post(f"/admin/orders/{o['id']}/accept", h=a)
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_in_status"
    assert cafe.post(f"/admin/orders/{o['id']}/status", {"status": "delivered"}, h=a).status_code == 422  # not an allowed admin status
    cafe.advance(o["id"], "preparing", "ready", "completed")
    assert cafe.post(f"/admin/orders/{o['id']}/status", {"status": "preparing"}, h=a).status_code == 409  # no going back
    assert cafe.post(f"/admin/orders/{o['id']}/cancel", {"reason": "too late now"}, h=a).status_code == 409


def test_pickup_orders_have_no_rider_states(cafe):
    from app.services.order_state import TRANSITIONS
    for t in ("pickup", "dine_in", "walk_in"):
        flat = {s for v in TRANSITIONS[t].values() for s in v} | set(TRANSITIONS[t])
        assert not ({"rider_assigned", "picked_up", "out_for_delivery", "delivered"} & flat)
    assert "delivered" in {s for v in TRANSITIONS["delivery"].values() for s in v}


def test_reject_and_cancel_require_reason(cafe):
    a = cafe.admin
    o = cafe.place("pickup", h=cafe.customer()).json()["order"]
    assert cafe.post(f"/admin/orders/{o['id']}/reject", {"reason": ""}, h=a).status_code == 422
    r = cafe.post(f"/admin/orders/{o['id']}/reject", {"reason": "Out of milk"}, h=a).json()
    assert r["status"] == "rejected" and r["rejection_reason"] == "Out of milk"
    o2 = cafe.place("pickup", h=cafe.customer("D")).json()["order"]
    assert cafe.post(f"/admin/orders/{o2['id']}/cancel", {"reason": "kitchen closed"}, h=a).json()["cancellation_reason"] == "kitchen closed"
    assert cafe.post(f"/admin/orders/{o2['id']}/accept", h=a).status_code == 409  # cannot resurrect


def test_customer_can_cancel_only_before_acceptance(cafe):
    h = cafe.customer()
    o = cafe.place("pickup", h=h).json()["order"]
    assert cafe.post(f"/orders/{o['id']}/cancel", {"reason": "changed my mind"}, h=h).json()["status"] == "cancelled"
    o2 = cafe.place("pickup", h=h).json()["order"]
    cafe.post(f"/admin/orders/{o2['id']}/accept", h=cafe.admin)
    r = cafe.post(f"/orders/{o2['id']}/cancel", {"reason": "changed my mind"}, h=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_cancellable"


def test_delivery_orders_cannot_be_completed_without_otp(cafe):
    a = cafe.admin
    o = cafe.place("delivery", h=cafe.customer()).json()["order"]
    cafe.post(f"/admin/orders/{o['id']}/accept", h=a)
    cafe.advance(o["id"], "preparing", "ready")
    r = cafe.post(f"/admin/orders/{o['id']}/status", {"status": "completed"}, h=a)
    assert r.status_code == 409 and r.json()["error"]["code"] == "otp_required"
    # the service layer itself also refuses to deliver without the OTP path
    from app.core.errors import Forbidden
    from app.db.session import session_scope
    from app.services import order_state as st

    async def try_deliver():
        async with session_scope() as db:
            await st.transition(db, uuid.UUID(o["id"]), "delivered", actor_role="admin")
    with pytest.raises(Forbidden):
        cafe.call(try_deliver)


def test_admin_new_order_alarm_events(cafe):
    h = cafe.customer()
    # an admin device is registered so FCM can reach it
    cafe.post("/notifications/devices", {"install_id": "admin-phone-1", "fcm_token": "A" * 40}, h=cafe.admin)
    o = cafe.place("pickup", h=h).json()["order"]
    cafe.run_jobs()
    alarms = [m for m in cafe.push.sent if m["data"]["type"] == "order.new"]
    assert len(alarms) == 1
    assert alarms[0]["high_priority"] and alarms[0]["data_only"] and alarms[0]["data"]["order_id"] == o["id"] and alarms[0]["data"]["alarm"] == "start"
    assert [x["id"] for x in cafe.get("/admin/orders/pending", h=cafe.admin).json()] == [o["id"]]
    assert cafe.post(f"/admin/orders/{o['id']}/alarm/ack", h=cafe.admin).status_code == 200
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.run_jobs()
    stops = [m for m in cafe.push.sent if m["data"]["type"] == "order.alarm_stop"]
    assert len(stops) == 1 and stops[0]["data"]["alarm"] == "stop"
    assert cafe.get("/admin/orders/pending", h=cafe.admin).json() == []


def test_new_order_event_is_deduplicated(cafe):
    o = cafe.place("pickup", h=cafe.customer()).json()["order"]
    from app.db.session import session_scope
    from app.models.orders import Order
    from app.services import order_state as st

    async def again():
        async with session_scope() as db:
            await st.emit_new_order_alarm(db, await db.get(Order, uuid.UUID(o["id"])))
    cafe.call(again)
    cafe.call(again)
    from sqlalchemy import func, select
    from app.models.notifications import NotificationEvent

    async def count():
        async with session_scope() as db:
            return (await db.execute(select(func.count()).select_from(NotificationEvent).where(NotificationEvent.type == "order.new"))).scalar_one()
    assert cafe.call(count) == 1


def test_admin_order_filters_and_dashboard(cafe):
    a = cafe.admin
    h = cafe.customer()
    o1 = cafe.place("pickup", h=h).json()["order"]
    cafe.place("dine_in", h=h)
    cafe.post(f"/admin/orders/{o1['id']}/accept", h=a)
    assert cafe.get("/admin/orders?status=accepted", h=a).json()["total"] == 1
    assert cafe.get("/admin/orders?order_type=dine_in", h=a).json()["total"] == 1
    assert cafe.get(f"/admin/orders?q={o1['order_number'][:8]}", h=a).json()["total"] >= 1
    assert cafe.get("/admin/orders?phone=9876543210", h=a).json()["total"] == 2
    d = cafe.get("/admin/dashboard", h=a).json()
    assert d["pending_decisions"] == 1 and d["orders_placed_today"] == 2
    assert d["pending_counter_payments"] == 2 and isinstance(d["setup_warnings"], list)
