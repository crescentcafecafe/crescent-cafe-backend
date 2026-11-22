from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.core.permissions import Principal, current_customer
from app.core.security import now_utc
from app.db.session import get_db
from app.models.accounts import CustomerAddress
from app.models.core import Customer, Device
from app.schemas.admin import CustomerAddressIn, CustomerAddressPatch
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


MAX_ADDRESSES = 10


def _addr(a: CustomerAddress) -> dict:
    return {"id": a.id, "label": a.label, "line1": a.line1, "line2": a.line2, "landmark": a.landmark, "city": a.city, "postal_code": a.postal_code,
            "latitude": a.latitude, "longitude": a.longitude, "location_source": a.location_source, "is_default": a.is_default}


async def _live_addresses(db: AsyncSession, customer_id: uuid.UUID) -> list[CustomerAddress]:
    return list((await db.execute(select(CustomerAddress).where(CustomerAddress.customer_id == customer_id, CustomerAddress.deleted_at.is_(None))
                                  .order_by(CustomerAddress.is_default.desc(), CustomerAddress.created_at.desc()))).scalars().all())


@router.get("/me/addresses")
async def list_addresses(p: Principal = Depends(current_customer), db: AsyncSession = Depends(get_db)):
    return [_addr(a) for a in await _live_addresses(db, p.id)]


@router.post("/me/addresses", status_code=201)
async def add_address(body: CustomerAddressIn, p: Principal = Depends(current_customer), db: AsyncSession = Depends(get_db)):
    """Saved once, reused at checkout. The order keeps its own copy of the address, so editing this later never changes past orders."""
    existing = await _live_addresses(db, p.id)
    if len(existing) >= MAX_ADDRESSES:
        raise Conflict(f"You can save up to {MAX_ADDRESSES} addresses", code="too_many_addresses")
    make_default = body.is_default or not existing
    if make_default:
        await db.execute(update(CustomerAddress).where(CustomerAddress.customer_id == p.id).values(is_default=False))
    a = CustomerAddress(customer_id=p.id, **{**body.model_dump(), "is_default": make_default})
    db.add(a)
    await db.flush()
    return _addr(a)


async def _own_address(db: AsyncSession, customer_id: uuid.UUID, address_id: uuid.UUID) -> CustomerAddress:
    a = await db.get(CustomerAddress, address_id)
    if a is None or a.customer_id != customer_id or a.deleted_at is not None:
        raise NotFound("Address not found")
    return a


@router.patch("/me/addresses/{address_id}")
async def update_address(address_id: uuid.UUID, body: CustomerAddressPatch, p: Principal = Depends(current_customer), db: AsyncSession = Depends(get_db)):
    a = await _own_address(db, p.id, address_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("is_default"):
        await db.execute(update(CustomerAddress).where(CustomerAddress.customer_id == p.id).values(is_default=False))
    for k, v in data.items():
        if k == "is_default" and v is None:
            continue
        setattr(a, k, v)
    return _addr(a)


@router.delete("/me/addresses/{address_id}", response_model=OK)
async def delete_address(address_id: uuid.UUID, p: Principal = Depends(current_customer), db: AsyncSession = Depends(get_db)):
    a = await _own_address(db, p.id, address_id)
    a.deleted_at, was_default = now_utc(), a.is_default
    a.is_default = False
    if was_default:
        rest = [x for x in await _live_addresses(db, p.id) if x.id != a.id]
        if rest:
            rest[0].is_default = True  # newest remaining address becomes the default
    return OK()


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
