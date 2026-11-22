import json
import uuid

from tests.test_delivery import customer_otp


def to_door(cafe, payment="cash", rider=("Ravi", "9000000001"), **extra):
    """Delivery order picked up by a rider and out for delivery. Returns (rider_id, rider_headers, order, customer_headers, assignment_id)."""
    rid, rh = cafe.rider(*rider)
    h = cafe.customer("Cust", phone="9876543210")
    cafe.post("/notifications/devices", {"install_id": f"c-{uuid.uuid4().hex[:6]}", "fcm_token": "C" * 30 + uuid.uuid4().hex}, h=h)
    o = cafe.place("delivery", payment=payment, h=h, **extra).json()["order"]
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.advance(o["id"], "preparing", "ready")
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    assert cafe.post(f"/deliveries/{aid}/accept", h=rh).status_code == 200
    assert cafe.post(f"/deliveries/{aid}/picked-up", h=rh).status_code == 200
    assert cafe.post(f"/deliveries/{aid}/out-for-delivery", h=rh).status_code == 200
    return rid, rh, o, h, aid


def due(cafe, aid, rh):
    return cafe.get(f"/deliveries/{aid}", h=rh).json()["order"]["collect_cash_paise"]


def qr_id_of(cafe, order_id):
    from sqlalchemy import select
    from app.db.session import session_scope
    from app.models.payments import Payment

    async def q():
        async with session_scope() as db:
            return (await db.execute(select(Payment.gateway_order_id).where(Payment.order_id == uuid.UUID(order_id), Payment.collection_channel == "rider_upi_qr"))).scalars().first()
    return cafe.call(q)


def qr_webhook(cafe, qr_id, amount, event_id="evqr1"):
    body = {"event_id": event_id, "type": "qr_code.credited", "order_id": qr_id, "status": "paid", "payment_id": f"pay_{event_id}", "amount": amount, "currency": "INR"}
    return cafe.req("POST", "/webhooks/razorpay", h={"x-test-signature": "ok", "content-type": "application/json"}, content=json.dumps(body).encode())


def test_delivery_cannot_complete_before_payment_and_the_try_is_not_wasted(cafe):
    rid, rh, o, ch, aid = to_door(cafe)
    otp = customer_otp(cafe, o["id"], ch)
    r = cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": otp}, h=rh)
    assert r.status_code == 409 and r.json()["error"]["code"] == "payment_not_collected"
    assert r.json()["error"]["details"]["amount_due_paise"] == due(cafe, aid, rh)
    cafe.post(f"/deliveries/{aid}/collect/cash", {"amount_received_paise": due(cafe, aid, rh)}, h=rh)
    ok = cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": otp}, h=rh)  # same code still works: the early attempt did not burn it
    assert ok.status_code == 200 and ok.json()["status"] == "delivered"


def test_rider_records_cash_with_change_and_it_is_idempotent(cafe):
    rid, rh, o, ch, aid = to_door(cafe)
    amount = due(cafe, aid, rh)
    assert amount > 0
    low = cafe.post(f"/deliveries/{aid}/collect/cash", {"amount_received_paise": amount - 1}, h=rh)
    assert low.status_code == 422 and low.json()["error"]["code"] == "insufficient_amount"
    ok = cafe.post(f"/deliveries/{aid}/collect/cash", {"amount_received_paise": amount + 500}, h=rh)
    assert ok.status_code == 200, ok.text
    assert ok.json()["change_due_paise"] == 500 and ok.json()["already_recorded"] is False and ok.json()["payment_status"] == "paid"
    again = cafe.post(f"/deliveries/{aid}/collect/cash", {"amount_received_paise": amount + 500}, h=rh)
    assert again.status_code == 200 and again.json()["already_recorded"] is True
    assert cafe.get(f"/admin/orders/{o['id']}", h=cafe.admin).json()["payment_status"] == "paid"
    assert due(cafe, aid, rh) == 0
    cafe.run_jobs()
    assert any(p["title"] == "Payment received" for p in cafe.push.sent)
    assert cafe.post(f"/deliveries/{aid}/collect/cash", {"amount_received_paise": 0}, h=rh).status_code == 422


def test_cash_ledger_summary_and_handover(cafe):
    rid, rh, o, ch, aid = to_door(cafe)
    amount = due(cafe, aid, rh)
    cafe.post(f"/deliveries/{aid}/collect/cash", {"amount_received_paise": amount}, h=rh)
    s = cafe.get("/admin/cash/summary", h=cafe.admin).json()
    row = next(r for r in s["riders"] if r["rider_id"] == rid)
    assert row["collected_paise"] == amount and row["outstanding_paise"] == amount and s["total_outstanding_paise"] == amount
    too_much = cafe.post(f"/admin/riders/{rid}/cash-handover", {"amount_paise": amount + 1}, h=cafe.admin)
    assert too_much.status_code == 409 and too_much.json()["error"]["code"] == "handover_exceeds_outstanding"
    part = cafe.post(f"/admin/riders/{rid}/cash-handover", {"amount_paise": 1000, "note": "evening"}, h=cafe.admin)
    assert part.status_code == 200 and part.json()["outstanding_paise"] == amount - 1000
    led = cafe.get(f"/admin/riders/{rid}/cash-ledger", h=cafe.admin).json()
    assert [e["entry_type"] for e in led["items"]] == ["handover", "collected"] and led["outstanding_paise"] == amount - 1000
    assert cafe.post(f"/admin/riders/{rid}/cash-handover", {"amount_paise": 1000}, h=rh).status_code == 403


def test_admin_reversing_rider_cash_updates_the_ledger(cafe):
    rid, rh, o, ch, aid = to_door(cafe)
    pid = cafe.post(f"/deliveries/{aid}/collect/cash", {"amount_received_paise": due(cafe, aid, rh)}, h=rh).json()["payment_id"]
    r = cafe.post(f"/admin/payments/{pid}/reverse-cash", {"reason": "customer disputed"}, h=cafe.admin)
    assert r.status_code == 200, r.text
    assert cafe.get(f"/admin/riders/{rid}/cash-ledger", h=cafe.admin).json()["outstanding_paise"] == 0
    assert cafe.get(f"/admin/orders/{o['id']}", h=cafe.admin).json()["payment_status"] == "unpaid"


def test_only_the_assigned_rider_can_collect(cafe):
    rid, rh, o, ch, aid = to_door(cafe)
    other = cafe.rider("R2", "9000000002")[1]
    amount = due(cafe, aid, rh)
    assert cafe.post(f"/deliveries/{aid}/collect/cash", {"amount_received_paise": amount}, h=other).status_code == 404
    assert cafe.post(f"/deliveries/{aid}/collect/upi-qr", h=other).status_code == 404
    assert cafe.get(f"/deliveries/{aid}/collect/status", h=other).status_code == 404
    assert cafe.post(f"/deliveries/{aid}/collect/cash", {"amount_received_paise": amount}, h=ch).status_code == 403


def test_collection_needs_the_final_leg_to_have_started(cafe):
    rid, rh = cafe.rider()
    h = cafe.customer("Cust", phone="9876543210")
    o = cafe.place("delivery", h=h).json()["order"]
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.advance(o["id"], "preparing", "ready")
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    cafe.post(f"/deliveries/{aid}/accept", h=rh)
    r = cafe.post(f"/deliveries/{aid}/collect/cash", {"amount_received_paise": 100000}, h=rh)
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state"


def test_qr_created_reused_and_scanning_alone_never_pays(cafe):
    rid, rh, o, ch, aid = to_door(cafe)
    amount = due(cafe, aid, rh)
    q = cafe.post(f"/deliveries/{aid}/collect/upi-qr", h=rh)
    assert q.status_code == 200, q.text
    assert q.json()["qr_image_url"].startswith("https://qr.test/") and q.json()["amount_paise"] == amount
    assert cafe.gw["razorpay"].qrs[0]["amount_paise"] == amount
    assert cafe.post(f"/deliveries/{aid}/collect/upi-qr", h=rh).json()["payment_id"] == q.json()["payment_id"]  # same open QR, no second code
    st = cafe.get(f"/deliveries/{aid}/collect/status", h=rh).json()
    assert st["payment_status"] != "paid" and st["amount_due_paise"] == amount and st["qr"]["payment_id"] == q.json()["payment_id"]
    otp = customer_otp(cafe, o["id"], ch)
    assert cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": otp}, h=rh).json()["error"]["code"] == "payment_not_collected"


def test_qr_payment_confirmed_by_verified_webhook(cafe):
    rid, rh, o, ch, aid = to_door(cafe)
    amount = due(cafe, aid, rh)
    cafe.post(f"/deliveries/{aid}/collect/upi-qr", h=rh)
    qr = qr_id_of(cafe, o["id"])
    bad = cafe.req("POST", "/webhooks/razorpay", h={"x-test-signature": "no", "content-type": "application/json"}, content=b"{}")
    assert bad.status_code == 401
    assert qr_webhook(cafe, qr, amount).status_code == 200
    st = cafe.get(f"/deliveries/{aid}/collect/status", h=rh).json()
    assert st["payment_status"] == "paid" and st["amount_due_paise"] == 0 and st["qr"] is None
    adm = cafe.get(f"/admin/orders/{o['id']}", h=cafe.admin).json()
    assert adm["payment_status"] == "paid" and adm["payment_method"] == "razorpay"
    pend = [p for p in cafe.get("/admin/payments/counter-pending", h=cafe.admin).json() if p.get("order_id") == o["id"] or p.get("order_number") == o.get("order_number")]
    assert pend == []  # the unpaid cash row was voided, the admin is not asked to chase it
    otp = customer_otp(cafe, o["id"], ch)
    assert cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": otp}, h=rh).json()["status"] == "delivered"


def test_qr_status_poll_catches_a_missed_webhook(cafe):
    from app.integrations.base import GatewayPaymentInfo
    rid, rh, o, ch, aid = to_door(cafe)
    amount = due(cafe, aid, rh)
    cafe.post(f"/deliveries/{aid}/collect/upi-qr", h=rh)
    cafe.gw["razorpay"].statuses[qr_id_of(cafe, o["id"])] = GatewayPaymentInfo(status="paid", gateway_payment_id="pay_missed", amount_paise=amount, currency="INR")
    assert cafe.get(f"/deliveries/{aid}/collect/status", h=rh).json()["payment_status"] == "paid"


def test_wrong_amount_on_a_qr_payment_is_flagged_not_accepted(cafe):
    rid, rh, o, ch, aid = to_door(cafe)
    amount = due(cafe, aid, rh)
    cafe.post(f"/deliveries/{aid}/collect/upi-qr", h=rh)
    qr_webhook(cafe, qr_id_of(cafe, o["id"]), amount - 100)
    assert cafe.get(f"/deliveries/{aid}/collect/status", h=rh).json()["payment_status"] != "paid"


def test_customer_scanning_after_rider_took_cash_is_refunded(cafe):
    rid, rh, o, ch, aid = to_door(cafe)
    amount = due(cafe, aid, rh)
    cafe.post(f"/deliveries/{aid}/collect/upi-qr", h=rh)
    cafe.post(f"/deliveries/{aid}/collect/cash", {"amount_received_paise": amount}, h=rh)  # customer changed their mind and paid cash
    qr_webhook(cafe, qr_id_of(cafe, o["id"]), amount, "evdup")
    cafe.run_jobs()
    assert cafe.gw["razorpay"].refunds and cafe.gw["razorpay"].refunds[0][1] == amount  # the duplicate online payment goes back automatically


def test_qr_unavailable_when_online_payment_is_off_or_order_prepaid(cafe):
    rid, rh, o, ch, aid = to_door(cafe)
    cafe.put("/admin/payment-gateways", {"razorpay_enabled": False}, h=cafe.admin)
    r = cafe.post(f"/deliveries/{aid}/collect/upi-qr", h=rh)
    assert r.status_code == 422 and r.json()["error"]["code"] == "qr_unavailable"
    cafe.post(f"/deliveries/{aid}/collect/cash", {"amount_received_paise": due(cafe, aid, rh)}, h=rh)
    cafe.put("/admin/payment-gateways", {"razorpay_enabled": True}, h=cafe.admin)
    assert cafe.post(f"/deliveries/{aid}/collect/upi-qr", h=rh).json()["error"]["code"] == "already_paid"


def test_cashfree_cannot_create_a_qr():
    import asyncio
    import pytest
    from app.integrations.base import GatewayError
    from app.integrations.cashfree.client import CashfreeGateway
    with pytest.raises(GatewayError):
        asyncio.run(CashfreeGateway().create_qr(reference="x", amount_paise=100, description="d", expires_at_epoch=1))


# ------------------------------------------------------------------ "rider is nearby"
def age_locations(cafe):
    from datetime import timedelta
    from sqlalchemy import update
    from app.core.security import now_utc
    from app.db.session import session_scope
    from app.models.delivery import RiderLocation

    async def go():
        async with session_scope() as db:
            await db.execute(update(RiderLocation).values(received_at=now_utc() - timedelta(minutes=1)))
    cafe.call(go)


def test_nearby_alert_is_sent_once_when_the_rider_reaches_the_pin(cafe):
    addr = {"line1": "12 MG Road", "postal_code": "560001", "latitude": 12.9716, "longitude": 77.5946}
    rid, rh, o, ch, aid = to_door(cafe, delivery_address=addr)
    pos = lambda lat, acc=6: {"assignment_id": aid, "latitude": lat, "longitude": 77.5946, "accuracy_m": acc}  # noqa: E731
    assert cafe.post("/tracking/location", pos(12.9816), h=rh).status_code == 200  # about 1.1 km away
    cafe.run_jobs()
    assert not [p for p in cafe.push.sent if p["title"] == "Your rider is nearby!"]
    age_locations(cafe)
    assert cafe.post("/tracking/location", pos(12.97162), h=rh).status_code == 200  # about 2 m away
    cafe.run_jobs()
    nearby = [p for p in cafe.push.sent if p["title"] == "Your rider is nearby!"]
    assert len(nearby) == 1 and nearby[0]["body"] == "Your Crescent Cafe delivery rider is nearby!"
    age_locations(cafe)
    cafe.post("/tracking/location", pos(12.97161), h=rh)
    cafe.run_jobs()
    assert len([p for p in cafe.push.sent if p["title"] == "Your rider is nearby!"]) == 1  # never repeated for the same arrival


def test_nearby_alert_ignores_imprecise_gps_and_orders_without_a_pin(cafe):
    addr = {"line1": "12 MG Road", "postal_code": "560001", "latitude": 12.9716, "longitude": 77.5946}
    rid, rh, o, ch, aid = to_door(cafe, delivery_address=addr)
    cafe.post("/tracking/location", {"assignment_id": aid, "latitude": 12.97162, "longitude": 77.5946, "accuracy_m": 400}, h=rh)
    cafe.run_jobs()
    assert not [p for p in cafe.push.sent if p["title"] == "Your rider is nearby!"]
    rid2, rh2, o2, ch2, aid2 = to_door(cafe, rider=("R2", "9000000002"))  # no coordinates on this address
    cafe.post("/tracking/location", {"assignment_id": aid2, "latitude": 12.9716, "longitude": 77.5946, "accuracy_m": 5}, h=rh2)
    cafe.run_jobs()
    assert not [p for p in cafe.push.sent if p["title"] == "Your rider is nearby!"]


def test_haversine_distance():
    from app.services.delivery import haversine_m
    assert haversine_m(12.9716, 77.5946, 12.9716, 77.5946) == 0
    assert 1090 < haversine_m(12.9716, 77.5946, 12.9816, 77.5946) < 1130
    assert 5 < haversine_m(12.9716, 77.5946, 12.97165, 77.5946) < 6


def test_rate_limit_key_ignores_a_spoofed_forwarded_header():
    from starlette.requests import Request
    from app.core.ratelimit import client_ip

    def req(xff):
        return Request({"type": "http", "headers": [(b"x-forwarded-for", xff.encode())], "client": ("5.5.5.5", 1)})
    assert client_ip(req("1.2.3.4, 9.9.9.9")) == client_ip(req("6.6.6.6, 9.9.9.9")) == "9.9.9.9"  # the proxy's own entry wins
    assert client_ip(Request({"type": "http", "headers": [], "client": ("5.5.5.5", 1)})) == "5.5.5.5"
