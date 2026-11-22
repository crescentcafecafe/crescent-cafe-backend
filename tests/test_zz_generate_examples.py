"""Writes docs/EXAMPLES.md from REAL responses. Skipped unless GEN_EXAMPLES=1:   GEN_EXAMPLES=1 pytest tests/test_zz_generate_examples.py"""
import json
import os
import re

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("GEN_EXAMPLES") != "1", reason="docs generator")


def clip(o, depth=0):
    if isinstance(o, dict):
        return {k: clip(v, depth + 1) for k, v in list(o.items())[:30]}
    if isinstance(o, list):
        return [clip(v, depth + 1) for v in o[:2]] + (["…"] if len(o) > 2 else [])
    if isinstance(o, str) and len(o) > 60 and re.match(r"^[A-Za-z0-9_\-\.]{60,}$", o):
        return o[:18] + "…"
    return o


def test_generate(cafe):
    out = ["# Example requests & responses", "",
           "Captured from a real run of the test-suite scenario (fake payment/FCM providers). IDs, timestamps and tokens differ in your environment. Amounts are paise. `$BASE` = your API URL, `$ADMIN`/`$CUST`/`$RIDER` = `Authorization: Bearer …` headers.", ""]

    def ex(title, method, path, body, r, note=""):
        try:
            data = clip(r.json())
        except Exception:
            data = r.text[:200]
        out.append(f"## {title}\n")
        if note:
            out.append(note + "\n")
        out.append("```http\n" + f"{method} /api/v1{path}" + ("\nContent-Type: application/json" if body is not None else "") + "\n```")
        if body is not None:
            out.append("```json\n" + json.dumps(clip(body), indent=2, ensure_ascii=False) + "\n```")
        out.append(f"Response `{r.status_code}`:\n\n```json\n" + json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n```\n")
        return r

    r = ex("Admin login", "POST", "/auth/admin/login", {"email": "owner@example.com", "password": "••••••••••"},
           cafe.post("/auth/admin/login", {"email": cafe.admin_email, "password": cafe.admin_password}))
    ids = cafe.ids
    a = cafe.admin
    ex("Add a new menu item (variants + add-ons)", "POST", "/admin/menu/items",
       {"category_id": ids["cat"], "name": "Cappuccino", "price_paise": 18000, "variants": [{"name": "Regular", "price_paise": 18000}, {"name": "Large", "price_paise": 23000}], "addons": [{"name": "Extra shot", "price_paise": 4000, "max_quantity": 2}]},
       cafe.post("/admin/menu/items", {"category_id": ids["cat"], "name": "Cappuccino", "price_paise": 18000, "variants": [{"name": "Regular", "price_paise": 18000}, {"name": "Large", "price_paise": 23000}], "addons": [{"name": "Extra shot", "price_paise": 4000, "max_quantity": 2}]}, h=a),
       "Then upload the photo with `POST /admin/menu/items/{id}/image` (multipart field `file`).")
    ex("Customer registration", "POST", "/auth/customer/register", {"name": "Asha", "email": "asha@example.com", "password": "••••••••••••", "phone": "98765 43210"},
       cafe.post("/auth/customer/register", {"name": "Asha", "email": "asha@example.com", "password": "Passw0rdPass1", "phone": "98765 43210"}))
    ex("Google (Gmail) sign-in", "POST", "/auth/customer/google", {"id_token": "<Google ID token from the Android/web SDK>"},
       cafe.post("/auth/customer/google", {"id_token": "good-meera"}), "`phone_required: true` means the app must ask for the mobile number and call `PUT /customers/me/phone` before the first order.")
    ex("Resolve a scanned table QR", "POST", "/qr/resolve", {"token": "<token from the QR url /t/{token}>"}, cafe.post("/qr/resolve", {"token": ids["qr"]}))
    tok = cafe.table_token()
    cart = cafe.cart("latte", 2)
    ex("Server-side price quote", "POST", "/orders/quote", {"order_type": "dine_in", "items": cart}, cafe.post("/orders/quote", {"order_type": "dine_in", "items": cart}))
    cafe.post("/admin/offers", {"name": "Ten off", "discount_type": "percent", "value": 1000, "max_discount_paise": 5000, "coupon_code": "TEN"}, h=a)
    ex("Validate a coupon (valid)", "POST", "/coupons/validate", {"order_type": "dine_in", "items": cart, "coupon_code": "TEN"}, cafe.post("/coupons/validate", {"order_type": "dine_in", "items": cart, "coupon_code": "TEN"}))
    ex("Validate a coupon (explains why not)", "POST", "/coupons/validate", {"order_type": "dine_in", "items": cart, "coupon_code": "WRONG"}, cafe.post("/coupons/validate", {"order_type": "dine_in", "items": cart, "coupon_code": "WRONG"}))
    body = {"order_type": "dine_in", "payment_method": "cash", "source": "qr_web", "table_session_token": tok, "items": cart, "coupon_code": "TEN", "customer_name": "Ravi", "customer_phone": "9812345678", "special_instructions": "Less sugar"}
    r = ex("Place a guest dine-in order (QR website)", "POST", "/orders", body, cafe.post("/orders", body, h={"Idempotency-Key": "9f1c-example"}),
           "Header `Idempotency-Key` makes retries safe. `guest_token` is returned once: store it and send it as `X-Guest-Token` to track the order.")
    oid = r.json()["order"]["id"]
    ex("Admin accepts the order (stops the alarm)", "POST", f"/admin/orders/{{id}}/accept", None, cafe.post(f"/admin/orders/{oid}/accept", h=a))
    pay = cafe.get(f"/admin/payments/counter-pending", h=a).json()[0]
    ex("Admin confirms cash at the counter", "POST", "/admin/payments/{id}/confirm-cash", {"amount_received_paise": 50000}, cafe.post(f"/admin/payments/{pay['id']}/confirm-cash", {"amount_received_paise": 50000}, h=a))
    ex("Invalid transition is rejected safely", "POST", "/admin/orders/{id}/status", {"status": "ready"}, cafe.post(f"/admin/orders/{oid}/status", {"status": "ready"}, h=a))
    ch = cafe.customer("Online", phone="9000011111")
    o2 = cafe.place("pickup", payment="razorpay", h=ch, kind="cake").json()["order"]
    ex("Start an online payment (Razorpay)", "POST", f"/payments/orders/{{id}}/initiate", {"gateway": "razorpay"}, cafe.post(f"/payments/orders/{o2['id']}/initiate", {"gateway": "razorpay"}, h=ch),
       "Only SDK-safe fields are returned. Success is confirmed server-side (webhook / provider fetch), never by the client callback.")
    rid, rh = cafe.rider()
    ho = cafe.customer("Dee", phone="9000022222")
    cafe.post("/notifications/devices", {"install_id": "dee-phone-1", "fcm_token": "F" * 40}, h=ho)
    d = cafe.place("delivery", h=ho).json()["order"]
    cafe.post(f"/admin/orders/{d['id']}/accept", h=a)
    cafe.advance(d["id"], "preparing", "ready")
    asg = ex("Admin manually assigns a rider", "POST", "/admin/orders/{id}/assign", {"rider_id": rid}, cafe.post(f"/admin/orders/{d['id']}/assign", {"rider_id": rid}, h=a))
    aid = asg.json()["assignment_id"]
    ex("Rider accepts", "POST", "/deliveries/{assignment_id}/accept", None, cafe.post(f"/deliveries/{aid}/accept", h=rh))
    cafe.post(f"/deliveries/{aid}/picked-up", h=rh)
    cafe.post(f"/deliveries/{aid}/out-for-delivery", h=rh)
    due = cafe.get(f"/deliveries/{aid}", h=rh).json()["order"]["collect_cash_paise"]
    if due:
        ex("Rider records the cash received", "POST", "/deliveries/{assignment_id}/collect/cash", {"amount_received_paise": due}, cafe.post(f"/deliveries/{aid}/collect/cash", {"amount_received_paise": due}, h=rh))
    ex("Rider sends GPS", "POST", "/tracking/location", {"assignment_id": aid, "latitude": 12.9716, "longitude": 77.5946, "accuracy_m": 8.5}, cafe.post("/tracking/location", {"assignment_id": aid, "latitude": 12.9716, "longitude": 77.5946, "accuracy_m": 8.5}, h=rh))
    ex("Customer tracks the delivery", "GET", "/tracking/orders/{order_id}", None, cafe.get(f"/tracking/orders/{d['id']}", h=ho))
    otp = cafe.post(f"/deliveries/orders/{d['id']}/otp", h=ho)
    ex("Customer reveals / regenerates the delivery OTP", "POST", "/deliveries/orders/{order_id}/otp", None, otp)
    wrong = "000000" if otp.json()["otp"] != "000000" else "111111"
    ex("Rider enters a wrong OTP", "POST", "/deliveries/{assignment_id}/verify-otp", {"otp": wrong}, cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": wrong}, h=rh))
    ex("Rider enters the correct OTP: delivered", "POST", "/deliveries/{assignment_id}/verify-otp", {"otp": "<6-digit code from the customer>"}, cafe.post(f"/deliveries/{aid}/verify-otp", {"otp": otp.json()["otp"]}, h=rh))
    ex("Register an FCM device token", "POST", "/notifications/devices", {"install_id": "dee-phone-1", "fcm_token": "<FCM token>", "device_model": "Pixel 8", "app_version": "1.0.0"}, cafe.post("/notifications/devices", {"install_id": "dee-phone-1", "fcm_token": "F" * 40, "device_model": "Pixel 8", "app_version": "1.0.0"}, h=ho))
    cid = cafe.post("/chat/conversations", {"order_id": d["id"]}, h=ho).json()["id"]
    ex("Customer sends a chat message", "POST", "/chat/conversations/{id}/messages", {"body": "Please ring the bell", "client_msg_id": "c-1"}, cafe.post(f"/chat/conversations/{cid}/messages", {"body": "Please ring the bell", "client_msg_id": "c-1"}, h=ho))
    ex("Admin inbox", "GET", "/chat/conversations", None, cafe.get("/chat/conversations", h=a))
    ex("Invoice data", "GET", "/invoices/order/{order_id}", None, cafe.get(f"/invoices/order/{d['id']}", h=ho))
    ex("Daily/monthly report", "GET", "/admin/reports/summary?granularity=day", None, cafe.get("/admin/reports/summary", h=a))
    ex("Admin dashboard", "GET", "/admin/dashboard", None, cafe.get("/admin/dashboard", h=a))
    ex("Generate a table QR (shown once)", "POST", "/admin/tables/{id}/qr", None, cafe.post(f"/admin/tables/{ids['table']}/qr", h=a))
    ex("Standard error format", "GET", "/orders/00000000-0000-0000-0000-000000000000", None, cafe.get("/orders/00000000-0000-0000-0000-000000000000", h=ch))
    os.makedirs("docs", exist_ok=True)
    open("docs/EXAMPLES.md", "w").write("\n".join(out))
