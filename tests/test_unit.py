import hashlib
import hmac

import jwt
import pytest

from app.core import security
from app.core.config import Settings, normalize_database_url
from app.core.errors import Invalid
from app.services import offers as offer_svc, pricing
from app.services.order_state import TRANSITIONS
from app.utils.phone import normalize_phone
from app.utils.timeutil import local_day_bounds, range_bounds


@pytest.mark.parametrize("raw,expected", [("9876543210", "+919876543210"), ("98765 43210", "+919876543210"), ("+91 98765-43210", "+919876543210"),
                                           ("09876543210", "+919876543210"), ("919876543210", "+919876543210"), ("+14155552671", "+14155552671"),
                                           ("0014155552671", "+14155552671")])
def test_phone_normalisation(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "12", "abcdefghij", "+0123456789", "12345"])
def test_phone_rejects_garbage(raw):
    with pytest.raises(Invalid):
        normalize_phone(raw)


def test_password_strength_rules():
    assert security.validate_password_strength("short1A") is not None
    assert security.validate_password_strength("alllowercase123") is not None
    assert security.validate_password_strength("NoDigitsHereAtAll") is not None
    assert security.validate_password_strength("GoodPassw0rd!") is None


def test_argon2_hashing_and_timing_safe_unknown_user():
    h = security.hash_password("GoodPassw0rd!")
    assert h.startswith("$argon2id$") and security.verify_password("GoodPassw0rd!", h)
    assert not security.verify_password("wrong", h) and not security.verify_password("x", None) and not security.verify_password("x", "not-a-hash")
    assert security.hash_password("GoodPassw0rd!") != h  # salted


def test_token_type_confusion_and_tampering():
    tok, _ = security.create_access_token(subject="u1", role="admin", session_id="s1")
    assert security.decode_token(tok, "access")["role"] == "admin"
    with pytest.raises(jwt.PyJWTError):
        security.decode_token(tok, "table_session")  # an access token can never act as a table-session token
    scoped = security.create_scoped_token({"sub": "x", "typ": "table_session"}, 5)
    with pytest.raises(jwt.PyJWTError):
        security.decode_token(scoped, "access")
    with pytest.raises(jwt.PyJWTError):
        security.decode_token(tok[:-3] + "abc", "access")
    expired = security.create_scoped_token({"sub": "x", "typ": "access"}, -1)
    with pytest.raises(jwt.PyJWTError):
        security.decode_token(expired, "access")
    import base64, json, time

    def b64(d): return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': 'x', 'role': 'admin', 'sid': 'y', 'typ': 'access', 'exp': int(time.time()) + 600})}."
    with pytest.raises(jwt.PyJWTError):  # unsigned 'alg: none' tokens are refused
        security.decode_token(forged, "access")


def test_otp_generation_and_hashing():
    otps = {security.new_numeric_otp() for _ in range(200)}
    assert all(len(o) == 6 and o.isdigit() for o in otps) and len(otps) > 150
    a, b = security.hash_otp("123456", "salt1"), security.hash_otp("123456", "salt2")
    assert a != b and a != "123456" and len(a) == 64
    assert security.consteq(a, security.hash_otp("123456", "salt1"))


def test_tokens_are_high_entropy_and_hashed():
    t = {security.new_token() for _ in range(100)}
    assert len(t) == 100 and all(len(x) >= 40 for x in t)
    assert security.hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


def test_tax_math():
    assert pricing.tax_exclusive(10000, 500) == 500
    assert pricing.tax_exclusive(333, 500) == 17  # 16.65 rounds half up
    assert pricing.tax_part_inclusive(10500, 500) == 500
    assert pricing.tax_part_inclusive(10000, 500) == 476
    assert pricing.tax_part_inclusive(10000, 0) == 0 and pricing.tax_exclusive(10000, 0) == 0


def test_discount_allocation_sums_exactly():
    for total, amounts in ((100, [333, 333, 334]), (1, [1, 1, 1]), (999, [1000, 1]), (7, [5, 5])):
        lines = [offer_svc.CartLine(key=i, item_id=None, category_id=None, subtotal_paise=a) for i, a in enumerate(amounts)]
        alloc = offer_svc._allocate(total, lines)
        assert sum(alloc.values()) == total and all(v >= 0 for v in alloc.values())
    assert offer_svc.half_up_pct(333, 1500) == 50


def test_state_machine_invariants():
    for kind, table in TRANSITIONS.items():
        for src, dsts in table.items():
            assert src in table and dsts <= set(table), (kind, src)
        assert table["completed"] == set() and table["refunded"] == set()
        assert all("delivered" not in d for s, d in table.items() if kind != "delivery")
        # nothing can move straight to 'delivered' except out_for_delivery, and delivered is terminal
        assert [s for s, d in TRANSITIONS["delivery"].items() if "delivered" in d] == ["out_for_delivery"]
    assert TRANSITIONS["delivery"]["delivered"] == set()
    assert "payment_verified" not in TRANSITIONS["delivery"]["placed"]  # cash orders skip payment states


def test_database_url_normalisation():
    assert normalize_database_url("postgres://u:p@h:5432/d") == "postgresql+asyncpg://u:p@h:5432/d"
    assert normalize_database_url("postgresql://u:p@h/d") == "postgresql+asyncpg://u:p@h/d"
    assert normalize_database_url("postgresql+asyncpg://u:p@h/d") == "postgresql+asyncpg://u:p@h/d"
    assert normalize_database_url("sqlite+aiosqlite:///x.db") == "sqlite+aiosqlite:///x.db"


def test_production_config_checks():
    s = Settings(environment="production", jwt_secret="change-me-please", storage_backend="local", cors_origins="")
    probs = " ".join(s.production_problems())
    assert "JWT_SECRET" in probs and "STORAGE_BACKEND" in probs and "CORS_ORIGINS" in probs
    ok = Settings(environment="production", jwt_secret="x" * 48, storage_backend="supabase", cors_origins="https://order.example.com")
    assert ok.production_problems() == []


def test_timezone_day_bounds_are_cafe_local():
    from datetime import date
    lo, hi = local_day_bounds(date(2026, 11, 13), "Asia/Kolkata")
    assert lo.isoformat() == "2026-11-12T18:30:00+00:00" and hi.isoformat() == "2026-11-13T18:30:00+00:00"
    lo2, hi2 = range_bounds(date(2026, 11, 1), date(2026, 11, 30), "Asia/Kolkata")
    assert (hi2 - lo2).days == 30


def test_image_sniffing_rejects_non_images():
    from app.api.v1.admin_catalog import sniff_image
    assert sniff_image(b"\xff\xd8\xff\xe0" + b"0" * 10)[1] == "jpg"
    assert sniff_image(b"\x89PNG\r\n\x1a\n" + b"0")[1] == "png"
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ")[1] == "webp"
    for bad in (b"GIF89a", b"<svg onload=alert(1)>", b"%PDF-1.4", b""):
        with pytest.raises(Invalid):
            sniff_image(bad)


def test_razorpay_checkout_signature_primitive():
    from app.integrations.razorpay.client import RazorpayGateway
    gw = RazorpayGateway()
    sig = hmac.new(b"rzp_secret", b"o1|p1", hashlib.sha256).hexdigest()
    assert gw.verify_checkout_signature(razorpay_order_id="o1", razorpay_payment_id="p1", razorpay_signature=sig)
    assert not gw.verify_checkout_signature(razorpay_order_id="o1", razorpay_payment_id="p1")
    assert gw.verify_webhook_signature(b"{}", {"x-razorpay-signature": hmac.new(b"rzp_whsec", b"{}", hashlib.sha256).hexdigest()})
    assert not gw.verify_webhook_signature(b"{}", {})
    refunded = gw.parse_webhook(b'{"event":"refund.processed","payload":{"refund":{"entity":{"id":"rfnd_1","payment_id":"p1","amount":500,"status":"processed","notes":{"refund_ref":"abc"}}}}}', {"x-razorpay-event-id": "e1"})
    assert refunded.refund.status == "succeeded" and refunded.refund_ref == "abc" and refunded.event_id == "e1"


def test_cashfree_amount_conversion_and_status_mapping():
    from app.integrations.base import paise_to_rupees, rupees_to_paise
    from app.integrations.cashfree.client import CashfreeGateway
    assert rupees_to_paise(105.5) == 10550 and rupees_to_paise("0.07") == 7 and str(paise_to_rupees(10550)) == "105.50"
    gw = CashfreeGateway()
    ev = gw.parse_webhook(b'{"type":"PAYMENT_FAILED_WEBHOOK","data":{"order":{"order_id":"o1"},"payment":{"cf_payment_id":9,"payment_status":"FAILED","payment_amount":10.5,"payment_currency":"INR"}}}', {})
    assert ev.payment.status == "failed" and ev.payment.amount_paise == 1050 and ev.gateway_order_id == "o1"
    assert gw.verify_checkout_signature() is False  # Cashfree has no client signature: server-side status only
