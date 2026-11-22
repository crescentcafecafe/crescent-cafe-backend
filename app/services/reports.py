"""Reporting definitions (documented in docs/REPORTING.md):
 orders placed  = orders created in range (any status)
 gross sales    = total of orders COMPLETED/DELIVERED in range (by completion time)
 payments rcvd  = paid payments by paid_at (money in, includes orders not yet completed)
 refunds        = succeeded refunds by processed_at
 net sales      = gross sales - refunds in range"""
from __future__ import annotations

import csv
import io
import uuid
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.offers import CouponRedemption, Offer
from app.models.orders import Order, OrderItem
from app.models.payments import Payment, Refund
from app.services.order_state import FINAL_OK
from app.utils.timeutil import range_bounds, to_local


async def build(db: AsyncSession, branch_id: uuid.UUID, start: date, end: date, tzname: str, granularity: str = "day") -> dict:
    lo, hi = range_bounds(start, end, tzname)

    def bucket(dt):
        d = to_local(dt, tzname).date()
        return d.strftime("%Y-%m") if granularity == "month" else d.isoformat()

    placed = (await db.execute(select(Order).where(Order.branch_id == branch_id, Order.created_at >= lo, Order.created_at < hi))).scalars().all()
    done = (await db.execute(select(Order).where(Order.branch_id == branch_id, Order.status.in_(FINAL_OK), Order.completed_at >= lo, Order.completed_at < hi))).scalars().all()
    pays = (await db.execute(select(Payment).where(Payment.branch_id == branch_id, Payment.paid_at >= lo, Payment.paid_at < hi,
                                                   Payment.status.in_(("paid", "partially_refunded", "refunded"))))).scalars().all()
    refunds = (await db.execute(select(Refund, Payment).join(Payment, Payment.id == Refund.payment_id).where(
        Payment.branch_id == branch_id, Refund.status == "succeeded", Refund.processed_at >= lo, Refund.processed_at < hi))).all()

    series: dict[str, dict] = defaultdict(lambda: {"orders_placed": 0, "orders_completed": 0, "gross_sales_paise": 0, "discount_paise": 0, "tax_paise": 0,
                                                    "delivery_charge_paise": 0, "refunds_paise": 0, "net_sales_paise": 0})
    for o in placed:
        series[bucket(o.created_at)]["orders_placed"] += 1
    for o in done:
        s = series[bucket(o.completed_at)]
        s["orders_completed"] += 1; s["gross_sales_paise"] += o.total_paise; s["discount_paise"] += o.discount_paise
        s["tax_paise"] += o.tax_paise; s["delivery_charge_paise"] += o.delivery_charge_paise
    refund_total = 0
    for r, _p in refunds:
        series[bucket(r.processed_at)]["refunds_paise"] += r.amount_paise
        refund_total += r.amount_paise
    for s in series.values():
        s["net_sales_paise"] = s["gross_sales_paise"] - s["refunds_paise"]

    by_type: dict[str, dict] = defaultdict(lambda: {"orders": 0, "sales_paise": 0})
    by_method: dict[str, dict] = defaultdict(lambda: {"orders": 0, "sales_paise": 0})
    for o in done:
        by_type[o.order_type]["orders"] += 1; by_type[o.order_type]["sales_paise"] += o.total_paise
        by_method[o.payment_method]["orders"] += 1; by_method[o.payment_method]["sales_paise"] += o.total_paise

    pay_summary: dict[str, dict] = defaultdict(lambda: {"payments": 0, "received_paise": 0, "refunded_paise": 0})
    for p in pays:
        k = p.gateway or "cash"
        pay_summary[k]["payments"] += 1; pay_summary[k]["received_paise"] += p.amount_paise
    for r, p in refunds:
        pay_summary[p.gateway or "cash"]["refunded_paise"] += r.amount_paise
    cash = [p for p in pays if p.method == "cash"]

    ids = [o.id for o in done]
    items_rows = (await db.execute(select(OrderItem).where(OrderItem.order_id.in_(ids)))).scalars().all() if ids else []
    best: dict[str, dict] = defaultdict(lambda: {"quantity": 0, "revenue_paise": 0})
    for i in items_rows:
        key = i.name + (f" ({i.variant_name})" if i.variant_name else "")
        best[key]["quantity"] += i.quantity; best[key]["revenue_paise"] += i.line_total_paise
    best_list = sorted(({"item": k, **v} for k, v in best.items()), key=lambda x: (-x["quantity"], x["item"]))

    reds = (await db.execute(select(CouponRedemption, Offer).join(Offer, Offer.id == CouponRedemption.offer_id).where(
        CouponRedemption.status == "consumed", CouponRedemption.consumed_at >= lo, CouponRedemption.consumed_at < hi))).all()
    coupons: dict[str, dict] = defaultdict(lambda: {"redemptions": 0, "discount_paise": 0})
    for r, o in reds:
        coupons[o.name]["redemptions"] += 1; coupons[o.name]["discount_paise"] += r.discount_paise

    gross = sum(o.total_paise for o in done)
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "timezone": tzname, "granularity": granularity},
        "totals": {
            "orders_placed": len(placed), "orders_completed": len(done),
            "orders_cancelled": sum(1 for o in placed if o.status == "cancelled" or o.status == "refunded" and o.cancellation_reason),
            "orders_rejected": sum(1 for o in placed if o.rejection_reason is not None),
            "gross_sales_paise": gross, "discount_paise": sum(o.discount_paise for o in done), "tax_paise": sum(o.tax_paise for o in done),
            "delivery_charge_paise": sum(o.delivery_charge_paise for o in done), "payments_received_paise": sum(p.amount_paise for p in pays),
            "cash_collected_paise": sum(p.amount_paise for p in cash), "cash_collections": len(cash),
            "refunds_paise": refund_total, "net_sales_paise": gross - refund_total,
        },
        "series": [{"period": k, **v} for k, v in sorted(series.items())],
        "by_order_type": dict(by_type), "by_payment_method": dict(by_method), "payments_by_gateway": dict(pay_summary),
        "coupon_usage": dict(coupons), "best_selling_items": best_list[:20], "item_quantities": best_list,
    }


def to_csv(kind: str, data: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "sales":
        cols = ["period", "orders_placed", "orders_completed", "gross_sales_paise", "discount_paise", "tax_paise", "delivery_charge_paise", "refunds_paise", "net_sales_paise"]
        w.writerow(cols)
        for r in data["series"]:
            w.writerow([r[c] for c in cols])
    elif kind == "items":
        w.writerow(["item", "quantity", "revenue_paise"])
        for r in data["item_quantities"]:
            w.writerow([r["item"], r["quantity"], r["revenue_paise"]])
    elif kind == "payments":
        w.writerow(["gateway", "payments", "received_paise", "refunded_paise"])
        for k, v in data["payments_by_gateway"].items():
            w.writerow([k, v["payments"], v["received_paise"], v["refunded_paise"]])
    elif kind == "coupons":
        w.writerow(["offer", "redemptions", "discount_paise"])
        for k, v in data["coupon_usage"].items():
            w.writerow([k, v["redemptions"], v["discount_paise"]])
    return buf.getvalue()
