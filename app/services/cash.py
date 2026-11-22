"""Cash held by riders: summary, ledger and hand-over to the cafe. The ledger is append-only."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Invalid, NotFound
from app.models.core import Rider
from app.models.payments import RiderCashLedger
from app.services import events as ev
from app.services import payments
from app.services.audit import audit


async def summary(db: AsyncSession, branch_id: uuid.UUID, start: datetime | None = None, end: datetime | None = None) -> dict:
    riders = (await db.execute(select(Rider).where(Rider.branch_id == branch_id).order_by(Rider.name))).scalars().all()
    totals: dict[uuid.UUID, dict[str, int]] = {}
    for rider_id, kind, amount in (await db.execute(select(RiderCashLedger.rider_id, RiderCashLedger.entry_type, func.coalesce(func.sum(RiderCashLedger.amount_paise), 0))
                                                    .where(RiderCashLedger.branch_id == branch_id).group_by(RiderCashLedger.rider_id, RiderCashLedger.entry_type))).all():
        totals.setdefault(rider_id, {})[kind] = int(amount)
    in_period: dict[uuid.UUID, int] = {}
    if start or end:
        q = select(RiderCashLedger.rider_id, func.coalesce(func.sum(RiderCashLedger.amount_paise), 0)).where(RiderCashLedger.branch_id == branch_id, RiderCashLedger.entry_type == "collected")
        if start:
            q = q.where(RiderCashLedger.created_at >= start)
        if end:
            q = q.where(RiderCashLedger.created_at < end)
        in_period = {rid: int(v) for rid, v in (await db.execute(q.group_by(RiderCashLedger.rider_id))).all()}
    rows = []
    for r in riders:
        t = totals.get(r.id, {})
        collected, handed, reversed_ = t.get("collected", 0), t.get("handover", 0), t.get("reversal", 0)
        rows.append({"rider_id": r.id, "name": r.name, "phone": r.phone, "collected_paise": collected, "handed_over_paise": handed,
                     "reversed_paise": reversed_, "outstanding_paise": collected - handed - reversed_,
                     **({"collected_in_period_paise": in_period.get(r.id, 0)} if (start or end) else {})})
    return {"riders": rows, "total_outstanding_paise": sum(x["outstanding_paise"] for x in rows)}


async def ledger(db: AsyncSession, rider_id: uuid.UUID, limit: int, offset: int) -> dict:
    if await db.get(Rider, rider_id) is None:
        raise NotFound("Rider not found")
    total = (await db.execute(select(func.count()).select_from(RiderCashLedger).where(RiderCashLedger.rider_id == rider_id))).scalar_one()
    rows = (await db.execute(select(RiderCashLedger).where(RiderCashLedger.rider_id == rider_id).order_by(RiderCashLedger.id.desc()).limit(limit).offset(offset))).scalars().all()
    return {"total": total, "outstanding_paise": await payments.rider_cash_outstanding(db, rider_id),
            "items": [{"id": r.id, "entry_type": r.entry_type, "amount_paise": r.amount_paise, "order_id": r.order_id, "payment_id": r.payment_id,
                       "note": r.note, "created_by_role": r.created_by_role, "created_at": r.created_at} for r in rows]}


async def handover(db: AsyncSession, admin_id: uuid.UUID, rider_id: uuid.UUID, amount_paise: int, note: str | None) -> dict:
    rider = await db.get(Rider, rider_id)
    if rider is None:
        raise NotFound("Rider not found")
    if amount_paise <= 0:
        raise Invalid("Amount must be greater than zero", code="invalid_amount")
    outstanding = await payments.rider_cash_outstanding(db, rider_id)
    if amount_paise > outstanding:
        raise Conflict(f"The rider only holds ₹{outstanding / 100:.2f}", code="handover_exceeds_outstanding", details={"outstanding_paise": outstanding})
    row = RiderCashLedger(branch_id=rider.branch_id, rider_id=rider_id, entry_type="handover", amount_paise=amount_paise,
                          note=(note or "").strip()[:300] or None, created_by_role="admin", created_by_id=admin_id)
    db.add(row)
    await db.flush()
    await audit(db, actor_role="admin", actor_id=admin_id, action="rider.cash_handover", entity_type="rider", entity_id=rider_id,
                after={"amount_paise": amount_paise, "note": row.note}, branch_id=rider.branch_id)
    left = outstanding - amount_paise
    await ev.emit(db, type="cash.handover_confirmed", topics=[f"account:rider:{rider_id}"], dedupe_key=f"cash.handover:{row.id}",
                  recipients=[{"role": "rider", "id": str(rider_id)}], title="Cash handover confirmed",
                  body=f"The cafe recorded your handover of ₹{amount_paise / 100:,.2f}." + (f" You still hold ₹{left / 100:,.2f}." if left > 0 else " You hold no cash now."),
                  data={"amount_paise": amount_paise, "outstanding_paise": left}, branch_id=rider.branch_id)
    return {"id": row.id, "outstanding_paise": left}
