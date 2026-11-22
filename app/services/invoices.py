from __future__ import annotations

import io
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict
from app.models.invoices import Invoice, InvoiceItem
from app.models.orders import Order, OrderItem, OrderItemCustomization
from app.models.settings import CafeSettings
from app.services.settings import get_cafe_settings
from app.utils.timeutil import to_local

NOT_INVOICEABLE = ("placed", "payment_pending", "payment_verified", "rejected", "cancelled", "refund_pending", "refunded")


async def ensure_invoice(db: AsyncSession, order: Order) -> Invoice:
    inv = (await db.execute(select(Invoice).where(Invoice.order_id == order.id))).scalar_one_or_none()
    if inv:
        return inv
    if order.status in NOT_INVOICEABLE:
        raise Conflict("An invoice is generated once the order has been accepted", code="not_invoiceable")
    await get_cafe_settings(db, order.branch_id)
    cs = (await db.execute(select(CafeSettings).where(CafeSettings.branch_id == order.branch_id).with_for_update())).scalar_one()
    inv = (await db.execute(select(Invoice).where(Invoice.order_id == order.id))).scalar_one_or_none()  # re-check under lock
    if inv:
        return inv
    n = cs.invoice_next_number
    cs.invoice_next_number = n + 1
    year = to_local(order.created_at, cs.timezone).year
    number = f"{cs.invoice_prefix}-{year}-{n:06d}"
    inv = Invoice(branch_id=order.branch_id, order_id=order.id, invoice_number=number,
                  seller={"name": cs.name, "address": cs.address, "gstin": cs.gstin, "phone": cs.phone, "email": cs.email},
                  buyer={"name": order.customer_name, "phone": order.customer_phone, "email": order.customer_email, "address": order.delivery_address},
                  currency=order.currency, subtotal_paise=order.subtotal_paise, discount_paise=order.discount_paise, tax_paise=order.tax_paise,
                  delivery_charge_paise=order.delivery_charge_paise, total_paise=order.total_paise, tax_breakdown=order.tax_breakdown,
                  payment_method=order.payment_method, payment_status=order.payment_status, footer=cs.invoice_footer)
    db.add(inv)
    await db.flush()
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
    custs = (await db.execute(select(OrderItemCustomization).where(OrderItemCustomization.order_item_id.in_([i.id for i in items])))).scalars().all() if items else []
    for i in items:
        extras = [f"{c.name} x{c.quantity}" if c.kind == "addon" else f"{c.name}: {c.value}" for c in custs if c.order_item_id == i.id]
        desc = i.name + (f" ({i.variant_name})" if i.variant_name else "") + (f" + {', '.join(extras)}" if extras else "")
        db.add(InvoiceItem(invoice_id=inv.id, description=desc[:300], quantity=i.quantity, unit_price_paise=i.unit_price_paise, discount_paise=i.discount_paise,
                           tax_rate_bps=i.tax_rate_bps, tax_paise=i.tax_paise, total_paise=i.line_total_paise))
    await db.flush()
    return inv


async def invoice_data(db: AsyncSession, inv: Invoice) -> dict:
    items = (await db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id))).scalars().all()
    order = await db.get(Order, inv.order_id)
    return {
        "id": inv.id, "invoice_number": inv.invoice_number, "order_id": inv.order_id, "order_number": order.order_number, "order_type": order.order_type,
        "table_label": order.table_label, "issued_at": inv.issued_at, "order_created_at": order.created_at, "currency": inv.currency,
        "seller": inv.seller, "buyer": inv.buyer, "payment_method": inv.payment_method, "payment_status": order.payment_status,
        "items": [{"description": i.description, "quantity": i.quantity, "unit_price_paise": i.unit_price_paise, "discount_paise": i.discount_paise,
                   "tax_rate_bps": i.tax_rate_bps, "tax_paise": i.tax_paise, "total_paise": i.total_paise} for i in items],
        "subtotal_paise": inv.subtotal_paise, "discount_paise": inv.discount_paise, "tax_paise": inv.tax_paise,
        "delivery_charge_paise": inv.delivery_charge_paise, "total_paise": inv.total_paise, "tax_breakdown": inv.tax_breakdown or [],
        "prices_include_tax": order.prices_include_tax, "footer": inv.footer,
    }


def _rs(p: int) -> str:
    return f"Rs. {p / 100:,.2f}"


def render_pdf(d: dict) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    y = h - 50
    s = d["seller"]
    c.setFont("Helvetica-Bold", 16); c.drawString(40, y, s.get("name") or "Crescent Cafe"); y -= 16
    c.setFont("Helvetica", 9)
    for line in filter(None, [s.get("address"), f"Phone: {s['phone']}" if s.get("phone") else None, f"GSTIN: {s['gstin']}" if s.get("gstin") else None]):
        c.drawString(40, y, str(line)[:100]); y -= 12
    c.setFont("Helvetica-Bold", 12); c.drawRightString(w - 40, h - 50, "TAX INVOICE"); c.setFont("Helvetica", 9)
    c.drawRightString(w - 40, h - 66, f"Invoice: {d['invoice_number']}")
    c.drawRightString(w - 40, h - 78, f"Order: {d['order_number']}")
    c.drawRightString(w - 40, h - 90, f"Date: {d['issued_at'].strftime('%d %b %Y %H:%M')} UTC")
    y -= 10
    b = d["buyer"]
    c.setFont("Helvetica-Bold", 10); c.drawString(40, y, "Billed to"); y -= 13; c.setFont("Helvetica", 9)
    for line in filter(None, [b.get("name"), b.get("phone"), d.get("table_label")]):
        c.drawString(40, y, str(line)); y -= 12
    y -= 8
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Item"); c.drawRightString(330, y, "Qty"); c.drawRightString(400, y, "Price"); c.drawRightString(470, y, "Tax"); c.drawRightString(w - 40, y, "Total")
    y -= 4; c.line(40, y, w - 40, y); y -= 12; c.setFont("Helvetica", 9)
    for i in d["items"]:
        if y < 120:
            c.showPage(); y = h - 50; c.setFont("Helvetica", 9)
        c.drawString(40, y, i["description"][:60]); c.drawRightString(330, y, str(i["quantity"])); c.drawRightString(400, y, _rs(i["unit_price_paise"]))
        c.drawRightString(470, y, _rs(i["tax_paise"])); c.drawRightString(w - 40, y, _rs(i["total_paise"])); y -= 13
    y -= 4; c.line(40, y, w - 40, y); y -= 14
    def row(label, val, bold=False):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9); c.drawString(340, y, label); c.drawRightString(w - 40, y, val); y -= 13
    row("Subtotal", _rs(d["subtotal_paise"]))
    if d["discount_paise"]: row("Discount", "-" + _rs(d["discount_paise"]))
    for t in d["tax_breakdown"]: row(f"{t['name']} ({t['rate_bps'] / 100:g}%)", _rs(t["tax_paise"]))
    if d["delivery_charge_paise"]: row("Delivery charge", _rs(d["delivery_charge_paise"]))
    row("Total" + (" (incl. tax)" if d["prices_include_tax"] else ""), _rs(d["total_paise"]), bold=True)
    y -= 6
    c.setFont("Helvetica", 9); c.drawString(40, y, f"Payment: {d['payment_method']} ({d['payment_status']})"); y -= 20
    if d.get("footer"): c.drawString(40, y, d["footer"][:110])
    c.showPage(); c.save()
    return buf.getvalue()
