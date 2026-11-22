import csv
import io
import re
import uuid

from app.integrations.base import GatewayPaymentInfo
from tests.test_payments import mark_paid, online_order


def finish(cafe, oid):
    cafe.post(f"/admin/orders/{oid}/accept", h=cafe.admin)
    cafe.advance(oid, "preparing", "ready", "completed")


def test_invoice_numbers_are_unique_sequential_and_stable(cafe):
    h = cafe.customer()
    o1 = cafe.place("pickup", h=h, kind="latte", qty=2).json()["order"]
    assert cafe.get(f"/invoices/order/{o1['id']}", h=h).json()["error"]["code"] == "not_invoiceable"  # nothing before acceptance
    finish(cafe, o1["id"])
    o2 = cafe.walk_in().json()["order"]
    i1 = cafe.get(f"/invoices/order/{o1['id']}", h=h).json()
    i2 = cafe.get(f"/invoices/order/{o2['id']}", h=cafe.admin).json()
    nums = [int(x["invoice_number"].rsplit("-", 1)[1]) for x in (i1, i2)]
    assert re.fullmatch(r"CC-\d{4}-\d{6}", i1["invoice_number"]) and sorted(nums) == [1, 2]
    assert cafe.get(f"/invoices/order/{o1['id']}", h=h).json()["invoice_number"] == i1["invoice_number"]  # stable on re-read
    assert i1["total_paise"] == o1["total_paise"] == 31500 and i1["tax_paise"] == 1500 and i1["subtotal_paise"] == 30000
    assert i1["items"][0]["description"] == "Latte (Small)" and i1["items"][0]["quantity"] == 2
    assert i1["tax_breakdown"] == [{"name": "GST 5%", "rate_bps": 500, "taxable_paise": 30000, "tax_paise": 1500}]
    assert i1["buyer"]["phone"] == "+919876543210" and i1["seller"]["name"] == "Crescent Cafe"
    assert i1["payment_method"] == "cash" and i1["payment_status"] == "unpaid"
    invs = cafe.get("/admin/invoices", h=cafe.admin).json()
    assert invs["total"] == 2


def test_invoice_snapshot_unaffected_by_later_price_changes_and_has_delivery_and_discount(cafe):
    cafe.post("/admin/offers", {"name": "T", "discount_type": "fixed", "value": 1000, "coupon_code": "OFF10"}, h=cafe.admin)
    h = cafe.customer()
    o = cafe.place("delivery", h=h, kind="cake", coupon_code="OFF10").json()["order"]
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    inv = cafe.get(f"/invoices/order/{o['id']}", h=h).json()
    cafe.patch(f"/admin/menu/items/{cafe.ids['cake']}", {"price_paise": 99999, "name": "Renamed"}, h=cafe.admin)
    again = cafe.get(f"/invoices/order/{o['id']}", h=h).json()
    assert again == inv and inv["discount_paise"] == 1000 and inv["delivery_charge_paise"] == 3000
    assert inv["total_paise"] == o["total_paise"] == 9000 + 450 + 3000
    assert inv["items"][0]["description"] == "Cake"


def test_invoice_access_control_and_pdf(cafe):
    h, other = cafe.customer(), cafe.customer("O", phone="9111111111")
    r = cafe.place("pickup", customer_name="G", customer_phone="9800011111").json()
    g, gt = r["order"], {"X-Guest-Token": r["guest_token"]}
    o = cafe.place("pickup", h=h).json()["order"]
    for oid in (g["id"], o["id"]):
        finish(cafe, oid)
    assert cafe.get(f"/invoices/order/{o['id']}", h=other).status_code == 404
    assert cafe.get(f"/invoices/order/{o['id']}").status_code == 404
    assert cafe.get(f"/invoices/order/{g['id']}", h=gt).status_code == 200
    assert cafe.get(f"/invoices/order/{g['id']}", h={"X-Guest-Token": "bad"}).status_code == 404
    pdf = cafe.get(f"/invoices/order/{o['id']}/pdf", h=h)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF") and len(pdf.content) > 1000
    assert cafe.get(f"/invoices/order/{o['id']}/pdf", h=other).status_code == 404


def test_invoice_generation_job_is_idempotent(cafe):
    h = cafe.customer()
    o = cafe.place("pickup", h=h).json()["order"]
    finish(cafe, o["id"])
    cafe.run_jobs()
    cafe.run_jobs()
    assert cafe.get("/admin/invoices", h=cafe.admin).json()["total"] == 1


def build_week(cafe):
    """5 orders: A cash walk-in completed, B online latte x2 completed (+refund 50.00), C rejected, D cancelled, E coupon order completed."""
    a = cafe.walk_in().json()["order"]
    finish_walk = cafe.advance(a["id"], "preparing", "ready", "completed")
    pa = cafe.get(f"/admin/orders/{a['id']}", h=cafe.admin).json()["payments"][0]["id"]
    cafe.post(f"/admin/payments/{pa}/confirm-cash", {"amount_received_paise": 10500}, h=cafe.admin)
    b, init, hb = online_order(cafe, "razorpay", kind="latte", qty=2)
    mark_paid(cafe, "razorpay", init, b)
    cafe.post(f"/payments/orders/{b['id']}/sync", h=hb)
    finish(cafe, b["id"])
    pb = cafe.get(f"/admin/orders/{b['id']}", h=cafe.admin).json()["payments"][0]["id"]
    rf = cafe.post(f"/admin/payments/{pb}/refunds", {"amount_paise": 5000, "reason": "Spilled"}, h=cafe.admin).json()
    cafe.run_jobs()
    from tests.test_payments import webhook
    webhook(cafe, "razorpay", {"event_id": "rep-1", "type": "refund.processed", "refund_status": "succeeded", "gateway_refund_id": "rf_1", "refund_ref": rf["id"]})
    c = cafe.place("pickup", h=cafe.customer("C", phone="9111111111")).json()["order"]
    cafe.post(f"/admin/orders/{c['id']}/reject", {"reason": "closing early"}, h=cafe.admin)
    hd = cafe.customer("D", phone="9222222222")
    d = cafe.place("pickup", h=hd).json()["order"]
    cafe.post(f"/orders/{d['id']}/cancel", {"reason": "changed my mind"}, h=hd)
    cafe.post("/admin/offers", {"name": "Ten off", "discount_type": "percent", "value": 1000, "coupon_code": "TEN"}, h=cafe.admin)
    e = cafe.place("pickup", h=cafe.customer("E", phone="9333333333"), coupon_code="TEN").json()["order"]
    finish(cafe, e["id"])
    pe = cafe.get(f"/admin/orders/{e['id']}", h=cafe.admin).json()["payments"][0]["id"]
    cafe.post(f"/admin/payments/{pe}/confirm-cash", {"amount_received_paise": 9450}, h=cafe.admin)
    return a, b, c, d, e


def test_report_figures_are_consistent(cafe):
    build_week(cafe)
    r = cafe.get("/admin/reports/summary", h=cafe.admin).json()
    t = r["totals"]
    assert t["orders_placed"] == 5 and t["orders_completed"] == 3 and t["orders_cancelled"] == 1 and t["orders_rejected"] == 1
    assert t["gross_sales_paise"] == 10500 + 31500 + 9450 == 51450
    assert t["discount_paise"] == 1000 and t["tax_paise"] == 500 + 1500 + 450 and t["delivery_charge_paise"] == 0
    assert t["payments_received_paise"] == 51450 and t["cash_collected_paise"] == 10500 + 9450 and t["cash_collections"] == 2
    assert t["refunds_paise"] == 5000 and t["net_sales_paise"] == 46450  # payments vs refunds vs net are distinct figures
    assert r["by_order_type"] == {"walk_in": {"orders": 1, "sales_paise": 10500}, "pickup": {"orders": 2, "sales_paise": 40950}}
    assert r["by_payment_method"] == {"cash": {"orders": 2, "sales_paise": 19950}, "razorpay": {"orders": 1, "sales_paise": 31500}}
    assert r["payments_by_gateway"]["razorpay"] == {"payments": 1, "received_paise": 31500, "refunded_paise": 5000}
    assert r["payments_by_gateway"]["cash"] == {"payments": 2, "received_paise": 19950, "refunded_paise": 0}
    assert r["coupon_usage"] == {"Ten off": {"redemptions": 1, "discount_paise": 1000}}
    assert [(i["item"], i["quantity"]) for i in r["best_selling_items"]] == [("Cake", 2), ("Latte (Small)", 2)]
    assert len(r["series"]) == 1 and r["series"][0]["net_sales_paise"] == 46450 and r["series"][0]["orders_placed"] == 5
    assert sum(s["gross_sales_paise"] for s in r["series"]) == t["gross_sales_paise"]


def test_monthly_granularity_date_filters_and_validation(cafe):
    build_week(cafe)
    m = cafe.get("/admin/reports/summary?granularity=month", h=cafe.admin).json()
    assert len(m["series"]) == 1 and re.fullmatch(r"\d{4}-\d{2}", m["series"][0]["period"])
    empty = cafe.get("/admin/reports/summary?start=2020-01-01&end=2020-01-31", h=cafe.admin).json()
    assert empty["totals"]["orders_placed"] == 0 and empty["totals"]["gross_sales_paise"] == 0 and empty["series"] == []
    assert cafe.get("/admin/reports/summary?start=2026-02-01&end=2026-01-01", h=cafe.admin).json()["error"]["code"] == "invalid_range"
    assert cafe.get("/admin/reports/summary?start=2020-01-01&end=2026-01-01", h=cafe.admin).json()["error"]["code"] == "range_too_large"
    assert cafe.get("/admin/reports/summary", h=cafe.customer()).status_code == 403


def test_csv_exports(cafe):
    build_week(cafe)
    for kind, header in (("sales", "period"), ("items", "item"), ("payments", "gateway"), ("coupons", "offer")):
        r = cafe.get(f"/admin/reports/export?kind={kind}", h=cafe.admin)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
        rows = list(csv.reader(io.StringIO(r.text)))
        assert rows[0][0] == header and len(rows) >= 2
    sales = list(csv.DictReader(io.StringIO(cafe.get("/admin/reports/export?kind=sales", h=cafe.admin).text)))[0]
    assert sales["gross_sales_paise"] == "51450" and sales["refunds_paise"] == "5000" and sales["net_sales_paise"] == "46450"
    assert cafe.get("/admin/reports/export?kind=bogus", h=cafe.admin).status_code == 422
    assert cafe.get("/admin/reports/export?kind=sales").status_code == 401


def test_dashboard_sales_today_matches_report(cafe):
    build_week(cafe)
    d = cafe.get("/admin/dashboard", h=cafe.admin).json()
    assert d["sales_today_paise"] == 51450 and d["orders_placed_today"] == 5
