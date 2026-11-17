"""Public (unauthenticated) endpoints: menu, QR resolve, public settings, offers and coupon validation."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import public_branch
from app.core.errors import Invalid, NotFound
from app.core.permissions import Principal, optional_customer
from app.core.ratelimit import limit
from app.core.security import now_utc
from app.db.session import get_db
from app.models.core import Branch
from app.models.menu import MenuAddon, MenuItem, MenuItemVariant
from app.schemas.orders import QuoteRequest
from app.services import offers as offer_svc, pricing, settings as cfg, tables as table_svc
from app.services.menu import build_menu, item_dict
from app.utils.phone import normalize_phone

menu_router = APIRouter(prefix="/menu", tags=["menu"])
qr_router = APIRouter(prefix="/qr", tags=["qr"])
settings_router = APIRouter(prefix="/settings", tags=["settings"])
offers_router = APIRouter(tags=["offers"])


@menu_router.get("")
async def get_menu(branch: Branch = Depends(public_branch), db: AsyncSession = Depends(get_db)):
    """Active customer-facing menu. Unavailable items are included with is_available=false so apps can grey them out."""
    return {"branch_id": branch.id, "categories": [c for c in await build_menu(db, branch.id) if c["items"]]}


@menu_router.get("/items/{item_id}")
async def get_item(item_id: uuid.UUID, branch: Branch = Depends(public_branch), db: AsyncSession = Depends(get_db)):
    m = await db.get(MenuItem, item_id)
    if m is None or m.is_archived or m.branch_id != branch.id:
        raise NotFound("Item not found")
    vs = (await db.execute(select(MenuItemVariant).where(MenuItemVariant.item_id == m.id))).scalars().all()
    ads = (await db.execute(select(MenuAddon).where(MenuAddon.item_id == m.id))).scalars().all()
    return item_dict(m, list(vs), list(ads))


class QrResolve(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    pin: str | None = Field(default=None, max_length=12)


@qr_router.post("/resolve", dependencies=[Depends(limit("qr", 30, 60))])
async def resolve_qr(body: QrResolve, db: AsyncSession = Depends(get_db)):
    """Exchange a scanned QR token for a short-lived table-session token used when placing dine-in orders."""
    return await table_svc.resolve_qr(db, body.token, body.pin)


@settings_router.get("/public")
async def public_settings(branch: Branch = Depends(public_branch), db: AsyncSession = Depends(get_db)):
    cs = await cfg.get_cafe_settings(db, branch.id)
    ds = await cfg.get_delivery_settings(db, branch.id)
    open_now, why = cfg.is_open_now(cs, now_utc())
    return {"name": cs.name, "phone": cs.phone, "address": cs.address, "currency": cs.currency, "timezone": cs.timezone, "open_now": open_now,
            "closed_reason": why, "opening_hours": cs.opening_hours, "dine_in_enabled": cs.dine_in_enabled, "pickup_enabled": cs.pickup_enabled,
            "delivery_enabled": cs.delivery_enabled, "guest_checkout_enabled": cs.guest_checkout_enabled, "prices_include_tax": cs.prices_include_tax,
            "payment_methods": cfg.available_payment_methods(cs), "cash_on_delivery_enabled": cs.cash_on_delivery_enabled,
            "delivery": {"base_charge_paise": ds.base_charge_paise, "free_delivery_above_paise": ds.free_delivery_above_paise,
                         "min_order_paise": ds.min_order_paise, "allowed_postal_codes": ds.allowed_postal_codes},
            "rider_location_update_interval_seconds": cs.location_update_interval_seconds}


async def _quote_for(db: AsyncSession, branch: Branch, body: QuoteRequest, customer: Principal | None, coupon: str | None):
    phone = None
    if customer is not None:
        phone = customer.user.phone
    elif body.customer_phone:
        phone = normalize_phone(body.customer_phone)
    items = [pricing.ItemInput(menu_item_id=i.menu_item_id, quantity=i.quantity, variant_id=i.variant_id, addons=[(a.addon_id, a.quantity) for a in i.addons],
                               options=[(o.name, o.value) for o in i.options], notes=i.notes) for i in body.items]
    return await pricing.price_cart(db, branch_id=branch.id, items=items, order_type=body.order_type, coupon_code=coupon,
                                    customer_id=customer.id if customer else None, phone=phone,
                                    delivery_postal_code=body.delivery_address.postal_code if body.delivery_address else None)


@offers_router.post("/orders/quote", tags=["orders"], dependencies=[Depends(limit("quote", 60, 60))])
async def quote(body: QuoteRequest, branch: Branch = Depends(public_branch), customer: Principal | None = Depends(optional_customer),
                db: AsyncSession = Depends(get_db)):
    """Price preview computed by the server (items, discount, tax, delivery, total). Clients must display these numbers."""
    return pricing.cart_to_dict(await _quote_for(db, branch, body, customer, body.coupon_code))


@offers_router.get("/offers")
async def list_offers(branch: Branch = Depends(public_branch), customer: Principal | None = Depends(optional_customer), phone: str | None = None,
                      db: AsyncSession = Depends(get_db)):
    """Currently running offers a customer could use. Per-customer limits / first-order rules are evaluated when identity is known."""
    now = now_utc()
    cid = customer.id if customer else None
    ph = customer.user.phone if customer else (normalize_phone(phone) if phone else None)
    key = offer_svc.redeemer_key(cid, ph)
    out = []
    from app.models.offers import Coupon

    for o in await offer_svc.active_offers(db, branch.id, now):
        if not o.is_listed:
            continue
        if o.total_redemption_limit is not None and await offer_svc._count(db, o.id, now) >= o.total_redemption_limit:
            continue
        eligible, why = True, None
        if o.per_customer_limit is not None and key and await offer_svc._count(db, o.id, now, key) >= o.per_customer_limit:
            eligible, why = False, "You have already used this offer"
        if o.first_order_only and (cid or ph) and await offer_svc.has_prior_orders(db, cid, ph):
            eligible, why = False, "Only for your first order"
        code = (await db.execute(select(Coupon.code).where(Coupon.offer_id == o.id, Coupon.is_active.is_(True)).limit(1))).scalar_one_or_none()
        out.append({"id": o.id, "name": o.name, "description": o.description, "discount_type": o.discount_type, "value": o.value,
                    "min_order_paise": o.min_order_paise, "max_discount_paise": o.max_discount_paise, "ends_at": o.ends_at, "order_types": o.order_types,
                    "coupon_code": code, "applied_automatically": code is None, "eligible": eligible, "reason": why})
    return out


class CouponValidate(QuoteRequest):
    coupon_code: str = Field(min_length=3, max_length=40)


@offers_router.post("/coupons/validate", dependencies=[Depends(limit("coupon", 20, 60))])
async def validate_coupon(body: CouponValidate, branch: Branch = Depends(public_branch), customer: Principal | None = Depends(optional_customer),
                          db: AsyncSession = Depends(get_db)):
    """Always HTTP 200: {valid: false, reason} explains why a coupon cannot be used; valid=true returns the server-calculated quote."""
    try:
        q = await _quote_for(db, branch, body, customer, body.coupon_code.strip().upper())
    except Invalid as e:
        if e.code in ("coupon_invalid", "coupon_exhausted"):
            return {"valid": False, "reason": e.message, "code": e.code}
        raise
    if not q.coupon_code:
        return {"valid": False, "reason": "This coupon cannot be combined with your current offers", "code": "coupon_invalid"}
    return {"valid": True, "coupon_code": q.coupon_code, "discount_paise": q.discount, "quote": pricing.cart_to_dict(q)}
