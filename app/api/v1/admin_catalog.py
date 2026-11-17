"""Admin: menu (categories/items/variants/add-ons/images), dine-in tables + QR, offers & coupons."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Invalid, NotFound
from app.core.permissions import Principal, current_admin
from app.core.config import get_settings
from app.core.security import hash_token, now_utc
from app.db.session import get_db
from app.integrations import registry
from app.models.menu import MenuAddon, MenuCategory, MenuItem, MenuItemVariant
from app.models.offers import Coupon, CouponRedemption, Offer
from app.models.orders import Order
from app.models.settings import TaxSetting
from app.models.tables import DiningTable, TableQrToken, TableSession
from app.schemas.admin import (AddonIn, AddonPatch, AvailabilityIn, BulkQrIn, CategoryIn, CategoryPatch, CloseSessionIn, CouponIn, ItemIn, ItemPatch, OfferIn,
                               OfferPatch, ReorderIn, RevokeIn, TableIn, TablePatch, TransferIn, VariantIn, VariantPatch)
from app.schemas.common import OK
from app.services import events as ev, orders as order_svc, tables as table_svc
from app.services.audit import audit
from app.services.menu import build_menu, item_dict

router = APIRouter(prefix="/admin", tags=["admin-catalog"], dependencies=[Depends(current_admin)])


# ================================================================= menu
@router.get("/menu")
async def full_menu(a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Everything including inactive categories and archived items (admin view)."""
    return {"categories": await build_menu(db, a.user.branch_id, admin=True)}


@router.post("/menu/categories", status_code=201)
async def create_category(body: CategoryIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    c = MenuCategory(branch_id=a.user.branch_id, **body.model_dump())
    db.add(c)
    await db.flush()
    await audit(db, actor_role="admin", actor_id=a.id, action="menu.category_created", entity_type="category", entity_id=c.id)
    return {"id": c.id, **body.model_dump()}


@router.patch("/menu/categories/{category_id}")
async def patch_category(category_id: uuid.UUID, body: CategoryPatch, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    c = await db.get(MenuCategory, category_id)
    if c is None or c.branch_id != a.user.branch_id:
        raise NotFound("Category not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    return {"id": c.id, "name": c.name, "description": c.description, "sort_order": c.sort_order, "is_active": c.is_active}


@router.post("/menu/categories/reorder", response_model=OK)
async def reorder_categories(body: ReorderIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    for pos, cid in enumerate(body.ids):
        await db.execute(update(MenuCategory).where(MenuCategory.id == cid, MenuCategory.branch_id == a.user.branch_id).values(sort_order=pos))
    return OK()


@router.post("/menu/items/reorder", response_model=OK)
async def reorder_items(body: ReorderIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    for pos, iid in enumerate(body.ids):
        await db.execute(update(MenuItem).where(MenuItem.id == iid, MenuItem.branch_id == a.user.branch_id).values(sort_order=pos))
    return OK()


async def _item(db: AsyncSession, a: Principal, item_id: uuid.UUID) -> MenuItem:
    m = await db.get(MenuItem, item_id)
    if m is None or m.branch_id != a.user.branch_id:
        raise NotFound("Item not found")
    return m


async def _item_full(db: AsyncSession, m: MenuItem) -> dict:
    vs = (await db.execute(select(MenuItemVariant).where(MenuItemVariant.item_id == m.id))).scalars().all()
    ads = (await db.execute(select(MenuAddon).where(MenuAddon.item_id == m.id))).scalars().all()
    return item_dict(m, list(vs), list(ads), admin=True)


async def _check_refs(db: AsyncSession, a: Principal, category_id: uuid.UUID | None, tax_id: uuid.UUID | None) -> None:
    if category_id:
        c = await db.get(MenuCategory, category_id)
        if c is None or c.branch_id != a.user.branch_id:
            raise Invalid("Unknown category", code="invalid_category")
    if tax_id:
        t = await db.get(TaxSetting, tax_id)
        if t is None or t.branch_id != a.user.branch_id:
            raise Invalid("Unknown tax setting", code="invalid_tax")


@router.post("/menu/items", status_code=201)
async def create_item(body: ItemIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Add a new product. Upload its photo afterwards with POST /admin/menu/items/{id}/image."""
    await _check_refs(db, a, body.category_id, body.tax_setting_id)
    data = body.model_dump(exclude={"variants", "addons"})
    m = MenuItem(branch_id=a.user.branch_id, **data)
    db.add(m)
    await db.flush()
    for v in body.variants:
        db.add(MenuItemVariant(item_id=m.id, **v.model_dump()))
    for ad in body.addons:
        db.add(MenuAddon(item_id=m.id, **ad.model_dump()))
    await db.flush()
    await audit(db, actor_role="admin", actor_id=a.id, action="menu.item_created", entity_type="item", entity_id=m.id, after={"name": m.name, "price_paise": m.price_paise})
    return await _item_full(db, m)


@router.get("/menu/items/{item_id}")
async def get_item(item_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return await _item_full(db, await _item(db, a, item_id))


@router.patch("/menu/items/{item_id}")
async def patch_item(item_id: uuid.UUID, body: ItemPatch, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    m = await _item(db, a, item_id)
    data = body.model_dump(exclude_unset=True)
    await _check_refs(db, a, data.get("category_id"), data.get("tax_setting_id"))
    before = {k: getattr(m, k) for k in data}
    for k, v in data.items():
        setattr(m, k, v)
    await audit(db, actor_role="admin", actor_id=a.id, action="menu.item_updated", entity_type="item", entity_id=m.id,
                before={k: str(v) for k, v in before.items()}, after={k: str(v) for k, v in data.items()})
    return await _item_full(db, m)


@router.put("/menu/items/{item_id}/availability")
async def set_availability(item_id: uuid.UUID, body: AvailabilityIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Instant Available / Unavailable switch (no ingredient stock tracking). Orders re-check this at submission time."""
    m = await _item(db, a, item_id)
    m.is_available = body.is_available
    await ev.emit(db, type="menu.availability_changed", topics=["menu"], dedupe_key=f"menu.avail:{m.id}:{now_utc().timestamp()}", push=False,
                  data={"item_id": m.id, "is_available": m.is_available}, branch_id=m.branch_id)
    return {"id": m.id, "is_available": m.is_available}


@router.delete("/menu/items/{item_id}", response_model=OK)
async def archive_item(item_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Soft delete: old orders keep their item snapshot."""
    m = await _item(db, a, item_id)
    m.is_archived, m.is_available = True, False
    await audit(db, actor_role="admin", actor_id=a.id, action="menu.item_archived", entity_type="item", entity_id=m.id)
    return OK()


@router.post("/menu/items/{item_id}/restore", response_model=OK)
async def restore_item(item_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    m = await _item(db, a, item_id)
    m.is_archived = False
    return OK()


_MAGIC = [(b"\xff\xd8\xff", "image/jpeg", "jpg"), (b"\x89PNG\r\n\x1a\n", "image/png", "png")]


def sniff_image(data: bytes) -> tuple[str, str]:
    for magic, ctype, ext in _MAGIC:
        if data.startswith(magic):
            return ctype, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise Invalid("Only JPEG, PNG or WebP images are allowed", code="invalid_image")


@router.post("/menu/items/{item_id}/image")
async def upload_image(item_id: uuid.UUID, file: UploadFile = File(...), a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Upload/replace the product photo (stored in Supabase Storage). Max 5 MB; the content is verified, not just the file extension."""
    m = await _item(db, a, item_id)
    s = get_settings()
    data = await file.read(s.max_image_bytes + 1)
    if len(data) > s.max_image_bytes:
        raise Invalid(f"Image is larger than {s.max_image_bytes // (1024 * 1024)} MB", code="image_too_large")
    if not data:
        raise Invalid("Empty file", code="invalid_image")
    ctype, ext = sniff_image(data)
    storage = registry.get_storage()
    path = f"{m.branch_id}/{m.id}/{uuid.uuid4().hex}.{ext}"
    url = await storage.upload(path, data, ctype)
    old = m.image_path
    m.image_path, m.image_url = path, url
    if old:
        try:
            await storage.delete(old)
        except Exception:  # noqa: BLE001  (orphan cleanup is best effort)
            pass
    await audit(db, actor_role="admin", actor_id=a.id, action="menu.item_image", entity_type="item", entity_id=m.id)
    return {"id": m.id, "image_url": url}


@router.delete("/menu/items/{item_id}/image", response_model=OK)
async def delete_image(item_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    m = await _item(db, a, item_id)
    if m.image_path:
        try:
            await registry.get_storage().delete(m.image_path)
        except Exception:  # noqa: BLE001
            pass
    m.image_path = m.image_url = None
    return OK()


@router.post("/menu/items/{item_id}/variants", status_code=201)
async def add_variant(item_id: uuid.UUID, body: VariantIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    m = await _item(db, a, item_id)
    v = MenuItemVariant(item_id=m.id, **body.model_dump())
    db.add(v)
    await db.flush()
    return {"id": v.id, **body.model_dump()}


@router.patch("/menu/variants/{variant_id}")
async def patch_variant(variant_id: uuid.UUID, body: VariantPatch, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    v = await db.get(MenuItemVariant, variant_id)
    if v is None:
        raise NotFound("Variant not found")
    await _item(db, a, v.item_id)
    for k, val in body.model_dump(exclude_unset=True).items():
        setattr(v, k, val)
    return {"id": v.id, "name": v.name, "price_paise": v.price_paise, "is_available": v.is_available, "is_archived": v.is_archived}


@router.post("/menu/items/{item_id}/addons", status_code=201)
async def add_addon(item_id: uuid.UUID, body: AddonIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    m = await _item(db, a, item_id)
    ad = MenuAddon(item_id=m.id, **body.model_dump())
    db.add(ad)
    await db.flush()
    return {"id": ad.id, **body.model_dump()}


@router.patch("/menu/addons/{addon_id}")
async def patch_addon(addon_id: uuid.UUID, body: AddonPatch, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    ad = await db.get(MenuAddon, addon_id)
    if ad is None:
        raise NotFound("Add-on not found")
    await _item(db, a, ad.item_id)
    for k, val in body.model_dump(exclude_unset=True).items():
        setattr(ad, k, val)
    return {"id": ad.id, "name": ad.name, "price_paise": ad.price_paise, "is_available": ad.is_available, "is_archived": ad.is_archived}


# ================================================================= tables + QR
def _table(t: DiningTable) -> dict:
    return {"id": t.id, "number": t.number, "label": t.label, "capacity": t.capacity, "area": t.area, "notes": t.notes, "has_pin": t.pin_hash is not None,
            "is_active": t.is_active, "is_archived": t.is_archived}


async def _tbl(db: AsyncSession, a: Principal, table_id: uuid.UUID) -> DiningTable:
    t = await db.get(DiningTable, table_id)
    if t is None or t.branch_id != a.user.branch_id:
        raise NotFound("Table not found")
    return t


@router.get("/tables")
async def list_tables(include_archived: bool = False, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    q = select(DiningTable).where(DiningTable.branch_id == a.user.branch_id)
    if not include_archived:
        q = q.where(DiningTable.is_archived.is_(False))
    tables = (await db.execute(q.order_by(DiningTable.area, DiningTable.number))).scalars().all()
    open_sessions = {s.table_id: s.id for s in (await db.execute(select(TableSession).where(TableSession.status == "open"))).scalars().all()}
    qr_ok = {t for (t,) in (await db.execute(select(TableQrToken.table_id).where(TableQrToken.status == "active"))).all()}
    return [{**_table(t), "open_session_id": open_sessions.get(t.id), "has_active_qr": t.id in qr_ok} for t in tables]


@router.post("/tables", status_code=201)
async def create_table(body: TableIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """No maximum table count. Number must be unique among non-archived tables."""
    t = DiningTable(branch_id=a.user.branch_id, number=body.number.strip(), label=body.label, capacity=body.capacity, area=body.area, notes=body.notes,
                    is_active=body.is_active, pin_hash=hash_token(body.pin) if body.pin else None)
    try:
        async with db.begin_nested():
            db.add(t)
            await db.flush()
    except IntegrityError:
        raise Conflict("A table with this number already exists", code="table_exists")
    await audit(db, actor_role="admin", actor_id=a.id, action="table.created", entity_type="table", entity_id=t.id)
    return _table(t)


@router.patch("/tables/{table_id}")
async def patch_table(table_id: uuid.UUID, body: TablePatch, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    t = await _tbl(db, a, table_id)
    data = body.model_dump(exclude_unset=True)
    if "pin" in data:
        pin = data.pop("pin")
        t.pin_hash = hash_token(pin) if pin else None
    if data.get("is_archived") or data.get("is_active") is False:
        busy = (await db.execute(select(TableSession).where(TableSession.table_id == t.id, TableSession.status == "open"))).scalar_one_or_none()
        if busy:
            raise Conflict("Close the table's open session first", code="session_open")
    try:
        async with db.begin_nested():
            for k, v in data.items():
                setattr(t, k, v)
            await db.flush()
    except IntegrityError:
        raise Conflict("A table with this number already exists", code="table_exists")
    if data.get("is_archived") or data.get("is_active") is False:
        await table_svc.revoke_qr(db, t, a.id, "table disabled/archived")
    await audit(db, actor_role="admin", actor_id=a.id, action="table.updated", entity_type="table", entity_id=t.id, after={k: str(v) for k, v in data.items()})
    return _table(t)


@router.post("/tables/{table_id}/qr")
async def generate_qr(table_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Generate (or regenerate) the table's QR token. The previous token is revoked immediately and cannot open new sessions.
    The plaintext token/URL is shown ONLY in this response (only its hash is stored): render it to PNG/PDF on the client."""
    return await table_svc.generate_qr(db, await _tbl(db, a, table_id), a.id, "regenerated")


@router.post("/tables/{table_id}/qr/revoke")
async def revoke_qr(table_id: uuid.UUID, body: RevokeIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    n = await table_svc.revoke_qr(db, await _tbl(db, a, table_id), a.id, body.reason)
    return {"revoked": n}


@router.post("/tables/qr/bulk")
async def bulk_qr(body: BulkQrIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Bulk export data for printable ZIP/PDF generation on the frontend. Rotates the selected tables' tokens (hashes cannot be reversed),
    so confirm_rotate=true is required."""
    if not body.confirm_rotate:
        raise Invalid("Bulk export issues NEW tokens and invalidates the old QR codes. Send confirm_rotate=true to proceed.", code="confirm_required")
    q = select(DiningTable).where(DiningTable.branch_id == a.user.branch_id, DiningTable.is_archived.is_(False), DiningTable.is_active.is_(True))
    if body.table_ids:
        q = q.where(DiningTable.id.in_(body.table_ids))
    tables = (await db.execute(q.order_by(DiningTable.area, DiningTable.number))).scalars().all()
    return {"count": len(tables), "items": [await table_svc.generate_qr(db, t, a.id, "bulk export") for t in tables]}


@router.get("/table-sessions")
async def list_sessions(status: str = "open", table_id: uuid.UUID | None = None, limit: int = 50, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    q = select(TableSession).where(TableSession.branch_id == a.user.branch_id, TableSession.status == status)
    if table_id:
        q = q.where(TableSession.table_id == table_id)
    rows = (await db.execute(q.order_by(TableSession.opened_at.desc()).limit(max(1, min(limit, 200))))).scalars().all()
    out = []
    for s in rows:
        t = await db.get(DiningTable, s.table_id)
        n = (await db.execute(select(func.count()).select_from(Order).where(Order.table_session_id == s.id))).scalar_one()
        out.append({"id": s.id, "table": _table(t), "status": s.status, "opened_at": s.opened_at, "closed_at": s.closed_at, "order_count": n})
    return out


async def _sess(db: AsyncSession, a: Principal, sid: uuid.UUID) -> TableSession:
    s = await db.get(TableSession, sid)
    if s is None or s.branch_id != a.user.branch_id:
        raise NotFound("Session not found")
    return s


@router.get("/table-sessions/{session_id}/orders")
async def session_orders(session_id: uuid.UUID, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Every order of the session shown separately (each customer's order is billed on its own)."""
    s = await _sess(db, a, session_id)
    rows = (await db.execute(select(Order).where(Order.table_session_id == s.id).order_by(Order.created_at))).scalars().all()
    return [await order_svc.order_view(db, o, "admin") for o in rows]


@router.post("/table-sessions/{session_id}/close")
async def close_session(session_id: uuid.UUID, body: CloseSessionIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    s = await table_svc.close_session(db, await _sess(db, a, session_id), a.id, body.reason)
    return {"id": s.id, "status": s.status, "closed_at": s.closed_at}


@router.post("/table-sessions/{session_id}/transfer")
async def transfer_session(session_id: uuid.UUID, body: TransferIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    s = await table_svc.transfer_session(db, await _sess(db, a, session_id), await _tbl(db, a, body.new_table_id), a.id)
    return {"id": s.id, "table_id": s.table_id}


# ================================================================= offers & coupons
def _offer(o: Offer, codes: list[Coupon] | None = None) -> dict:
    d = {c: getattr(o, c) for c in ("id", "branch_id", "name", "description", "discount_type", "value", "min_order_paise", "max_discount_paise", "starts_at", "ends_at",
                                    "total_redemption_limit", "per_customer_limit", "item_ids", "category_ids", "order_types", "first_order_only", "stackable",
                                    "is_listed", "status", "created_at")}
    if codes is not None:
        d["coupons"] = [{"id": c.id, "code": c.code, "is_active": c.is_active} for c in codes]
    return d


def _check_offer_fields(discount_type: str | None, value: int | None, starts, ends) -> None:
    if discount_type == "percent" and value is not None and not (1 <= value <= 10000):
        raise Invalid("Percent discounts are in basis points between 1 and 10000", code="invalid_value")
    if starts and ends and ends <= starts:
        raise Invalid("End date must be after the start date", code="invalid_dates")


@router.get("/offers")
async def list_offers(status: str | None = None, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    q = select(Offer)
    if status:
        q = q.where(Offer.status == status)
    rows = (await db.execute(q.order_by(Offer.created_at.desc()))).scalars().all()
    out = []
    for o in rows:
        cs = (await db.execute(select(Coupon).where(Coupon.offer_id == o.id))).scalars().all()
        used = (await db.execute(select(func.count()).select_from(CouponRedemption).where(CouponRedemption.offer_id == o.id, CouponRedemption.status == "consumed"))).scalar_one()
        out.append({**_offer(o, list(cs)), "redemptions_consumed": used})
    return out


@router.post("/offers", status_code=201)
async def create_offer(body: OfferIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    _check_offer_fields(body.discount_type, body.value, body.starts_at, body.ends_at)
    data = body.model_dump(exclude={"coupon_code", "branch_specific"})
    data["item_ids"], data["category_ids"] = [str(i) for i in body.item_ids], [str(i) for i in body.category_ids]
    o = Offer(branch_id=a.user.branch_id if body.branch_specific else None, created_by=a.id, **data)
    db.add(o)
    await db.flush()
    if body.coupon_code:
        if (await db.execute(select(Coupon).where(Coupon.code == body.coupon_code))).scalar_one_or_none():
            raise Conflict("This coupon code already exists", code="coupon_exists")
        db.add(Coupon(offer_id=o.id, code=body.coupon_code))
        await db.flush()
    await audit(db, actor_role="admin", actor_id=a.id, action="offer.created", entity_type="offer", entity_id=o.id, after={"name": o.name, "code": body.coupon_code})
    cs = (await db.execute(select(Coupon).where(Coupon.offer_id == o.id))).scalars().all()
    return _offer(o, list(cs))


@router.patch("/offers/{offer_id}")
async def patch_offer(offer_id: uuid.UUID, body: OfferPatch, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Activate / pause / expire an offer (history is never deleted) or edit its rules."""
    o = await db.get(Offer, offer_id)
    if o is None:
        raise NotFound("Offer not found")
    data = body.model_dump(exclude_unset=True)
    _check_offer_fields(None, None, data.get("starts_at", o.starts_at), data.get("ends_at", o.ends_at))
    if o.discount_type == "percent" and "value" in data and not (1 <= data["value"] <= 10000):
        raise Invalid("Percent discounts are in basis points between 1 and 10000", code="invalid_value")
    before = {k: str(getattr(o, k)) for k in data}
    for k in ("item_ids", "category_ids"):
        if k in data and data[k] is not None:
            data[k] = [str(i) for i in data[k]]
    for k, v in data.items():
        setattr(o, k, v)
    await audit(db, actor_role="admin", actor_id=a.id, action="offer.updated", entity_type="offer", entity_id=o.id, before=before, after={k: str(v) for k, v in data.items()})
    cs = (await db.execute(select(Coupon).where(Coupon.offer_id == o.id))).scalars().all()
    return _offer(o, list(cs))


@router.post("/offers/{offer_id}/coupons", status_code=201)
async def add_coupon(offer_id: uuid.UUID, body: CouponIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    o = await db.get(Offer, offer_id)
    if o is None:
        raise NotFound("Offer not found")
    try:
        async with db.begin_nested():
            c = Coupon(offer_id=o.id, code=body.code)
            db.add(c)
            await db.flush()
    except IntegrityError:
        raise Conflict("This coupon code already exists", code="coupon_exists")
    await audit(db, actor_role="admin", actor_id=a.id, action="coupon.created", entity_type="coupon", entity_id=c.id, after={"code": c.code})
    return {"id": c.id, "code": c.code, "is_active": c.is_active}


@router.patch("/coupons/{coupon_id}/active")
async def toggle_coupon(coupon_id: uuid.UUID, body: AvailabilityIn, a: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    c = await db.get(Coupon, coupon_id)
    if c is None:
        raise NotFound("Coupon not found")
    c.is_active = body.is_available
    await audit(db, actor_role="admin", actor_id=a.id, action="coupon.toggled", entity_type="coupon", entity_id=c.id, after={"is_active": c.is_active})
    return {"id": c.id, "code": c.code, "is_active": c.is_active}


@router.get("/offers/{offer_id}/redemptions")
async def redemptions(offer_id: uuid.UUID, limit: int = 100, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(CouponRedemption).where(CouponRedemption.offer_id == offer_id).order_by(CouponRedemption.created_at.desc()).limit(max(1, min(limit, 500))))).scalars().all()
    return [{"id": r.id, "order_id": r.order_id, "customer_id": r.customer_id, "redeemer_key": r.redeemer_key, "discount_paise": r.discount_paise, "status": r.status,
             "created_at": r.created_at, "consumed_at": r.consumed_at, "released_at": r.released_at} for r in rows]
