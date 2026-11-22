"""Server-side pricing. Client totals are never trusted: everything is recomputed from DB prices (integer paise)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Invalid, NotFound
from app.models.menu import MenuAddon, MenuCategory, MenuItem, MenuItemVariant
from app.models.settings import TaxSetting
from app.services import offers as offer_svc
from app.services.settings import get_cafe_settings, get_delivery_settings

MAX_LINE_QTY = 50
MAX_LINES = 40


@dataclass
class ItemInput:
    menu_item_id: uuid.UUID
    quantity: int
    variant_id: uuid.UUID | None = None
    addons: list[tuple[uuid.UUID, int]] = field(default_factory=list)
    options: list[tuple[str, str]] = field(default_factory=list)
    notes: str | None = None


@dataclass
class PricedLine:
    key: int
    item: MenuItem
    variant: MenuItemVariant | None
    addons: list[tuple[MenuAddon, int]]
    options: list[tuple[str, str]]
    quantity: int
    unit_price: int
    subtotal: int
    notes: str | None
    tax_name: str | None = None
    tax_rate_bps: int = 0
    discount: int = 0
    tax: int = 0
    total: int = 0


@dataclass
class PricedCart:
    lines: list[PricedLine]
    subtotal: int
    discount: int
    tax: int
    delivery_charge: int
    delivery_tax: int
    total: int
    tax_breakdown: list[dict]
    discount_breakdown: list[dict]
    applied: list[offer_svc.AppliedOffer]
    prices_include_tax: bool
    currency: str
    coupon_code: str | None


def tax_part_inclusive(gross: int, bps: int) -> int:
    return gross - (gross * 10000 + (10000 + bps) // 2) // (10000 + bps) if bps else 0


def tax_exclusive(base: int, bps: int) -> int:
    return (base * bps + 5000) // 10000


async def price_cart(db: AsyncSession, *, branch_id: uuid.UUID, items: list[ItemInput], order_type: str, coupon_code: str | None,
                     customer_id: uuid.UUID | None, phone: str | None, delivery_postal_code: str | None = None,
                     now: datetime | None = None) -> PricedCart:
    if not items:
        raise Invalid("Your cart is empty", code="empty_cart")
    if len(items) > MAX_LINES:
        raise Invalid("Too many items in one order", code="too_many_lines")
    cs = await get_cafe_settings(db, branch_id)
    ids = {i.menu_item_id for i in items}
    found = {m.id: m for m in (await db.execute(select(MenuItem).where(MenuItem.id.in_(ids)))).scalars().all()}
    cats = {c.id: c for c in (await db.execute(select(MenuCategory).where(MenuCategory.id.in_({m.category_id for m in found.values()})))).scalars().all()}
    variants = (await db.execute(select(MenuItemVariant).where(MenuItemVariant.item_id.in_(ids)))).scalars().all()
    addons = (await db.execute(select(MenuAddon).where(MenuAddon.item_id.in_(ids)))).scalars().all()
    vmap, amap = {v.id: v for v in variants}, {a.id: a for a in addons}
    taxes = {t.id: t for t in (await db.execute(select(TaxSetting).where(TaxSetting.branch_id == branch_id, TaxSetting.is_active.is_(True)))).scalars().all()}
    default_tax = next((t for t in taxes.values() if t.is_default), None)

    lines: list[PricedLine] = []
    for idx, inp in enumerate(items):
        m = found.get(inp.menu_item_id)
        if m is None or m.branch_id != branch_id or m.is_archived:
            raise NotFound("An item in your cart no longer exists", code="item_not_found", )
        if not (1 <= inp.quantity <= MAX_LINE_QTY):
            raise Invalid(f"Quantity for {m.name} must be between 1 and {MAX_LINE_QTY}", code="invalid_quantity")
        cat = cats.get(m.category_id)
        if not m.is_available or (cat is not None and not cat.is_active):
            raise Conflict(f"{m.name} is currently unavailable", code="item_unavailable", details={"item_id": str(m.id), "name": m.name})
        live_variants = [v for v in variants if v.item_id == m.id and not v.is_archived]
        variant = None
        if live_variants:
            if inp.variant_id is None:
                raise Invalid(f"Please choose a size/variant for {m.name}", code="variant_required")
            variant = vmap.get(inp.variant_id)
            if variant is None or variant.item_id != m.id or variant.is_archived:
                raise Invalid(f"Invalid variant for {m.name}", code="invalid_variant")
            if not variant.is_available:
                raise Conflict(f"{m.name} ({variant.name}) is currently unavailable", code="item_unavailable")
        elif inp.variant_id is not None:
            raise Invalid(f"{m.name} has no variants", code="invalid_variant")
        chosen: list[tuple[MenuAddon, int]] = []
        seen: set[uuid.UUID] = set()
        for aid, q in inp.addons:
            a = amap.get(aid)
            if a is None or a.item_id != m.id or a.is_archived or aid in seen:
                raise Invalid(f"Invalid add-on for {m.name}", code="invalid_addon")
            seen.add(aid)
            if not a.is_available:
                raise Conflict(f"Add-on {a.name} is currently unavailable", code="item_unavailable")
            if not (1 <= q <= a.max_quantity):
                raise Invalid(f"Add-on {a.name}: quantity must be 1..{a.max_quantity}", code="invalid_addon")
            chosen.append((a, q))
        base = variant.price_paise if variant else m.price_paise
        unit = base + sum(a.price_paise * q for a, q in chosen)
        t = taxes.get(m.tax_setting_id) if m.tax_setting_id else default_tax
        lines.append(PricedLine(key=idx, item=m, variant=variant, addons=chosen, options=[(n[:60], v[:120]) for n, v in inp.options][:10],
                                quantity=inp.quantity, unit_price=unit, subtotal=unit * inp.quantity, notes=(inp.notes or None) and inp.notes[:300],
                                tax_name=t.name if t else None, tax_rate_bps=t.rate_bps if t else 0))

    subtotal = sum(l.subtotal for l in lines)
    cart_lines = [offer_svc.CartLine(key=l.key, item_id=l.item.id, category_id=l.item.category_id, subtotal_paise=l.subtotal) for l in lines]
    applied = await offer_svc.evaluate(db, lines=cart_lines, branch_id=branch_id, order_type=order_type, coupon_code=coupon_code,
                                       customer_id=customer_id, phone=phone, now=now)
    for a in applied:
        for k, v in a.allocations.items():
            lines[k].discount += v
    discount = sum(l.discount for l in lines)

    # delivery fee
    delivery_charge = 0
    if order_type == "delivery":
        if not cs.delivery_enabled:
            raise Conflict("Home delivery is currently unavailable", code="delivery_disabled")
        ds = await get_delivery_settings(db, branch_id)
        pc = (delivery_postal_code or "").strip()
        if ds.allowed_postal_codes and pc not in ds.allowed_postal_codes:
            raise Invalid("Sorry, we do not deliver to this postal code", code="outside_delivery_area")
        net = subtotal - discount
        if net < ds.min_order_paise:
            raise Invalid(f"Minimum order for delivery is ₹{ds.min_order_paise / 100:.2f}", code="below_min_delivery_order")
        delivery_charge = 0 if (ds.free_delivery_above_paise is not None and net >= ds.free_delivery_above_paise) else ds.base_charge_paise
        delivery_tax_bps = ds.tax_rate_bps
    else:
        delivery_tax_bps = 0
    if order_type == "pickup" and not cs.pickup_enabled:
        raise Conflict("Pickup is currently unavailable", code="pickup_disabled")

    inc = cs.prices_include_tax
    groups: dict[tuple[str, int], dict] = {}

    def add_group(name: str, bps: int, taxable: int, tax: int):
        g = groups.setdefault((name, bps), {"name": name, "rate_bps": bps, "taxable_paise": 0, "tax_paise": 0})
        g["taxable_paise"] += taxable
        g["tax_paise"] += tax

    for l in lines:
        base = l.subtotal - l.discount
        l.tax = tax_part_inclusive(base, l.tax_rate_bps) if inc else tax_exclusive(base, l.tax_rate_bps)
        l.total = base if inc else base + l.tax
        if l.tax_rate_bps:
            add_group(l.tax_name or "Tax", l.tax_rate_bps, base - l.tax if inc else base, l.tax)
    delivery_tax = 0
    if delivery_charge and delivery_tax_bps:
        delivery_tax = tax_part_inclusive(delivery_charge, delivery_tax_bps) if inc else tax_exclusive(delivery_charge, delivery_tax_bps)
        add_group("Delivery tax", delivery_tax_bps, delivery_charge - delivery_tax if inc else delivery_charge, delivery_tax)
    tax_total = sum(l.tax for l in lines) + delivery_tax
    items_total = sum(l.total for l in lines)
    total = items_total + delivery_charge + (0 if inc else delivery_tax)
    return PricedCart(lines=lines, subtotal=subtotal, discount=discount, tax=tax_total, delivery_charge=delivery_charge, delivery_tax=delivery_tax,
                      total=total, tax_breakdown=list(groups.values()),
                      discount_breakdown=[{"offer_id": str(a.offer.id), "name": a.offer.name, "code": a.coupon.code if a.coupon else None,
                                           "discount_paise": a.discount_paise} for a in applied],
                      applied=applied, prices_include_tax=inc, currency=cs.currency, coupon_code=applied[0].coupon.code if applied and applied[0].coupon else None)


def cart_to_dict(c: PricedCart) -> dict:
    return {
        "currency": c.currency, "prices_include_tax": c.prices_include_tax, "subtotal_paise": c.subtotal, "discount_paise": c.discount,
        "tax_paise": c.tax, "delivery_charge_paise": c.delivery_charge, "total_paise": c.total, "tax_breakdown": c.tax_breakdown,
        "discounts": c.discount_breakdown, "coupon_code": c.coupon_code,
        "lines": [{"menu_item_id": str(l.item.id), "name": l.item.name, "variant": l.variant.name if l.variant else None, "quantity": l.quantity,
                   "unit_price_paise": l.unit_price, "subtotal_paise": l.subtotal, "discount_paise": l.discount, "tax_paise": l.tax,
                   "tax_rate_bps": l.tax_rate_bps, "total_paise": l.total} for l in c.lines],
    }
