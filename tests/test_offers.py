import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import IS_PG


def offer(cafe, **kw):
    body = {"name": "Offer", "discount_type": "percent", "value": 1000, **kw}
    r = cafe.post("/admin/offers", body, h=cafe.admin)
    assert r.status_code == 201, r.text
    return r.json()


def validate(cafe, code, h=None, kind="cake", qty=1, **kw):
    r = cafe.post("/coupons/validate", {"order_type": "pickup", "items": cafe.cart(kind, qty), "coupon_code": code, **kw}, h=h)
    assert r.status_code == 200, r.text
    return r.json()


def iso(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def test_percent_with_cap_and_min_order_and_explanations(cafe):
    offer(cafe, coupon_code="BIG", value=2000, max_discount_paise=1500, min_order_paise=20000)
    v = validate(cafe, "BIG", qty=1)
    assert v["valid"] is False and "more to use this offer" in v["reason"]  # explains the shortfall
    v = validate(cafe, "big", qty=3)  # case-insensitive; 300.00 * 20% = 60.00 capped to 15.00
    assert v["valid"] and v["discount_paise"] == 1500 and v["quote"]["total_paise"] == (30000 - 1500) + round((30000 - 1500) * 0.05)
    assert validate(cafe, "NOPE")["reason"] == "This coupon code does not exist"
    assert cafe.post("/coupons/validate", {"order_type": "pickup", "items": cafe.cart(), "coupon_code": "x"}).status_code == 422


def test_fixed_discount_and_tax_computed_after_discount(cafe):
    offer(cafe, coupon_code="FLAT50", discount_type="fixed", value=5000)
    h = cafe.customer()
    o = cafe.place("pickup", h=h, kind="latte", qty=2, coupon_code="flat50").json()["order"]
    # 300.00 - 50.00 = 250.00 ; 5% tax = 12.50
    assert (o["subtotal_paise"], o["discount_paise"], o["tax_paise"], o["total_paise"]) == (30000, 5000, 1250, 26250)
    assert o["coupon_code"] == "FLAT50" and o["discount_breakdown"][0]["code"] == "FLAT50"


def test_discount_never_exceeds_items_value(cafe):
    offer(cafe, discount_type="fixed", value=999999, coupon_code="HUGE")
    v = validate(cafe, "HUGE")
    assert v["discount_paise"] == 10000 and v["quote"]["total_paise"] == 0
    r = cafe.place("pickup", h=cafe.customer(), coupon_code="HUGE")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_total"  # free orders are refused


def test_item_and_category_scoped_offers(cafe):
    offer(cafe, coupon_code="CAKE20", value=2000, item_ids=[cafe.ids["cake"]])
    two = [{"menu_item_id": cafe.ids["cake"], "quantity": 1}, {"menu_item_id": cafe.ids["latte"], "variant_id": cafe.ids["latte_small"], "quantity": 1}]
    r = cafe.post("/coupons/validate", {"order_type": "pickup", "items": two, "coupon_code": "CAKE20"}).json()
    assert r["discount_paise"] == 2000  # only the cake qualifies: 20% of 100.00
    r = cafe.post("/coupons/validate", {"order_type": "pickup", "items": [two[1]], "coupon_code": "CAKE20"}).json()
    assert r["valid"] is False and "qualify" in r["reason"]
    offer(cafe, coupon_code="COFFEE", value=1000, category_ids=[cafe.ids["cat"]])
    assert cafe.post("/coupons/validate", {"order_type": "pickup", "items": two, "coupon_code": "COFFEE"}).json()["discount_paise"] == 2500


def test_time_window_pause_and_expiry(cafe):
    offer(cafe, coupon_code="SOON", starts_at=iso(days=1))
    offer(cafe, coupon_code="GONE", starts_at=iso(days=-3), ends_at=iso(days=-1))
    live = offer(cafe, coupon_code="LIVE", starts_at=iso(days=-1), ends_at=iso(days=1))
    assert "not started" in validate(cafe, "SOON")["reason"]
    assert "expired" in validate(cafe, "GONE")["reason"]
    assert validate(cafe, "LIVE")["valid"]
    assert cafe.patch(f"/admin/offers/{live['id']}", {"status": "paused"}, h=cafe.admin).json()["status"] == "paused"
    assert "paused" in validate(cafe, "LIVE")["reason"]
    cafe.patch(f"/admin/offers/{live['id']}", {"status": "active"}, h=cafe.admin)
    assert validate(cafe, "LIVE")["valid"]
    cafe.patch(f"/admin/coupons/{live['coupons'][0]['id']}/active", {"is_available": False}, h=cafe.admin)
    assert "no longer active" in validate(cafe, "LIVE")["reason"]
    assert cafe.post("/admin/offers", {"name": "bad", "discount_type": "percent", "value": 20000}, h=cafe.admin).status_code == 422
    assert cafe.post("/admin/offers", {"name": "bad", "discount_type": "fixed", "value": 5, "starts_at": iso(days=2), "ends_at": iso(days=1)}, h=cafe.admin).status_code == 422
    assert cafe.post("/admin/offers", {"name": "dup", "discount_type": "fixed", "value": 5, "coupon_code": "SOON"}, h=cafe.admin).status_code == 409


def test_expire_job_marks_offers_expired_but_keeps_history(cafe):
    o = offer(cafe, coupon_code="OLD", ends_at=iso(seconds=1))
    from app.db.session import session_scope
    from app.workers.handlers import _expire_offers
    import time
    time.sleep(1.2)

    async def run():
        async with session_scope() as db:
            await _expire_offers(db, {})
    cafe.call(run)
    assert [x for x in cafe.get("/admin/offers?status=expired", h=cafe.admin).json() if x["id"] == o["id"]]


def test_first_order_only(cafe):
    offer(cafe, coupon_code="WELCOME", first_order_only=True)
    h = cafe.customer()
    assert validate(cafe, "WELCOME", h=h)["valid"]
    o = cafe.place("pickup", h=h, coupon_code="WELCOME").json()["order"]
    assert o["discount_paise"] == 1000
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    assert "first order" in validate(cafe, "WELCOME", h=h)["reason"]
    # same mobile number as a guest does not get it again either
    g = validate(cafe, "WELCOME", customer_phone="9876543210")
    assert g["valid"] is False
    assert validate(cafe, "WELCOME", customer_phone="9000011111")["valid"]


def test_per_customer_and_total_limits(cafe):
    offer(cafe, coupon_code="ONCE", per_customer_limit=1)
    offer(cafe, coupon_code="TWO", total_redemption_limit=2)
    a, b, c = cafe.customer("A"), cafe.customer("B", phone="9111111111"), cafe.customer("C", phone="9222222222")
    assert cafe.place("pickup", h=a, coupon_code="ONCE").status_code == 201
    r = cafe.place("pickup", h=a, coupon_code="ONCE")
    assert r.status_code == 422 and "already used" in r.json()["error"]["message"]
    assert cafe.place("pickup", h=b, coupon_code="ONCE").status_code == 201  # another customer is fine
    assert cafe.place("pickup", h=a, coupon_code="TWO").status_code == 201
    assert cafe.place("pickup", h=b, coupon_code="TWO").status_code == 201
    assert "redemption limit" in cafe.place("pickup", h=c, coupon_code="TWO").json()["error"]["message"]
    # guests are limited per mobile number
    g1 = cafe.place("pickup", coupon_code="ONCE", customer_name="G", customer_phone="9800000001")
    g2 = cafe.place("pickup", coupon_code="ONCE", customer_name="G", customer_phone="9800000001")
    assert g1.status_code == 201 and g2.status_code == 422


def test_rejected_or_cancelled_order_releases_redemption(cafe):
    offer(cafe, coupon_code="ONE", total_redemption_limit=1)
    a, b = cafe.customer("A"), cafe.customer("B", phone="9111111111")
    o = cafe.place("pickup", h=a, coupon_code="ONE").json()["order"]
    assert cafe.place("pickup", h=b, coupon_code="ONE").status_code == 422
    cafe.post(f"/admin/orders/{o['id']}/reject", {"reason": "sold out of cake"}, h=cafe.admin)
    assert cafe.place("pickup", h=b, coupon_code="ONE").status_code == 201
    row = cafe.get("/admin/offers", h=cafe.admin).json()[0]
    reds = cafe.get(f"/admin/offers/{row['id']}/redemptions", h=cafe.admin).json()
    assert sorted(r["status"] for r in reds) == ["consumed", "released"]


def test_stacking_rules(cafe):
    offer(cafe, name="auto A", value=1000)
    offer(cafe, name="auto B", value=500)
    q = cafe.post("/orders/quote", {"order_type": "pickup", "items": cafe.cart(qty=2)}).json()
    assert q["discount_paise"] == 2000 and len(q["discounts"]) == 1 and q["discounts"][0]["name"] == "auto A"  # best single offer, no stacking by default
    offer(cafe, name="auto C", value=500, stackable=True)
    offer(cafe, name="auto D", value=500, stackable=True)
    q = cafe.post("/orders/quote", {"order_type": "pickup", "items": cafe.cart(qty=2)}).json()
    assert len(q["discounts"]) == 1  # A (non-stackable) wins and blocks the rest
    for o in cafe.get("/admin/offers", h=cafe.admin).json():
        if o["name"] in ("auto A", "auto B"):
            cafe.patch(f"/admin/offers/{o['id']}", {"status": "paused"}, h=cafe.admin)
    q = cafe.post("/orders/quote", {"order_type": "pickup", "items": cafe.cart(qty=2)}).json()
    assert len(q["discounts"]) == 2 and q["discount_paise"] == 2000  # C + D stack because both allow it
    offer(cafe, coupon_code="CODE", value=2000, stackable=True)
    q = validate(cafe, "CODE", qty=2)["quote"]
    assert len(q["discounts"]) == 3 and q["discount_paise"] == 6000


def test_offer_listing_and_visibility(cafe):
    offer(cafe, name="Listed", coupon_code="SHOWME")
    offer(cafe, name="Secret", coupon_code="HIDDEN", is_listed=False)
    offer(cafe, name="Auto promo", value=500)
    names = {o["name"]: o for o in cafe.get("/offers").json()}
    assert set(names) == {"Listed", "Auto promo"} and names["Listed"]["coupon_code"] == "SHOWME" and names["Auto promo"]["applied_automatically"]
    offer(cafe, name="First", coupon_code="FIRST1", first_order_only=True)
    h = cafe.customer()
    cafe.post(f"/admin/orders/{cafe.place('pickup', h=h).json()['order']['id']}/accept", h=cafe.admin)
    first = {o["name"]: o for o in cafe.get("/offers", h=h).json()}["First"]
    assert first["eligible"] is False and "first order" in first["reason"].lower()


def test_branch_specific_offer_and_order_type_restriction(cafe):
    offer(cafe, coupon_code="DINE", order_types=["dine_in"], branch_specific=True)
    assert "order type" in validate(cafe, "DINE")["reason"]
    r = cafe.post("/coupons/validate", {"order_type": "dine_in", "items": cafe.cart(), "coupon_code": "DINE"}).json()
    assert r["valid"]


def test_offer_changes_and_redemptions_are_audited(cafe):
    o = offer(cafe, coupon_code="AUDIT")
    cafe.patch(f"/admin/offers/{o['id']}", {"value": 1500}, h=cafe.admin)
    cafe.place("pickup", h=cafe.customer(), coupon_code="AUDIT")
    logs = cafe.get("/admin/audit-logs?entity_type=offer", h=cafe.admin).json()
    assert {"offer.created", "offer.updated"} <= {l["action"] for l in logs}
    upd = [l for l in logs if l["action"] == "offer.updated"][0]
    assert upd["before"]["value"] == "1000" and upd["after"]["value"] == "1500"
    assert cafe.get(f"/admin/offers/{o['id']}/redemptions", h=cafe.admin).json()[0]["discount_paise"] == 1500


def test_offer_admin_only(cafe):
    ch = cafe.customer()
    assert cafe.post("/admin/offers", {"name": "x", "discount_type": "fixed", "value": 5}, h=ch).status_code == 403
    assert cafe.get("/admin/offers").status_code == 401


def make_order_data(cafe, coupon):
    from app.schemas.orders import OrderCreate
    return OrderCreate(order_type="pickup", payment_method="cash", items=[{"menu_item_id": cafe.ids["cake"], "quantity": 1}], coupon_code=coupon)


@pytest.mark.skipif(not IS_PG, reason="row-level locking needs PostgreSQL")
def test_concurrent_redemptions_respect_total_limit(cafe):
    offer(cafe, coupon_code="RACE", total_redemption_limit=1)
    ids = []
    for i in range(6):
        h = cafe.customer(f"R{i}", phone=f"90000000{i:02d}")
        ids.append(cafe.get("/customers/me", h=h).json()["id"])
    from app.db.session import session_scope
    from app.models.core import Branch, Customer
    from app.services import orders as svc
    from sqlalchemy import select

    async def attempt(cid):
        async with session_scope() as db:
            br = (await db.execute(select(Branch))).scalars().first()
            cust = await db.get(Customer, uuid.UUID(cid))
            o, _, _ = await svc.create_order(db, branch_id=br.id, data=make_order_data(cafe, "RACE"), customer=cust, admin_id=None, idempotency_key=None)
            return o.id

    async def race():
        return await asyncio.gather(*[attempt(c) for c in ids], return_exceptions=True)
    res = cafe.call(race)
    wins = [r for r in res if not isinstance(r, Exception)]
    assert len(wins) == 1, res
    assert cafe.get("/admin/offers", h=cafe.admin).json()[0]["redemptions_consumed"] == 1


@pytest.mark.skipif(not IS_PG, reason="row-level locking needs PostgreSQL")
def test_concurrent_per_customer_limit(cafe):
    offer(cafe, coupon_code="SAME", per_customer_limit=1)
    h = cafe.customer("Dup")
    cid = cafe.get("/customers/me", h=h).json()["id"]
    from app.db.session import session_scope
    from app.models.core import Branch, Customer
    from app.services import orders as svc
    from sqlalchemy import select

    async def attempt():
        async with session_scope() as db:
            br = (await db.execute(select(Branch))).scalars().first()
            cust = await db.get(Customer, uuid.UUID(cid))
            return (await svc.create_order(db, branch_id=br.id, data=make_order_data(cafe, "SAME"), customer=cust, admin_id=None, idempotency_key=None))[0].id

    async def race():
        return await asyncio.gather(attempt(), attempt(), attempt(), return_exceptions=True)
    res = cafe.call(race)
    assert sum(1 for r in res if not isinstance(r, Exception)) == 1
