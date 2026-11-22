from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.menu import MenuAddon, MenuCategory, MenuItem, MenuItemVariant


def item_dict(m: MenuItem, variants: list[MenuItemVariant], addons: list[MenuAddon], *, admin: bool = False) -> dict:
    d = {
        "id": m.id, "category_id": m.category_id, "name": m.name, "description": m.description, "price_paise": m.price_paise,
        "image_url": m.image_url, "prep_minutes": m.prep_minutes, "is_veg": m.is_veg, "is_available": m.is_available, "sort_order": m.sort_order,
        "variants": [{"id": v.id, "name": v.name, "price_paise": v.price_paise, "is_available": v.is_available, **({"is_archived": v.is_archived, "sort_order": v.sort_order} if admin else {})}
                     for v in sorted(variants, key=lambda x: (x.sort_order, x.name)) if admin or not v.is_archived],
        "addons": [{"id": a.id, "name": a.name, "price_paise": a.price_paise, "max_quantity": a.max_quantity, "is_available": a.is_available,
                    **({"is_archived": a.is_archived, "sort_order": a.sort_order} if admin else {})}
                   for a in sorted(addons, key=lambda x: (x.sort_order, x.name)) if admin or not a.is_archived],
    }
    if admin:
        d.update(prep_notes=m.prep_notes, tax_setting_id=m.tax_setting_id, is_archived=m.is_archived, image_path=m.image_path)
    return d


async def build_menu(db: AsyncSession, branch_id: uuid.UUID, *, admin: bool = False) -> list[dict]:
    cq = select(MenuCategory).where(MenuCategory.branch_id == branch_id)
    iq = select(MenuItem).where(MenuItem.branch_id == branch_id)
    if not admin:
        cq, iq = cq.where(MenuCategory.is_active.is_(True)), iq.where(MenuItem.is_archived.is_(False))
    cats = (await db.execute(cq.order_by(MenuCategory.sort_order, MenuCategory.name))).scalars().all()
    items = (await db.execute(iq.order_by(MenuItem.sort_order, MenuItem.name))).scalars().all()
    ids = [i.id for i in items]
    variants = (await db.execute(select(MenuItemVariant).where(MenuItemVariant.item_id.in_(ids)))).scalars().all() if ids else []
    addons = (await db.execute(select(MenuAddon).where(MenuAddon.item_id.in_(ids)))).scalars().all() if ids else []
    out = []
    for c in cats:
        its = [item_dict(i, [v for v in variants if v.item_id == i.id], [a for a in addons if a.item_id == i.id], admin=admin) for i in items if i.category_id == c.id]
        out.append({"id": c.id, "name": c.name, "description": c.description, "sort_order": c.sort_order, "is_active": c.is_active, "items": its})
    return out
