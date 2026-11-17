"""Offer / coupon rules, eligibility explanations, and safe reservation/consumption of redemptions."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Invalid
from app.core.security import now_utc
from app.models.offers import Coupon, CouponRedemption, Offer
from app.models.orders import Order


@dataclass
class CartLine:
    key: int
    item_id: uuid.UUID
    category_id: uuid.UUID
    subtotal_paise: int
    discount_paise: int = 0


@dataclass
class AppliedOffer:
    offer: Offer
    coupon: Coupon | None
    discount_paise: int
    allocations: dict[int, int] = field(default_factory=dict)  # line key -> paise


def half_up_pct(base: int, bps: int) -> int:
    return (base * bps + 5000) // 10000


def redeemer_key(customer_id: uuid.UUID | None, phone: str | None) -> str | None:
    if customer_id:
        return f"c:{customer_id}"
    if phone:
        return f"p:{phone}"
    return None


def _live_redemption_filter(now: datetime):
    return or_(CouponRedemption.status == "consumed",
               and_(CouponRedemption.status == "reserved", or_(CouponRedemption.reserved_until.is_(None), CouponRedemption.reserved_until > now)))


async def _count(db: AsyncSession, offer_id: uuid.UUID, now: datetime, key: str | None = None) -> int:
    q = select(func.count()).select_from(CouponRedemption).where(CouponRedemption.offer_id == offer_id, _live_redemption_filter(now))
    if key:
        q = q.where(CouponRedemption.redeemer_key == key)
    return (await db.execute(q)).scalar_one()


async def has_prior_orders(db: AsyncSession, customer_id: uuid.UUID | None, phone: str | None, exclude_order: uuid.UUID | None = None) -> bool:
    conds = []
    if customer_id:
        conds.append(Order.customer_id == customer_id)
    if phone:
        conds.append(Order.customer_phone == phone)
    if not conds:
        return False
    q = select(func.count()).select_from(Order).where(or_(*conds), Order.status.not_in(("rejected", "cancelled", "payment_pending", "refunded")))
    if exclude_order:
        q = q.where(Order.id != exclude_order)
    return (await db.execute(q)).scalar_one() > 0


def _eligible_lines(offer: Offer, lines: list[CartLine]) -> list[CartLine]:
    items, cats = set(offer.item_ids or []), set(offer.category_ids or [])
    if not items and not cats:
        return list(lines)
    return [l for l in lines if str(l.item_id) in items or str(l.category_id) in cats]


async def check_offer(db: AsyncSession, offer: Offer, *, lines: list[CartLine], branch_id: uuid.UUID, order_type: str,
                      key: str | None, customer_id: uuid.UUID | None, phone: str | None, now: datetime) -> tuple[AppliedOffer | None, str | None]:
    """Return (applied, None) or (None, human readable reason)."""
    if offer.branch_id and offer.branch_id != branch_id:
        return None, "This offer is not valid at this branch"
    if offer.status == "paused":
        return None, "This offer is currently paused"
    if offer.status == "expired" or (offer.ends_at and offer.ends_at < now):
        return None, "This offer has expired"
    if offer.starts_at and offer.starts_at > now:
        return None, "This offer has not started yet"
    if offer.order_types and order_type not in offer.order_types:
        return None, "This offer is not valid for this order type"
    subtotal = sum(l.subtotal_paise for l in lines)
    if subtotal < offer.min_order_paise:
        return None, f"Add items worth ₹{(offer.min_order_paise - subtotal) / 100:.2f} more to use this offer"
    elig = _eligible_lines(offer, lines)
    base = sum(l.subtotal_paise for l in elig)
    if base <= 0:
        return None, "No items in your cart qualify for this offer"
    if offer.first_order_only and await has_prior_orders(db, customer_id, phone):
        return None, "This offer is only for your first order"
    if offer.total_redemption_limit is not None and await _count(db, offer.id, now) >= offer.total_redemption_limit:
        return None, "This offer has reached its redemption limit"
    if offer.per_customer_limit is not None:
        if key is None:
            return None, "Sign in or enter your mobile number to use this offer"
        if await _count(db, offer.id, now, key) >= offer.per_customer_limit:
            return None, "You have already used this offer the maximum number of times"
    disc = half_up_pct(base, offer.value) if offer.discount_type == "percent" else min(offer.value, base)
    if offer.max_discount_paise is not None:
        disc = min(disc, offer.max_discount_paise)
    disc = min(disc, base)
    if disc <= 0:
        return None, "This offer gives no discount on your cart"
    alloc = _allocate(disc, elig)
    return AppliedOffer(offer=offer, coupon=None, discount_paise=disc, allocations=alloc), None


def _allocate(total: int, lines: list[CartLine]) -> dict[int, int]:
    """Proportional allocation with largest-remainder so the parts sum exactly to total."""
    base = sum(l.subtotal_paise for l in lines)
    parts = [(l.key, total * l.subtotal_paise // base, (total * l.subtotal_paise) % base) for l in lines]
    rem = total - sum(p[1] for p in parts)
    order = sorted(range(len(parts)), key=lambda i: -parts[i][2])
    out = {p[0]: p[1] for p in parts}
    for i in order[:rem]:
        out[parts[i][0]] += 1
    return out


async def active_offers(db: AsyncSession, branch_id: uuid.UUID, now: datetime) -> list[Offer]:
    q = select(Offer).where(Offer.status == "active", or_(Offer.branch_id.is_(None), Offer.branch_id == branch_id),
                            or_(Offer.starts_at.is_(None), Offer.starts_at <= now), or_(Offer.ends_at.is_(None), Offer.ends_at >= now))
    return list((await db.execute(q)).scalars().all())


async def evaluate(db: AsyncSession, *, lines: list[CartLine], branch_id: uuid.UUID, order_type: str, coupon_code: str | None,
                   customer_id: uuid.UUID | None, phone: str | None, now: datetime | None = None) -> list[AppliedOffer]:
    now = now or now_utc()
    key = redeemer_key(customer_id, phone)
    applied: list[AppliedOffer] = []
    coupon_offer_ids: set[uuid.UUID] = set()
    if coupon_code:
        code = coupon_code.strip().upper()
        coupon = (await db.execute(select(Coupon).where(Coupon.code == code))).scalar_one_or_none()
        if coupon is None:
            raise Invalid("This coupon code does not exist", code="coupon_invalid")
        if not coupon.is_active:
            raise Invalid("This coupon is no longer active", code="coupon_invalid")
        offer = await db.get(Offer, coupon.offer_id)
        res, why = await check_offer(db, offer, lines=lines, branch_id=branch_id, order_type=order_type, key=key,
                                     customer_id=customer_id, phone=phone, now=now)
        if res is None:
            raise Invalid(why or "Coupon cannot be applied", code="coupon_invalid")
        res.coupon = coupon
        applied.append(res)
    coupon_backed = {c for (c,) in (await db.execute(select(Coupon.offer_id).where(Coupon.is_active.is_(True)))).all()}
    autos: list[AppliedOffer] = []
    for o in await active_offers(db, branch_id, now):
        if o.id in coupon_backed or any(a.offer.id == o.id for a in applied):
            continue  # code-only offers are never auto applied
        r, _ = await check_offer(db, o, lines=lines, branch_id=branch_id, order_type=order_type, key=key,
                                 customer_id=customer_id, phone=phone, now=now)
        if r:
            autos.append(r)
    for r in sorted(autos, key=lambda a: -a.discount_paise):
        if not applied or (all(a.offer.stackable for a in applied) and r.offer.stackable):
            applied.append(r)
    # never discount more than the line value
    spent: dict[int, int] = {}
    cap = {l.key: l.subtotal_paise for l in lines}
    for a in applied:
        for k, v in list(a.allocations.items()):
            v2 = min(v, cap[k] - spent.get(k, 0))
            a.allocations[k] = v2
            spent[k] = spent.get(k, 0) + v2
        a.discount_paise = sum(a.allocations.values())
    return [a for a in applied if a.discount_paise > 0]


async def reserve(db: AsyncSession, order: Order, applied: list[AppliedOffer], *, key: str | None, hold_minutes: int | None) -> None:
    """Re-check limits under a row lock and create redemptions. hold_minutes=None => consumed immediately (cash orders)."""
    now = now_utc()
    for a in sorted(applied, key=lambda x: str(x.offer.id)):  # deterministic lock order avoids deadlocks
        offer = (await db.execute(select(Offer).where(Offer.id == a.offer.id).with_for_update())).scalar_one()
        if offer.total_redemption_limit is not None and await _count(db, offer.id, now) >= offer.total_redemption_limit:
            raise Invalid("This offer has just reached its redemption limit", code="coupon_exhausted")
        if offer.per_customer_limit is not None and key and await _count(db, offer.id, now, key) >= offer.per_customer_limit:
            raise Invalid("You have already used this offer the maximum number of times", code="coupon_exhausted")
        db.add(CouponRedemption(offer_id=offer.id, coupon_id=a.coupon.id if a.coupon else None, order_id=order.id,
                                customer_id=order.customer_id, redeemer_key=key or f"o:{order.id}", discount_paise=a.discount_paise,
                                status="reserved" if hold_minutes else "consumed",
                                reserved_until=now + timedelta(minutes=hold_minutes) if hold_minutes else None,
                                consumed_at=None if hold_minutes else now))
    await db.flush()


async def consume_for_order(db: AsyncSession, order_id: uuid.UUID) -> None:
    now = now_utc()
    rows = (await db.execute(select(CouponRedemption).where(CouponRedemption.order_id == order_id, CouponRedemption.status == "reserved"))).scalars().all()
    for r in rows:
        r.status, r.consumed_at, r.reserved_until = "consumed", now, None


async def release_for_order(db: AsyncSession, order_id: uuid.UUID) -> None:
    now = now_utc()
    rows = (await db.execute(select(CouponRedemption).where(CouponRedemption.order_id == order_id, CouponRedemption.status.in_(("reserved", "consumed"))))).scalars().all()
    for r in rows:
        r.status, r.released_at = "released", now
