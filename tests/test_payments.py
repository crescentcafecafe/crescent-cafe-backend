import base64
import hashlib
import hmac
import json
import uuid
from datetime import timedelta

import pytest

from app.integrations.base import GatewayPaymentInfo


def online_order(cafe, gateway="razorpay", h=None, **kw):
    h = h or cafe.customer(f"P{uuid.uuid4().hex[:4]}", phone="9" + str(uuid.uuid4().int)[:9])
    r = cafe.place("pickup", payment=gateway, h=h, **kw)
    assert r.status_code == 201, r.text
    o = r.json()["order"]
    init = cafe.post(f"/payments/orders/{o['id']}/initiate", {"gateway": gateway}, h=h)
    assert init.status_code == 200, init.text
    return o, init.json(), h


def mark_paid(cafe, gateway, init, o, pid="pay_1", amount=None):
    cafe.gw[gateway].statuses[init["client_payload"]["order_id"]] = GatewayPaymentInfo(
        status="paid", gateway_payment_id=pid, amount_paise=amount or o["total_paise"], currency="INR")


def webhook(cafe, gateway, body, sig="ok"):
    return cafe.req("POST", f"/webhooks/{gateway}", h={"x-test-signature": sig, "content-type": "application/json"}, content=json.dumps(body).encode())


def order_status(cafe, oid):
    return cafe.get(f"/admin/orders/{oid}", h=cafe.admin).json()


def test_only_enabled_gateways_are_offered(cafe):
    assert set(cafe.get("/payments/methods").json()["methods"]) == {"razorpay", "cashfree", "cash"}
    cafe.put("/admin/payment-gateways", {"cashfree_enabled": False}, h=cafe.admin)
    assert set(cafe.get("/payments/methods").json()["methods"]) == {"razorpay", "cash"}
    r = cafe.place("pickup", payment="cashfree", h=cafe.customer())
    assert r.status_code == 422 and r.json()["error"]["code"] == "payment_method_unavailable"
    # an unconfigured gateway cannot be switched on, and at least one method must remain
    cafe.gw["cashfree"].configured = False
    assert cafe.put("/admin/payment-gateways", {"cashfree_enabled": True}, h=cafe.admin).json()["error"]["code"] == "gateway_not_configured"
    assert cafe.put("/admin/payment-gateways", {"razorpay_enabled": False, "cash_enabled": False}, h=cafe.admin).json()["error"]["code"] == "no_payment_method"
    g = cafe.get("/admin/payment-gateways", h=cafe.admin).json()
    assert "secret" not in json.dumps(g).lower()


def test_gateway_switches_admin_only(cafe):
    assert cafe.put("/admin/payment-gateways", {"razorpay_enabled": False}, h=cafe.customer()).status_code == 403


def test_razorpay_full_flow_server_verification(cafe):
    o, init, h = online_order(cafe, "razorpay")
    assert o["status"] == "payment_pending" and o["payment_status"] == "pending"
    assert set(init["client_payload"]) >= {"order_id", "amount"} and "secret" not in json.dumps(init)
    cafe.run_jobs()
    assert not [m for m in cafe.push.sent if m["data"]["type"] == "order.new"]  # admin is not alarmed before payment is verified
    oid = init["client_payload"]["order_id"]
    # bad checkout signature is rejected outright
    bad = cafe.post(f"/payments/orders/{o['id']}/verify/razorpay", {"razorpay_order_id": oid, "razorpay_payment_id": "pay_1", "razorpay_signature": "forged"}, h=h)
    assert bad.status_code == 401
    # good signature but the PROVIDER still says pending: a client success callback alone proves nothing
    good = {"razorpay_order_id": oid, "razorpay_payment_id": "pay_1", "razorpay_signature": "valid-signature"}
    r = cafe.post(f"/payments/orders/{o['id']}/verify/razorpay", good, h=h)
    assert r.status_code == 200 and r.json()["status"] == "pending" and order_status(cafe, o["id"])["status"] == "payment_pending"
    mark_paid(cafe, "razorpay", init, o)
    assert cafe.post(f"/payments/orders/{o['id']}/verify/razorpay", good, h=h).json()["status"] == "paid"
    d = order_status(cafe, o["id"])
    assert d["status"] == "payment_verified" and d["payment_status"] == "paid"
    p = d["payments"][0]
    assert p["gateway"] == "razorpay" and p["amount_paise"] == o["total_paise"]
    # duplicate confirmation is a no-op: no second transition, no second alarm
    cafe.post(f"/payments/orders/{o['id']}/verify/razorpay", good, h=h)
    cafe.post(f"/payments/orders/{o['id']}/sync", h=h)
    d = order_status(cafe, o["id"])
    assert [x["to"] for x in d["history"]].count("payment_verified") == 1
    from sqlalchemy import func, select
    from app.db.session import session_scope
    from app.models.notifications import NotificationEvent

    async def alarms():
        async with session_scope() as db:
            return (await db.execute(select(func.count()).select_from(NotificationEvent).where(NotificationEvent.type == "order.new"))).scalar_one()
    assert cafe.call(alarms) == 1  # exactly one admin alarm, created only after verified payment
    assert cafe.get("/admin/orders/pending", h=cafe.admin).json()[0]["id"] == o["id"]
    assert cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin).json()["status"] == "accepted"


def test_cashfree_flow_via_sync(cafe):
    o, init, h = online_order(cafe, "cashfree")
    assert init["client_payload"] == {"order_id": init["client_payload"]["order_id"], "amount": o["total_paise"], "currency": "INR"}
    assert cafe.post(f"/payments/orders/{o['id']}/sync", h=h).json()["order_status"] == "payment_pending"
    mark_paid(cafe, "cashfree", init, o, pid="cf_pay_9")
    r = cafe.post(f"/payments/orders/{o['id']}/sync", h=h).json()
    assert r["order_status"] == "payment_verified" and r["payment_status"] == "paid"
    assert order_status(cafe, o["id"])["payments"][0]["gateway"] == "cashfree"


def test_initiate_reuses_open_payment_and_blocks_wrong_states(cafe):
    o, init, h = online_order(cafe, "razorpay")
    again = cafe.post(f"/payments/orders/{o['id']}/initiate", {"gateway": "razorpay"}, h=h).json()
    assert again["payment_id"] == init["payment_id"]
    other = cafe.post(f"/payments/orders/{o['id']}/initiate", {"gateway": "cashfree"}, h=h).json()
    assert other["payment_id"] != init["payment_id"]  # customer may switch gateway
    cash = cafe.place("pickup", payment="cash", h=h).json()["order"]
    assert cafe.post(f"/payments/orders/{cash['id']}/initiate", {"gateway": "razorpay"}, h=h).json()["error"]["code"] == "not_payable"
    stranger = cafe.customer("S", phone="9444444444")
    assert cafe.post(f"/payments/orders/{o['id']}/initiate", {"gateway": "razorpay"}, h=stranger).status_code == 404


def test_webhook_signature_duplicates_and_amount_check(cafe):
    o, init, h = online_order(cafe, "razorpay")
    goid = init["client_payload"]["order_id"]
    ev = {"event_id": "evt_1", "type": "payment.captured", "order_id": goid, "status": "paid", "payment_id": "pay_77", "amount": o["total_paise"]}
    assert webhook(cafe, "razorpay", ev, sig="forged").status_code == 401
    assert order_status(cafe, o["id"])["status"] == "payment_pending"
    # wrong amount is recorded as a mismatch and NEVER marks the order paid
    bad = {**ev, "event_id": "evt_bad", "amount": 1}
    assert webhook(cafe, "razorpay", bad).status_code == 200
    d = order_status(cafe, o["id"])
    assert d["status"] == "payment_pending" and d["payments"][0]["status"] != "paid"
    assert webhook(cafe, "razorpay", ev).json()["status"] == "processed"
    assert order_status(cafe, o["id"])["status"] == "payment_verified"
    assert webhook(cafe, "razorpay", ev).json()["status"] == "duplicate"
    # a stale 'failed' event can never downgrade a paid payment
    fail = {"event_id": "evt_f", "type": "payment.failed", "order_id": goid, "status": "failed", "payment_id": "pay_78"}
    webhook(cafe, "razorpay", fail)
    assert order_status(cafe, o["id"])["payment_status"] == "paid"


def test_failed_payment_allows_retry_and_does_not_consume_coupon(cafe):
    a = cafe.admin
    cafe.post("/admin/offers", {"name": "Ten off", "discount_type": "percent", "value": 1000, "coupon_code": "TEN", "total_redemption_limit": 1}, h=a)
    h = cafe.customer()
    o = cafe.place("pickup", payment="razorpay", h=h, coupon_code="TEN").json()["order"]
    assert o["discount_paise"] == 1000
    init = cafe.post(f"/payments/orders/{o['id']}/initiate", {"gateway": "razorpay"}, h=h).json()
    webhook(cafe, "razorpay", {"event_id": "f1", "type": "payment.failed", "order_id": init["client_payload"]["order_id"], "status": "failed", "payment_id": "p1"})
    d = order_status(cafe, o["id"])
    assert d["status"] == "payment_pending" and d["payment_status"] == "failed"
    red = cafe.get(f"/admin/offers", h=a).json()[0]
    assert red["redemptions_consumed"] == 0
    # the reservation still holds the single redemption while payment can be retried...
    other = cafe.customer("O", phone="9555555555")
    assert cafe.place("pickup", h=other, coupon_code="TEN").json()["error"]["code"] in ("coupon_invalid", "coupon_exhausted")
    # ...and succeeding on retry consumes it
    mark_paid(cafe, "razorpay", init, o)
    cafe.post(f"/payments/orders/{o['id']}/sync", h=h)
    assert cafe.get("/admin/offers", h=a).json()[0]["redemptions_consumed"] == 1


def test_abandoned_payment_expires_order_and_releases_coupon(cafe):
    a = cafe.admin
    cafe.post("/admin/offers", {"name": "Ten off", "discount_type": "percent", "value": 1000, "coupon_code": "TEN", "total_redemption_limit": 1}, h=a)
    h = cafe.customer()
    o = cafe.place("pickup", payment="razorpay", h=h, coupon_code="TEN").json()["order"]
    cafe.post(f"/payments/orders/{o['id']}/initiate", {"gateway": "razorpay"}, h=h)
    from sqlalchemy import update
    from app.core.security import now_utc
    from app.db.session import session_scope
    from app.models.orders import Order
    from app.workers.handlers import _expire_unpaid

    async def age_and_expire():
        async with session_scope() as db:
            await db.execute(update(Order).values(created_at=now_utc() - timedelta(hours=2)))
        async with session_scope() as db:
            await _expire_unpaid(db, {})
    cafe.call(age_and_expire)
    d = order_status(cafe, o["id"])
    assert d["status"] == "cancelled" and d["cancellation_reason"] == "Payment not completed in time"
    assert cafe.get("/admin/offers", h=a).json()[0]["redemptions_consumed"] == 0
    other = cafe.customer("O", phone="9555555555")
    assert cafe.place("pickup", h=other, coupon_code="TEN").status_code == 201  # coupon is free again


def test_expiry_job_notices_a_payment_that_actually_succeeded(cafe):
    o, init, h = online_order(cafe, "razorpay")
    mark_paid(cafe, "razorpay", init, o)  # paid at the provider but the webhook never arrived
    from sqlalchemy import update
    from app.core.security import now_utc
    from app.db.session import session_scope
    from app.models.orders import Order
    from app.workers.handlers import _expire_unpaid

    async def go():
        async with session_scope() as db:
            await db.execute(update(Order).values(created_at=now_utc() - timedelta(hours=2)))
        async with session_scope() as db:
            await _expire_unpaid(db, {})
    cafe.call(go)
    assert order_status(cafe, o["id"])["status"] == "payment_verified"  # reconciled, not cancelled


def test_reconcile_job_picks_up_missed_webhook(cafe):
    o, init, h = online_order(cafe, "cashfree")
    mark_paid(cafe, "cashfree", init, o)
    from sqlalchemy import update
    from app.core.security import now_utc
    from app.db.session import session_scope
    from app.models.payments import Payment
    from app.workers.handlers import _reconcile

    async def go():
        async with session_scope() as db:
            await db.execute(update(Payment).values(created_at=now_utc() - timedelta(minutes=10)))
        async with session_scope() as db:
            await _reconcile(db, {})
    cafe.call(go)
    assert order_status(cafe, o["id"])["payment_status"] == "paid"


def test_double_payment_via_two_gateways_is_auto_refunded(cafe):
    o, init1, h = online_order(cafe, "razorpay")
    init2 = cafe.post(f"/payments/orders/{o['id']}/initiate", {"gateway": "cashfree"}, h=h).json()
    mark_paid(cafe, "razorpay", init1, o, "pay_a")
    cafe.post(f"/payments/orders/{o['id']}/sync", h=h)
    mark_paid(cafe, "cashfree", init2, o, "pay_b")
    cafe.post(f"/payments/orders/{o['id']}/sync", h=h)
    cafe.run_jobs()
    refunds = cafe.get("/admin/refunds", h=cafe.admin).json()
    assert len(refunds) == 1 and refunds[0]["amount_paise"] == o["total_paise"] and refunds[0]["reason"] == "Duplicate payment"
    assert cafe.gw["cashfree"].refunds and not cafe.gw["razorpay"].refunds


# ------------------------------------------------------------------ real signature primitives (no network)
def test_real_razorpay_signatures_and_webhook(cafe):
    from app.integrations import registry
    from app.integrations.razorpay.client import RazorpayGateway
    registry.override("gw:razorpay", None)
    registry._cache.pop("gw:razorpay", None)
    gw = RazorpayGateway()
    sig = hmac.new(b"rzp_secret", b"order_X|pay_Y", hashlib.sha256).hexdigest()
    assert gw.verify_checkout_signature(razorpay_order_id="order_X", razorpay_payment_id="pay_Y", razorpay_signature=sig)
    assert not gw.verify_checkout_signature(razorpay_order_id="order_X", razorpay_payment_id="pay_Z", razorpay_signature=sig)
    # webhook through the HTTP endpoint with a real HMAC and real payload parsing
    h = cafe.customer()
    o = cafe.place("pickup", payment="razorpay", h=h).json()["order"]
    from app.db.session import session_scope
    from app.models.payments import Payment
    from app.models.orders import Order

    async def mk():
        async with session_scope() as db:
            order = await db.get(Order, uuid.UUID(o["id"]))
            p = Payment(order_id=order.id, branch_id=order.branch_id, method="online", gateway="razorpay", gateway_order_id="order_REAL1",
                        amount_paise=order.total_paise, currency="INR", status="pending")
            db.add(p)
    cafe.call(mk)
    body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_REAL", "order_id": "order_REAL1", "amount": o["total_paise"],
                                                                                    "currency": "INR", "status": "captured"}}}}).encode()
    good = hmac.new(b"rzp_whsec", body, hashlib.sha256).hexdigest()
    assert cafe.req("POST", "/webhooks/razorpay", h={"X-Razorpay-Signature": "00", "X-Razorpay-Event-Id": "ev1"}, content=body).status_code == 401
    r = cafe.req("POST", "/webhooks/razorpay", h={"X-Razorpay-Signature": good, "X-Razorpay-Event-Id": "ev1"}, content=body)
    assert r.status_code == 200 and r.json()["status"] == "processed"
    assert order_status(cafe, o["id"])["payment_status"] == "paid"
    assert cafe.req("POST", "/webhooks/razorpay", h={"X-Razorpay-Signature": good, "X-Razorpay-Event-Id": "ev1"}, content=body).json()["status"] == "duplicate"


def test_real_cashfree_signature_and_webhook(cafe):
    from app.integrations import registry
    registry.override("gw:cashfree", None)
    registry._cache.pop("gw:cashfree", None)
    h = cafe.customer()
    o = cafe.place("pickup", payment="cashfree", h=h).json()["order"]
    from app.db.session import session_scope
    from app.models.payments import Payment
    from app.models.orders import Order

    async def mk():
        async with session_scope() as db:
            order = await db.get(Order, uuid.UUID(o["id"]))
            db.add(Payment(order_id=order.id, branch_id=order.branch_id, method="online", gateway="cashfree", gateway_order_id="cfORDER1",
                           amount_paise=order.total_paise, currency="INR", status="pending"))
    cafe.call(mk)
    rupees = o["total_paise"] / 100
    body = json.dumps({"type": "PAYMENT_SUCCESS_WEBHOOK", "data": {"order": {"order_id": "cfORDER1", "order_amount": rupees, "order_currency": "INR"},
                                                                  "payment": {"cf_payment_id": 555, "payment_status": "SUCCESS", "payment_amount": rupees, "payment_currency": "INR"}}}).encode()
    ts = "1700000000"
    sig = base64.b64encode(hmac.new(b"cf_secret", ts.encode() + body, hashlib.sha256).digest()).decode()
    assert cafe.req("POST", "/webhooks/cashfree", h={"x-webhook-signature": "AAAA", "x-webhook-timestamp": ts}, content=body).status_code == 401
    r = cafe.req("POST", "/webhooks/cashfree", h={"x-webhook-signature": sig, "x-webhook-timestamp": ts}, content=body)
    assert r.status_code == 200, r.text
    assert order_status(cafe, o["id"])["payment_status"] == "paid"
    assert cafe.req("POST", "/webhooks/cashfree", h={"x-webhook-signature": sig, "x-webhook-timestamp": ts}, content=body).json()["status"] == "duplicate"


# ------------------------------------------------------------------ cash at counter
def cash_payment(cafe, oid):
    return order_status(cafe, oid)["payments"][0]


def test_cash_requires_admin_confirmation(cafe):
    h = cafe.customer()
    o = cafe.place("dine_in", payment="cash", h=h).json()["order"]
    assert o["payment_status"] == "unpaid"
    pend = cafe.get("/admin/payments/counter-pending", h=cafe.admin).json()
    assert len(pend) == 1 and pend[0]["order_id"] == o["id"] and pend[0]["table_label"].startswith("Table 1")
    pid = pend[0]["id"]
    assert cafe.post(f"/admin/payments/{pid}/confirm-cash", {"amount_received_paise": 500}, h=cafe.admin).json()["error"]["code"] == "insufficient_amount"
    assert cafe.post(f"/admin/payments/{pid}/confirm-cash", {"amount_received_paise": 20000}, h=h).status_code == 403
    r = cafe.post(f"/admin/payments/{pid}/confirm-cash", {"amount_received_paise": 20000}, h=cafe.admin).json()
    assert r["status"] == "paid" and r["change_paise"] == 20000 - o["total_paise"] and r["confirmed_by_admin_id"] and r["paid_at"]
    dup = cafe.post(f"/admin/payments/{pid}/confirm-cash", {"amount_received_paise": 20000}, h=cafe.admin)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "already_confirmed"
    assert cafe.get("/admin/payments/counter-pending", h=cafe.admin).json() == []
    assert cafe.get(f"/orders/{o['id']}", h=h).json()["payment_status"] == "paid"


def test_cash_not_marked_paid_automatically_even_when_completed(cafe):
    o = cafe.walk_in().json()["order"]
    cafe.advance(o["id"], "preparing", "ready", "completed")
    assert order_status(cafe, o["id"])["payment_status"] == "unpaid"


def test_cash_reversal_is_audited(cafe):
    o = cafe.walk_in().json()["order"]
    pid = cash_payment(cafe, o["id"])["id"]
    cafe.post(f"/admin/payments/{pid}/confirm-cash", {"amount_received_paise": o["total_paise"]}, h=cafe.admin)
    assert cafe.post(f"/admin/payments/{pid}/reverse-cash", {"reason": "x"}, h=cafe.admin).status_code == 422
    r = cafe.post(f"/admin/payments/{pid}/reverse-cash", {"reason": "Entered wrong order by mistake"}, h=cafe.admin)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert order_status(cafe, o["id"])["payment_status"] == "unpaid"
    logs = cafe.get("/admin/audit-logs?entity_type=payment", h=cafe.admin).json()
    assert {"payment.cash_confirmed", "payment.cash_reversed"} <= {l["action"] for l in logs}
    rev = [l for l in logs if l["action"] == "payment.cash_reversed"][0]
    assert rev["before"]["status"] == "paid" and rev["after"]["reason"] == "Entered wrong order by mistake"
    assert cafe.post(f"/admin/payments/{pid}/reverse-cash", {"reason": "again please"}, h=cafe.admin).status_code == 409
    assert cafe.post(f"/admin/payments/{pid}/confirm-cash", {"amount_received_paise": o["total_paise"]}, h=cafe.admin).status_code == 200  # can be re-confirmed


# ------------------------------------------------------------------ refunds
def test_rejected_paid_online_order_refund_waits_for_provider(cafe):
    o, init, h = online_order(cafe, "razorpay")
    mark_paid(cafe, "razorpay", init, o)
    cafe.post(f"/payments/orders/{o['id']}/sync", h=h)
    r = cafe.post(f"/admin/orders/{o['id']}/reject", {"reason": "Cannot fulfil"}, h=cafe.admin).json()
    assert r["status"] == "refund_pending"
    cafe.run_jobs()
    refunds = cafe.get("/admin/refunds", h=cafe.admin).json()
    assert len(refunds) == 1 and refunds[0]["status"] == "pending" and refunds[0]["gateway_refund_id"] == "rf_1"
    assert order_status(cafe, o["id"])["status"] == "refund_pending"  # NOT refunded until the provider confirms
    msgs = [m for m in cafe.push.sent if m["data"]["type"] == "refund.succeeded"]
    assert not msgs
    ok = webhook(cafe, "razorpay", {"event_id": "r1", "type": "refund.processed", "refund_status": "succeeded", "gateway_refund_id": "rf_1", "refund_ref": refunds[0]["id"]})
    assert ok.status_code == 200
    d = order_status(cafe, o["id"])
    assert d["status"] == "refunded" and d["payment_status"] == "refunded" and d["payments"][0]["refunded_paise"] == o["total_paise"]


def test_failed_refund_can_be_retried(cafe):
    o, init, h = online_order(cafe, "cashfree")
    mark_paid(cafe, "cashfree", init, o)
    cafe.post(f"/payments/orders/{o['id']}/sync", h=h)
    cafe.post(f"/admin/orders/{o['id']}/cancel", {"reason": "kitchen fire"}, h=cafe.admin)
    cafe.run_jobs()
    rid = cafe.get("/admin/refunds", h=cafe.admin).json()[0]["id"]
    webhook(cafe, "cashfree", {"event_id": "rf-fail", "type": "REFUND_STATUS_WEBHOOK", "refund_status": "failed", "gateway_refund_id": "rf_1", "refund_ref": rid})
    assert cafe.get("/admin/refunds?status=failed", h=cafe.admin).json()[0]["id"] == rid
    cafe.gw["cashfree"].refund_status = "succeeded"
    assert cafe.post(f"/admin/refunds/{rid}/retry", h=cafe.admin).json()["status"] == "pending"
    cafe.run_jobs()
    assert order_status(cafe, o["id"])["status"] == "refunded"


def test_partial_refund_on_completed_order_and_limits(cafe):
    o, init, h = online_order(cafe, "razorpay", kind="latte", qty=2)
    mark_paid(cafe, "razorpay", init, o)
    cafe.post(f"/payments/orders/{o['id']}/sync", h=h)
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.advance(o["id"], "preparing", "ready", "completed")
    pid = order_status(cafe, o["id"])["payments"][0]["id"]
    r = cafe.post(f"/admin/payments/{pid}/refunds", {"amount_paise": 5000, "reason": "Cold coffee"}, h={**cafe.admin, "Idempotency-Key": "rf-1"})
    assert r.status_code == 201 and r.json()["status"] == "pending"
    again = cafe.post(f"/admin/payments/{pid}/refunds", {"amount_paise": 5000, "reason": "Cold coffee"}, h={**cafe.admin, "Idempotency-Key": "rf-1"})
    assert again.json()["id"] == r.json()["id"]  # idempotent
    over = cafe.post(f"/admin/payments/{pid}/refunds", {"amount_paise": o["total_paise"], "reason": "too much"}, h=cafe.admin)
    assert over.status_code == 409 and over.json()["error"]["code"] == "refund_exceeds_balance"
    cafe.run_jobs()
    webhook(cafe, "razorpay", {"event_id": "pr1", "type": "refund.processed", "refund_status": "succeeded", "gateway_refund_id": "rf_1", "refund_ref": r.json()["id"]})
    d = order_status(cafe, o["id"])
    assert d["status"] == "completed" and d["payment_status"] == "partially_refunded" and d["payments"][0]["refunded_paise"] == 5000


def test_cash_refund_needs_admin_to_confirm_handing_back(cafe):
    o = cafe.walk_in().json()["order"]
    pid = cash_payment(cafe, o["id"])["id"]
    cafe.post(f"/admin/payments/{pid}/confirm-cash", {"amount_received_paise": o["total_paise"]}, h=cafe.admin)
    cafe.post(f"/admin/orders/{o['id']}/cancel", {"reason": "customer left"}, h=cafe.admin)
    d = order_status(cafe, o["id"])
    assert d["status"] == "refund_pending"
    rf = cafe.get("/admin/refunds", h=cafe.admin).json()[0]
    assert rf["method"] == "cash" and rf["status"] == "pending"
    assert cafe.post(f"/admin/refunds/{rf['id']}/confirm-cash", h=cafe.admin).json()["status"] == "succeeded"
    assert order_status(cafe, o["id"])["status"] == "refunded"
    assert cafe.post(f"/admin/refunds/{rf['id']}/confirm-cash", h=cafe.admin).status_code == 409


def test_late_payment_after_cancellation_triggers_refund(cafe):
    o, init, h = online_order(cafe, "razorpay")
    cafe.post(f"/orders/{o['id']}/cancel", {"reason": "oops"}, h=h)
    mark_paid(cafe, "razorpay", init, o)
    cafe.post(f"/payments/orders/{o['id']}/sync", h=h)  # money arrives for an already cancelled order
    d = order_status(cafe, o["id"])
    assert d["status"] == "refund_pending"
    cafe.run_jobs()
    assert cafe.gw["razorpay"].refunds
