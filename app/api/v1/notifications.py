from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.core.permissions import Principal, current_principal
from app.core.security import now_utc
from app.db.session import get_db
from app.models.core import Device, FcmToken
from app.models.notifications import NotificationEvent
from app.schemas.admin import DeviceIn, DeviceTokenRefresh
from app.schemas.common import OK
from app.services import inbox

router = APIRouter(prefix="/notifications", tags=["notifications"])


async def unlink_device(db: AsyncSession, p: Principal, install_id: str) -> None:
    d = (await db.execute(select(Device).where(Device.owner_role == p.role, Device.owner_id == p.id, Device.install_id == install_id))).scalar_one_or_none()
    if d:
        await db.execute(delete(FcmToken).where(FcmToken.device_id == d.id))
        d.is_active = False


async def _attach_token(db: AsyncSession, device: Device, token: str) -> None:
    existing = (await db.execute(select(FcmToken).where(FcmToken.token == token))).scalar_one_or_none()
    if existing:  # same phone, different account / reinstall: the token moves to this device
        existing.device_id, existing.is_valid, existing.last_error = device.id, True, None
    else:
        db.add(FcmToken(device_id=device.id, token=token))
    await db.flush()
    # an install has one live token at a time; older ones are stale after a refresh
    await db.execute(delete(FcmToken).where(FcmToken.device_id == device.id, FcmToken.token != token))


@router.post("/devices", status_code=201)
async def register_device(body: DeviceIn, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    """Register / update this install's FCM token for the signed-in account (multiple devices per account supported)."""
    d = (await db.execute(select(Device).where(Device.owner_role == p.role, Device.owner_id == p.id, Device.install_id == body.install_id))).scalar_one_or_none()
    if d is None:
        d = Device(owner_role=p.role, owner_id=p.id, install_id=body.install_id)
        db.add(d)
    d.platform, d.device_model, d.os_version, d.app_version, d.locale = body.platform, body.device_model, body.os_version, body.app_version, body.locale
    d.notifications_enabled, d.is_active, d.last_seen_at = body.notifications_enabled, True, now_utc()
    await db.flush()
    await _attach_token(db, d, body.fcm_token)
    return {"device_id": d.id, "install_id": d.install_id, "notifications_enabled": d.notifications_enabled}


@router.put("/devices/{install_id}/token")
async def refresh_token(install_id: str, body: DeviceTokenRefresh, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    """Call from FirebaseMessagingService.onNewToken and whenever the OS notification permission changes."""
    d = (await db.execute(select(Device).where(Device.owner_role == p.role, Device.owner_id == p.id, Device.install_id == install_id))).scalar_one_or_none()
    if d is None:
        raise NotFound("Device not registered")
    d.last_seen_at, d.is_active = now_utc(), True
    if body.notifications_enabled is not None:
        d.notifications_enabled = body.notifications_enabled
    await _attach_token(db, d, body.fcm_token)
    return OK()


@router.delete("/devices/{install_id}", response_model=OK)
async def remove_device(install_id: str, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    await unlink_device(db, p, install_id)
    return OK()


@router.get("/devices")
async def my_devices(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Device).where(Device.owner_role == p.role, Device.owner_id == p.id, Device.is_active.is_(True)))).scalars().all()
    return [{"install_id": d.install_id, "platform": d.platform, "device_model": d.device_model, "app_version": d.app_version,
             "notifications_enabled": d.notifications_enabled, "last_seen_at": d.last_seen_at} for d in rows]


@router.get("/events")
async def missed_events(after_id: int = 0, limit: int = 100, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    """Catch-up after a WebSocket reconnect: durable events addressed to me or my topics since after_id."""
    rows = (await db.execute(select(NotificationEvent).where(NotificationEvent.id > after_id).order_by(NotificationEvent.id).limit(500))).scalars().all()
    out = []
    for e in rows:
        mine = any(r.get("role") == p.role and (r.get("id") in (None, str(p.id))) for r in (e.recipients or []))
        if p.role == "admin" and e.type in ("order.new", "order.status_changed", "assignment.accepted", "assignment.rejected", "delivery.completed", "payment.succeeded"):
            mine = True
        if p.role == "customer" and (e.data or {}).get("order_id") is not None and e.type in ("order.status_changed", "order.created", "payment.succeeded", "payment.failed"):
            from app.models.orders import Order
            import uuid as _u

            o = await db.get(Order, _u.UUID(str(e.data["order_id"])))
            mine = mine or (o is not None and o.customer_id == p.id)
        if mine:
            out.append({"id": e.id, "type": e.type, "ts": e.created_at, "title": e.title, "body": e.body, "data": e.data})
        if len(out) >= max(1, min(limit, 200)):
            break
    return {"items": out}


# ------------------------------------------------------------------ notification centre
class InboxIds(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=500)


@router.get("/inbox")
async def inbox_list(category: str = "all", before_id: int | None = None, limit: int = 30, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    """My notifications, newest first. category: all | unread | orders | payments | delivery | messages | system."""
    return await inbox.list_items(db, p.role, p.id, category=category, before_id=before_id, limit=limit)


@router.get("/inbox/unread-count")
async def inbox_unread(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    return await inbox.unread_counts(db, p.role, p.id)


@router.post("/inbox/read")
async def inbox_read(body: InboxIds, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    return {"updated": await inbox.mark_read(db, p.role, p.id, body.ids)}


@router.post("/inbox/read-all")
async def inbox_read_all(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    return {"updated": await inbox.mark_all_read(db, p.role, p.id)}


@router.post("/inbox/clear")
async def inbox_clear(body: InboxIds, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    return {"updated": await inbox.clear(db, p.role, p.id, body.ids)}


@router.post("/inbox/clear-all")
async def inbox_clear_all(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    return {"updated": await inbox.clear_all(db, p.role, p.id)}
