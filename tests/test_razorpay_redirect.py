"""Razorpay redirect mode (used by the Android WebView apps): POST /payments/razorpay/callback/{order_id}."""
from urllib.parse import parse_qs, urlparse

from tests.test_payments import mark_paid, online_order, order_status

FORM = {"content-type": "application/x-www-form-urlencoded"}


def _post(cafe, oid, data):
    return cafe.req("POST", f"/payments/razorpay/callback/{oid}", h=FORM, data=data, follow_redirects=False)


def _where(r):
    u = urlparse(r.headers["location"])
    return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}


def test_success_callback_marks_paid_and_redirects_to_order(cafe):
    o, init, _ = online_order(cafe, "razorpay")
    mark_paid(cafe, "razorpay", init, o)
    r = _post(cafe, o["id"], {"razorpay_order_id": init["client_payload"]["order_id"], "razorpay_payment_id": "pay_1", "razorpay_signature": "valid-signature"})
    assert r.status_code == 303                       # 303: Razorpay's POST becomes a GET of our page
    path, q = _where(r)
    assert path == f"/orders/{o['id']}" and q["payment"] == "paid"
    assert order_status(cafe, o["id"])["payment_status"] == "paid"


def test_callback_with_good_signature_but_provider_pending_is_not_paid(cafe):
    o, init, _ = online_order(cafe, "razorpay")
    r = _post(cafe, o["id"], {"razorpay_order_id": init["client_payload"]["order_id"], "razorpay_payment_id": "pay_1", "razorpay_signature": "valid-signature"})
    assert _where(r)[1]["payment"] == "pending"
    assert order_status(cafe, o["id"])["payment_status"] != "paid"


def test_forged_signature_never_pays(cafe):
    o, init, _ = online_order(cafe, "razorpay")
    mark_paid(cafe, "razorpay", init, o)              # even if Razorpay says paid, a forged callback is rejected
    r = _post(cafe, o["id"], {"razorpay_order_id": init["client_payload"]["order_id"], "razorpay_payment_id": "pay_1", "razorpay_signature": "forged"})
    assert r.status_code == 303 and _where(r)[1]["payment"] == "error"
    assert order_status(cafe, o["id"])["payment_status"] != "paid"


def test_failed_and_cancelled_callbacks_show_reason(cafe):
    o, init, _ = online_order(cafe, "razorpay")
    r = _post(cafe, o["id"], {"error[code]": "BAD_REQUEST_ERROR", "error[description]": "Payment failed because the card was declined"})
    path, q = _where(r)
    assert q["payment"] == "failed" and "declined" in q["reason"] and path == f"/orders/{o['id']}"
    r = _post(cafe, o["id"], {"error[description]": "Payment processing cancelled by user"})
    assert _where(r)[1]["payment"] == "cancelled"


def test_unknown_order_and_junk_bodies_redirect_safely(cafe):
    import uuid
    r = _post(cafe, uuid.uuid4(), {"razorpay_payment_id": "x"})
    assert r.status_code == 303 and _where(r)[1]["payment"] == "error"
    o, _, _ = online_order(cafe, "razorpay")
    r = cafe.req("POST", f"/payments/razorpay/callback/{o['id']}", h={"content-type": "application/json"}, content=b"\xff\xfe not a form", follow_redirects=False)
    assert r.status_code == 303                       # never a 500 for a customer who just paid
    r = cafe.req("GET", f"/payments/razorpay/callback/{o['id']}?error_description=cancelled", follow_redirects=False)
    assert r.status_code == 303 and _where(r)[1]["payment"] == "cancelled"
