from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Principal, current_customer
from app.core.security import now_utc
from app.db.session import get_db
from app.models.core import Customer, Device
from app.schemas.auth import PhoneUpdate, ProfileUpdate
from app.schemas.common import OK
from app.services import auth as auth_svc
from app.utils.phone import normalize_phone

router = APIRouter(prefix="/customers", tags=["customers"])


def _me(c: Customer) -> dict:
    return {"id": c.id, "name": c.name, "email": c.email, "phone": c.phone, "phone_required": c.phone is None, "avatar_url": c.avatar_url,
            "marketing_opt_in": c.marketing_opt_in, "has_password": c.password_hash is not None, "google_linked": c.google_sub is not None,
            "created_at": c.created_at}


@router.get("/me")
async def me(p: Principal = Depends(current_customer)):
    return _me(p.user)


@router.patch("/me")
async def update_me(body: ProfileUpdate, p: Principal = Depends(current_customer)):
    c: Customer = p.user
    if body.name is not None:
        c.name = body.name.strip()
    if body.marketing_opt_in is not None:
        c.marketing_opt_in = body.marketing_opt_in
    return _me(c)


@router.put("/me/phone")
async def set_phone(body: PhoneUpdate, p: Principal = Depends(current_customer)):
    """Mobile number is mandatory before ordering (collected right after Google / email sign-in)."""
    p.user.phone = normalize_phone(body.phone)
    return _me(p.user)


@router.get("/me/devices")
async def my_devices(p: Principal = Depends(current_customer), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Device).where(Device.owner_role == "customer", Device.owner_id == p.id, Device.is_active.is_(True)))).scalars().all()
    return [{"install_id": d.install_id, "platform": d.platform, "device_model": d.device_model, "app_version": d.app_version, "last_seen_at": d.last_seen_at} for d in rows]


@router.delete("/me", response_model=OK)
async def delete_account(p: Principal = Depends(current_customer), db: AsyncSession = Depends(get_db)):
    """Right-to-erasure: personal data is anonymised; order records are kept for accounting."""
    c: Customer = p.user
    c.name, c.email, c.phone, c.google_sub, c.password_hash, c.avatar_url = "Deleted user", None, None, None, None, None
    c.is_active, c.anonymized_at, c.marketing_opt_in = False, now_utc(), False
    await auth_svc.revoke_all_sessions(db, "customer", c.id)
    for d in (await db.execute(select(Device).where(Device.owner_role == "customer", Device.owner_id == c.id))).scalars().all():
        d.is_active = False
    return OK()
