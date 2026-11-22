"""Admin: cafe / tax / delivery settings, gateway switches, reports, invoices."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import page
from app.core.errors import Invalid, NotFound
from app.core.permissions import Principal, current_admin
from app.core.security import now_utc
from app.db.session import get_db
from app.integrations import registry
from app.models.invoices import Invoice
from app.models.settings import TaxSetting
from app.schemas.admin import CafeSettingsPatch, DeliverySettingsPatch, GatewayConfigIn, TaxIn, TaxPatch
from app.services import invoices as inv_svc, reports, settings as cfg
from app.services.audit import audit
from app.utils.timeutil import to_local

router = APIRouter(prefix="/admin", tags=["admin-settings"], dependencies=[Depends(current_admin)])
reports_router = APIRouter(prefix="/admin/reports", tags=["reports"], dependencies=[Depends(current_admin)])


def _cs(cs) -> dict:
    skip = {"id", "branch_id", "updated_at"}
    return {c.name: getattr(cs, c.name) for c in cs.__table__.columns if c.name not in skip}


@router.get("/settings/cafe")
async def get_cafe(a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return _cs(await cfg.get_cafe_settings(db, a.user.branch_id))


@router.patch("/settings/cafe")
async def patch_cafe(body: CafeSettingsPatch, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    cs = await cfg.get_cafe_settings(db, a.user.branch_id)
    data = body.model_dump(exclude_unset=True)
    if "opening_hours" in data and data["opening_hours"] is not None:
        cfg.validate_opening_hours(data["opening_hours"])
    if "timezone" in data and data["timezone"]:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(data["timezone"])
        except Exception:
            raise Invalid("Unknown timezone", code="invalid_timezone")
    before = {k: str(getattr(cs, k)) for k in data}
    for k, v in data.items():
        setattr(cs, k, v)
    await audit(db, actor_role="admin", actor_id=a.id, action="settings.cafe_updated", entity_type="cafe_settings", entity_id=cs.id,
                before=before, after={k: str(v) for k, v in data.items()}, branch_id=cs.branch_id)
    return _cs(cs)


@router.get("/settings/delivery")
async def get_delivery(a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    ds = await cfg.get_delivery_settings(db, a.user.branch_id)
    return {"base_charge_paise": ds.base_charge_paise, "free_delivery_above_paise": ds.free_delivery_above_paise, "min_order_paise": ds.min_order_paise,
            "allowed_postal_codes": ds.allowed_postal_codes, "tax_rate_bps": ds.tax_rate_bps}


@router.patch("/settings/delivery")
async def patch_delivery(body: DeliverySettingsPatch, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    ds = await cfg.get_delivery_settings(db, a.user.branch_id)
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(ds, k, v)
    await audit(db, actor_role="admin", actor_id=a.id, action="settings.delivery_updated", entity_type="delivery_settings", entity_id=ds.id, after={k: str(v) for k, v in data.items()})
    return await get_delivery(a, db)


def _tax(t: TaxSetting) -> dict:
    return {"id": t.id, "name": t.name, "rate_bps": t.rate_bps, "rate_percent": t.rate_bps / 100, "is_default": t.is_default, "is_active": t.is_active}


async def _only_default(db: AsyncSession, branch_id: uuid.UUID, keep: uuid.UUID) -> None:
    for t in (await db.execute(select(TaxSetting).where(TaxSetting.branch_id == branch_id, TaxSetting.id != keep, TaxSetting.is_default.is_(True)))).scalars().all():
        t.is_default = False


@router.get("/settings/taxes")
async def list_taxes(a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return [_tax(t) for t in (await db.execute(select(TaxSetting).where(TaxSetting.branch_id == a.user.branch_id).order_by(TaxSetting.name))).scalars().all()]


@router.post("/settings/taxes", status_code=201)
async def create_tax(body: TaxIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Tax classes are configurable; items may point at a class, otherwise the default class applies. The owner must verify rates."""
    t = TaxSetting(branch_id=a.user.branch_id, **body.model_dump())
    db.add(t)
    await db.flush()
    if t.is_default:
        await _only_default(db, t.branch_id, t.id)
    await audit(db, actor_role="admin", actor_id=a.id, action="settings.tax_created", entity_type="tax", entity_id=t.id, after=body.model_dump(), branch_id=t.branch_id)
    return _tax(t)


@router.patch("/settings/taxes/{tax_id}")
async def patch_tax(tax_id: uuid.UUID, body: TaxPatch, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    t = await db.get(TaxSetting, tax_id)
    if t is None or t.branch_id != a.user.branch_id:
        raise NotFound("Tax setting not found")
    data = body.model_dump(exclude_unset=True)
    before = {k: str(getattr(t, k)) for k in data}
    for k, v in data.items():
        setattr(t, k, v)
    if t.is_default:
        await _only_default(db, t.branch_id, t.id)
    await audit(db, actor_role="admin", actor_id=a.id, action="settings.tax_updated", entity_type="tax", entity_id=t.id, before=before, after={k: str(v) for k, v in data.items()}, branch_id=t.branch_id)
    return _tax(t)


@router.get("/payment-gateways")
async def gateways(a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Gateway switches + whether server credentials are present. Secret keys are never returned."""
    cs = await cfg.get_cafe_settings(db, a.user.branch_id)
    return {"razorpay": {"enabled": cs.razorpay_enabled, "configured": registry.get_gateway("razorpay").is_configured()},
            "cashfree": {"enabled": cs.cashfree_enabled, "configured": registry.get_gateway("cashfree").is_configured()},
            "cash": {"enabled": cs.cash_enabled, "cash_on_delivery_enabled": cs.cash_on_delivery_enabled},
            "offered_to_customers": cfg.available_payment_methods(cs)}


@router.put("/payment-gateways")
async def set_gateways(body: GatewayConfigIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    cs = await cfg.get_cafe_settings(db, a.user.branch_id)
    data = body.model_dump(exclude_unset=True)
    for name in ("razorpay", "cashfree"):
        if data.get(f"{name}_enabled") and not registry.get_gateway(name).is_configured():
            raise Invalid(f"{name} credentials are not configured on the server (set the environment variables first)", code="gateway_not_configured")
    after_cash = data.get("cash_enabled", cs.cash_enabled)
    after_rz, after_cf = data.get("razorpay_enabled", cs.razorpay_enabled), data.get("cashfree_enabled", cs.cashfree_enabled)
    if not (after_cash or after_rz or after_cf):
        raise Invalid("At least one payment method must stay enabled", code="no_payment_method")
    before = {k: getattr(cs, k) for k in data}
    for k, v in data.items():
        setattr(cs, k, v)
    await audit(db, actor_role="admin", actor_id=a.id, action="settings.gateways_updated", entity_type="cafe_settings", entity_id=cs.id, before=before, after=data)
    return await gateways(a, db)


# ================================================================= reports
def _range(start: date | None, end: date | None, tzname: str) -> tuple[date, date]:
    today = to_local(now_utc(), tzname).date()
    start, end = start or today.replace(day=1), end or today
    if end < start:
        raise Invalid("end must not be before start", code="invalid_range")
    if (end - start).days > 400:
        raise Invalid("Date range is limited to 400 days", code="range_too_large")
    return start, end


@reports_router.get("/summary")
async def summary(start: date | None = None, end: date | None = None, granularity: str = "day", a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Daily/monthly sales, order counts, by type & payment method, cash collection, gateway summaries, coupons, refunds, best sellers."""
    cs = await cfg.get_cafe_settings(db, a.user.branch_id)
    s, e = _range(start, end, cs.timezone)
    return await reports.build(db, a.user.branch_id, s, e, cs.timezone, "month" if granularity == "month" else "day")


@reports_router.get("/export")
async def export(kind: str = "sales", start: date | None = None, end: date | None = None, granularity: str = "day", a: Principal = Depends(current_admin),
                 db: AsyncSession = Depends(get_db)):
    if kind not in ("sales", "items", "payments", "coupons"):
        raise Invalid("kind must be sales, items, payments or coupons", code="invalid_kind")
    cs = await cfg.get_cafe_settings(db, a.user.branch_id)
    s, e = _range(start, end, cs.timezone)
    data = await reports.build(db, a.user.branch_id, s, e, cs.timezone, "month" if granularity == "month" else "day")
    await audit(db, actor_role="admin", actor_id=a.id, action="report.exported", entity_type="report", after={"kind": kind, "start": str(s), "end": str(e)})
    return Response(reports.to_csv(kind, data), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{kind}_{s}_{e}.csv"'})


@router.get("/invoices")
async def list_invoices(limit: int = 50, offset: int = 0, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    limit, offset = page(limit, offset)
    total = (await db.execute(select(func.count()).select_from(Invoice).where(Invoice.branch_id == a.user.branch_id))).scalar_one()
    rows = (await db.execute(select(Invoice).where(Invoice.branch_id == a.user.branch_id).order_by(Invoice.issued_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [{"id": i.id, "invoice_number": i.invoice_number, "order_id": i.order_id, "total_paise": i.total_paise, "issued_at": i.issued_at} for i in rows],
            "total": total, "limit": limit, "offset": offset}
