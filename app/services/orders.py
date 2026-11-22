from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Forbidden, Invalid, NotFound
from app.core.security import hash_token, new_token, now_utc
from app.models.core import Customer, Rider
from app.models.delivery import RiderAssignment
from app.models.orders import Order, OrderItem, OrderItemCustomization, OrderStatusHistory
from app.models.payments import Payment
from app.models.tables import DiningTable
from app.services import emailing
from app.services import events as ev
from app.services import offers as offer_svc
from app.services import order_state as st
from app.services import pricing, settings as cfg, tables as table_svc
from app.utils.phone import normalize_phone

ORDER_TYPES = ("dine_in", "pickup", "delivery", "walk_in")
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_order_number(prefix: str = "CC") -> str:
    d = now_utc().strftime("%y%m%d")
    return f"{prefix}{d}-{''.join(secrets.choice(_ALPHABET) for _ in range(5))}"


async def create_order(db: AsyncSession, *, branch_id: uuid.UUID, data, customer: Customer | None, admin_id: uuid.UUID | None,
                       idempotency_key: str | None) -> tuple[Order, str | None, bool]:
    """Returns (order, guest_token_plaintext_or_None, duplicate)."""
    is_admin = admin_id is not None
    otype = data.order_type
    if otype not in ORDER_TYPES:
        raise Invalid("Unknown order type", code="invalid_order_type")
    if otype == "walk_in" and not is_admin:
        raise Forbidden("Walk-in orders can only be created by the Admin", code="admin_only")
    if is_admin and otype != "walk_in":
        raise Invalid("Admin creates walk-in orders", code="invalid_order_type")
    cs = await cfg.get_cafe_settings(db, branch_id)

    # --- identity / phone (mobile number is mandatory for every order except admin walk-ins without customer details)
    name, phone, email = data.customer_name, data.customer_phone, data.customer_email
    if customer is not None:
        name = name or customer.name
        email = email or customer.email
        if not customer.phone:
            if not phone:
                raise Invalid("Please add your mobile number before placing an order", code="phone_required")
            customer.phone = normalize_phone(phone)
        phone = customer.phone
    else:
        if not is_admin and not cs.guest_checkout_enabled:
            raise Forbidden("Please sign in to place an order", code="guest_disabled")
        if not is_admin and not (name and phone):
            raise Invalid("Name and mobile number are required", code="phone_required")
        phone = normalize_phone(phone) if phone else None
    if otype in ("delivery", "pickup") and not phone:
        raise Invalid("Mobile number is required", code="phone_required")

    # --- idempotency
    scope = f"a:{admin_id}" if is_admin else (f"c:{customer.id}" if customer else f"g:{phone}")
    if idempotency_key:
        existing = (await db.execute(select(Order).where(Order.idem_scope == scope, Order.idempotency_key == idempotency_key))).scalar_one_or_none()
        if existing is not None:
            return existing, None, True

    # --- cafe availability
    if not is_admin:
        ok, why = cfg.is_open_now(cs, now_utc())
        if not ok:
            raise Conflict(why or "Closed", code="cafe_closed")
        if otype == "dine_in" and not cs.dine_in_enabled:
            raise Conflict("Dine-in is currently unavailable", code="dine_in_disabled")
        if otype == "pickup" and not cs.pickup_enabled:
            raise Conflict("Pickup is currently unavailable", code="pickup_disabled")

    # --- table context
    sess = table = None
    if otype == "dine_in":
        if not data.table_session_token:
            raise Invalid("Scan the table QR code to order dine-in", code="table_session_required")
        sess, table = await table_svc.verify_table_token(db, data.table_session_token)
    elif otype == "walk_in" and data.table_id:
        table = await db.get(DiningTable, data.table_id)
        if table is None or table.branch_id != branch_id or table.is_archived:
            raise NotFound("Table not found")
        sess = await table_svc.open_session_for_admin(db, table)
    if otype == "delivery":
        addr = data.delivery_address
        if addr is None:
            raise Invalid("Delivery address is required", code="address_required")

    # --- payment method
    method = data.payment_method
    allowed = cfg.available_payment_methods(cs)
    if method not in allowed:
        raise Invalid(f"Payment method '{method}' is not available", code="payment_method_unavailable", details={"available": allowed})
    if method == "cash" and otype == "delivery" and not cs.cash_on_delivery_enabled:
        raise Invalid("Cash on delivery is not available", code="cod_disabled")

    # --- price on the server
    items = [pricing.ItemInput(menu_item_id=i.menu_item_id, quantity=i.quantity, variant_id=i.variant_id,
                               addons=[(a.addon_id, a.quantity) for a in i.addons], options=[(o.name, o.value) for o in i.options], notes=i.notes)
             for i in data.items]
    cart = await pricing.price_cart(db, branch_id=branch_id, items=items, order_type=otype, coupon_code=data.coupon_code,
                                    customer_id=customer.id if customer else None, phone=phone,
                                    delivery_postal_code=data.delivery_address.postal_code if data.delivery_address else None)
    if cart.total <= 0:
        raise Invalid("Order total must be greater than zero", code="invalid_total")

    online = method in ("razorpay", "cashfree")
    if online:
        status, pay_status = "payment_pending", "pending"
    elif is_admin:
        status, pay_status = "accepted", "unpaid"  # created by the Admin: no approval step
    else:
        status, pay_status = "placed", "unpaid"
    guest_token = None if (customer or is_admin) else new_token(24)
    order = Order(
        branch_id=branch_id, order_number=new_order_number(cs.invoice_prefix or "CC"), order_type=otype,
        source="admin" if is_admin else data.source, status=status, payment_method=method, payment_status=pay_status,
        customer_id=customer.id if customer else None, guest_token_hash=hash_token(guest_token) if guest_token else None,
        customer_name=name, customer_phone=phone, customer_email=email,
        table_id=table.id if table else None, table_session_id=sess.id if sess else None,
        table_label=(f"Table {table.number}" + (f" ({table.label})" if table.label else "")) if table else None,
        delivery_address=data.delivery_address.model_dump() if data.delivery_address else None,
        delivery_postal_code=data.delivery_address.postal_code if data.delivery_address else None,
        special_instructions=data.special_instructions, currency=cart.currency, prices_include_tax=cart.prices_include_tax,
        subtotal_paise=cart.subtotal, discount_paise=cart.discount, tax_paise=cart.tax, delivery_charge_paise=cart.delivery_charge,
        total_paise=cart.total, tax_breakdown=cart.tax_breakdown, discount_breakdown=cart.discount_breakdown, coupon_code=cart.coupon_code,
        created_by_admin_id=admin_id, idem_scope=scope if idempotency_key else None, idempotency_key=idempotency_key)
    try:
        async with db.begin_nested():
            db.add(order)
            await db.flush()
    except IntegrityError:  # concurrent duplicate submit with the same idempotency key
        existing = (await db.execute(select(Order).where(Order.idem_scope == scope, Order.idempotency_key == idempotency_key))).scalar_one_or_none()
        if existing is not None:
            return existing, None, True
        raise

    for l in cart.lines:
        oi = OrderItem(order_id=order.id, menu_item_id=l.item.id, category_id=l.item.category_id, name=l.item.name,
                       variant_id=l.variant.id if l.variant else None, variant_name=l.variant.name if l.variant else None,
                       unit_price_paise=l.unit_price, quantity=l.quantity, line_subtotal_paise=l.subtotal, discount_paise=l.discount,
                       tax_name=l.tax_name, tax_rate_bps=l.tax_rate_bps, tax_paise=l.tax, line_total_paise=l.total, notes=l.notes)
        db.add(oi)
        await db.flush()
        for a, q in l.addons:
            db.add(OrderItemCustomization(order_item_id=oi.id, kind="addon", ref_id=a.id, name=a.name, price_paise=a.price_paise, quantity=q))
        for n, v in l.options:
            db.add(OrderItemCustomization(order_item_id=oi.id, kind="option", name=n, value=v, price_paise=0, quantity=1))

    await st.add_history(db, order, None, status, "admin" if is_admin else ("customer" if customer else "guest"),
                         admin_id or (customer.id if customer else None), meta={"source": order.source})
    if cart.applied:
        await offer_svc.reserve(db, order, cart.applied, key=offer_svc.redeemer_key(order.customer_id, phone),
                                hold_minutes=cs.coupon_reservation_minutes if online else None)
    if method == "cash":
        db.add(Payment(order_id=order.id, branch_id=branch_id, method="cash", amount_paise=order.total_paise, currency=order.currency, status="pending"))

    topics = st.order_topics(order)
    await ev.emit(db, type="order.created", topics=topics, dedupe_key=f"order.created:{order.id}", recipients=st.customer_recipients(order),
                  title="Order placed", body=f"We received order {order.order_number}.", branch_id=branch_id,
                  data={"order_id": order.id, "order_number": order.order_number, "status": status, "total_paise": order.total_paise})
    if status == "placed":  # cash customer order awaits Admin decision -> alarm. Online orders alarm after payment verification.
        await st.emit_new_order_alarm(db, order, notify=cs.notify_admin_new_orders)
        await emailing.queue_email(db, "order_confirmation", order_id=order.id)  # online orders get theirs once payment is verified
    return order, guest_token, False


# ---------------------------------------------------------------- views / access
async def authorize_order(db: AsyncSession, order_id: uuid.UUID, principal, guest_token: str | None) -> tuple[Order, str]:
    order = await db.get(Order, order_id)
    if order is None:
        raise NotFound("Order not found", code="order_not_found")
    if principal is not None:
        if principal.role == "admin":
            return order, "admin"
        if principal.role == "customer" and order.customer_id == principal.id:
            return order, "customer"
        if principal.role == "rider":
            n = (await db.execute(select(func.count()).select_from(RiderAssignment).where(
                RiderAssignment.order_id == order.id, RiderAssignment.rider_id == principal.id,
                RiderAssignment.status.in_(("offered", "accepted", "picked_up", "completed"))))).scalar_one()
            if n:
                return order, "rider"
    if guest_token and order.guest_token_hash and hash_token(guest_token) == order.guest_token_hash:
        return order, "guest"
    raise NotFound("Order not found", code="order_not_found")  # 404 (not 403) so order ids cannot be probed


async def order_view(db: AsyncSession, order: Order, role: str, *, with_history: bool = False) -> dict:
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id))).scalars().all()
    cust = {}
    if items:
        rows = (await db.execute(select(OrderItemCustomization).where(OrderItemCustomization.order_item_id.in_([i.id for i in items])))).scalars().all()
        for c in rows:
            cust.setdefault(c.order_item_id, []).append(c)
    d = {
        "id": order.id, "order_number": order.order_number, "order_type": order.order_type, "source": order.source, "status": order.status,
        "payment_method": order.payment_method, "payment_status": order.payment_status, "currency": order.currency,
        "customer_name": order.customer_name, "customer_phone": order.customer_phone, "table_label": order.table_label,
        "table_id": order.table_id, "table_session_id": order.table_session_id, "delivery_address": order.delivery_address,
        "special_instructions": order.special_instructions, "created_at": order.created_at, "updated_at": order.updated_at,
        "items": [{"id": i.id, "menu_item_id": i.menu_item_id, "name": i.name, "variant_name": i.variant_name, "quantity": i.quantity,
                   "unit_price_paise": i.unit_price_paise, "line_subtotal_paise": i.line_subtotal_paise, "discount_paise": i.discount_paise,
                   "tax_paise": i.tax_paise, "tax_rate_bps": i.tax_rate_bps, "line_total_paise": i.line_total_paise, "notes": i.notes,
                   "customizations": [{"kind": c.kind, "name": c.name, "value": c.value, "price_paise": c.price_paise, "quantity": c.quantity}
                                      for c in cust.get(i.id, [])]} for i in items],
        "rejection_reason": order.rejection_reason, "cancellation_reason": order.cancellation_reason,
    }
    if role != "rider":
        d.update(subtotal_paise=order.subtotal_paise, discount_paise=order.discount_paise, tax_paise=order.tax_paise,
                 delivery_charge_paise=order.delivery_charge_paise, total_paise=order.total_paise, tax_breakdown=order.tax_breakdown,
                 discount_breakdown=order.discount_breakdown, coupon_code=order.coupon_code, customer_email=order.customer_email)
        pays = (await db.execute(select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at))).scalars().all()
        d["payments"] = [{"id": p.id, "method": p.method, "gateway": p.gateway, "status": p.status, "amount_paise": p.amount_paise,
                          "refunded_paise": p.refunded_paise, "paid_at": p.paid_at} for p in pays]
    else:
        d["collect_cash_paise"] = order.total_paise if (order.payment_method == "cash" and order.payment_status != "paid") else 0
    if role == "admin":
        d.update(created_by_admin_id=order.created_by_admin_id, customer_id=order.customer_id, decided_at=order.decided_at,
                 admin_alarm_acked_at=order.admin_alarm_acked_at)
    if order.order_type == "delivery" and role in ("admin", "customer", "guest"):
        a = (await db.execute(select(RiderAssignment).where(RiderAssignment.order_id == order.id,
                                                            RiderAssignment.status.in_(("offered", "accepted", "picked_up", "completed")))
                              .order_by(RiderAssignment.offered_at.desc()).limit(1))).scalar_one_or_none()
        if a and (role == "admin" or a.status != "offered"):
            r = await db.get(Rider, a.rider_id)
            d["delivery"] = {"assignment_id": a.id, "status": a.status, "rider": {"id": r.id, "name": r.name, "phone": r.phone} if r else None}
    if with_history:
        hist = (await db.execute(select(OrderStatusHistory).where(OrderStatusHistory.order_id == order.id).order_by(OrderStatusHistory.id))).scalars().all()
        d["history"] = [{"from": h.from_status, "to": h.to_status, "actor_role": h.actor_role if role == "admin" else None, "reason": h.reason,
                         "at": h.created_at} for h in hist]
    return d


def order_summary(o: Order) -> dict:
    return {"id": o.id, "order_number": o.order_number, "order_type": o.order_type, "status": o.status, "payment_method": o.payment_method,
            "payment_status": o.payment_status, "total_paise": o.total_paise, "customer_name": o.customer_name, "customer_phone": o.customer_phone,
            "table_label": o.table_label, "table_id": o.table_id, "table_session_id": o.table_session_id, "source": o.source, "created_at": o.created_at}
