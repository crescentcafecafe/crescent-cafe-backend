from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Invalid, NotFound
from app.integrations import registry
from app.models.core import Branch
from app.models.settings import CafeSettings, DeliverySettings, TaxSetting
from app.utils.timeutil import to_local

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


async def resolve_branch(db: AsyncSession, code: str | None = None) -> Branch:
    """Branch lookup (no hardcoded branch ids). With a single branch the default is returned."""
    q = select(Branch).where(Branch.is_active.is_(True))
    if code:
        q = q.where(Branch.code == code)
    b = (await db.execute(q.order_by(Branch.code).limit(1))).scalar_one_or_none()
    if b is None:
        raise NotFound("Branch not configured. Run the bootstrap script.", code="branch_missing")
    return b


async def get_cafe_settings(db: AsyncSession, branch_id: uuid.UUID) -> CafeSettings:
    cs = (await db.execute(select(CafeSettings).where(CafeSettings.branch_id == branch_id))).scalar_one_or_none()
    if cs is None:
        cs = CafeSettings(branch_id=branch_id)
        db.add(cs)
        await db.flush()
    return cs


async def get_delivery_settings(db: AsyncSession, branch_id: uuid.UUID) -> DeliverySettings:
    ds = (await db.execute(select(DeliverySettings).where(DeliverySettings.branch_id == branch_id))).scalar_one_or_none()
    if ds is None:
        ds = DeliverySettings(branch_id=branch_id)
        db.add(ds)
        await db.flush()
    return ds


async def get_default_tax(db: AsyncSession, branch_id: uuid.UUID) -> TaxSetting | None:
    return (await db.execute(select(TaxSetting).where(TaxSetting.branch_id == branch_id, TaxSetting.is_default.is_(True),
                                                      TaxSetting.is_active.is_(True)).limit(1))).scalar_one_or_none()


def validate_opening_hours(hours: dict) -> None:
    for day, slots in hours.items():
        if day not in DAYS:
            raise Invalid(f"Unknown day '{day}' in opening hours")
        for s in slots:
            try:
                datetime.strptime(s["open"], "%H:%M")
                datetime.strptime(s["close"], "%H:%M")
            except Exception:
                raise Invalid("Opening hours must look like {'open': '09:00', 'close': '22:00'}")


def is_open_now(cs: CafeSettings, now: datetime) -> tuple[bool, str | None]:
    if cs.temporarily_closed:
        return False, cs.temporary_closure_message or "The cafe is temporarily closed"
    if not cs.accepting_orders:
        return False, "The cafe is not accepting orders right now"
    if not cs.enforce_opening_hours or not cs.opening_hours:
        return True, None
    local = to_local(now, cs.timezone)
    for c in cs.closures or []:
        if c.get("date") == local.date().isoformat():
            return False, c.get("reason") or "Closed today"
    slots = cs.opening_hours.get(DAYS[local.weekday()], [])
    cur = local.strftime("%H:%M")
    for s in slots:
        if s["open"] <= s["close"]:
            if s["open"] <= cur < s["close"]:
                return True, None
        elif cur >= s["open"] or cur < s["close"]:  # overnight slot
            return True, None
    return False, "The cafe is currently closed"


def available_gateways(cs: CafeSettings) -> list[str]:
    out = []
    if cs.razorpay_enabled and registry.get_gateway("razorpay").is_configured():
        out.append("razorpay")
    if cs.cashfree_enabled and registry.get_gateway("cashfree").is_configured():
        out.append("cashfree")
    return out


def available_payment_methods(cs: CafeSettings) -> list[str]:
    return available_gateways(cs) + (["cash"] if cs.cash_enabled else [])
