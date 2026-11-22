"""Insert an optional Crescent Cafe demo menu for local visual/integration testing.

Run after `alembic upgrade head` and `python -m scripts.bootstrap_admin`.
This script is intentionally opt-in; it never runs automatically in production.
"""
from __future__ import annotations

import asyncio
import os

from sqlalchemy import select

from app.db.session import dispose_engine, session_scope
from app.models.core import Branch
from app.models.menu import MenuCategory, MenuItem

SAMPLE = [
    ("Burgers", [
        ("Classic Burger", "Grilled patty, cheddar, lettuce, tomato and house sauce.", 14900, False, "classic-burger.jpg"),
        ("Crescent Chicken Burger", "Crispy chicken, fresh lettuce and creamy house sauce.", 17900, False, "classic-burger.jpg"),
    ]),
    ("Pizza", [
        ("Margherita Pizza", "Tomato, mozzarella, basil and a crisp baked crust.", 24900, True, "margherita.jpg"),
        ("Farmhouse Pizza", "A colourful selection of vegetables and mozzarella.", 27900, True, "margherita.jpg"),
    ]),
    ("Snacks", [
        ("Crispy Chicken", "Golden, crunchy chicken bites served fresh.", 19900, False, "crispy-chicken.jpg"),
        ("Loaded Fries", "Hot fries with a savoury house seasoning.", 9900, True, "crispy-chicken.jpg"),
    ]),
    ("Drinks", [("Chilled Cola", "A cold fizzy drink.", 6900, True, "classic-burger.jpg")]),
    ("Desserts", [("Chocolate Pastry", "Soft chocolate cake with a rich cream layer.", 12900, True, "margherita.jpg")]),
]

async def seed() -> int:
    base = os.getenv("SAMPLE_MENU_ASSET_BASE_URL", "http://localhost:3000/assets/menu").rstrip("/")
    created = 0
    async with session_scope() as db:
        branch = (await db.execute(select(Branch).where(Branch.code == os.getenv("BRANCH_CODE", "main")))).scalar_one_or_none()
        if branch is None:
            raise RuntimeError("Branch not found. Run scripts.bootstrap_admin first.")
        for sort_cat, (category_name, entries) in enumerate(SAMPLE):
            category = (await db.execute(select(MenuCategory).where(MenuCategory.branch_id == branch.id, MenuCategory.name == category_name))).scalar_one_or_none()
            if category is None:
                category = MenuCategory(branch_id=branch.id, name=category_name, description=f"{category_name} at Crescent Cafe", sort_order=sort_cat, is_active=True)
                db.add(category)
                await db.flush()
            for sort_item, (name, description, price, is_veg, image_name) in enumerate(entries):
                existing = (await db.execute(select(MenuItem).where(MenuItem.branch_id == branch.id, MenuItem.name == name))).scalar_one_or_none()
                if existing is not None:
                    continue
                db.add(MenuItem(branch_id=branch.id, category_id=category.id, name=name, description=description, price_paise=price,
                                image_url=f"{base}/{image_name}", is_veg=is_veg, is_available=True, sort_order=sort_item, prep_minutes=15))
                created += 1
    return created

async def main() -> None:
    try:
        print(f"Inserted {await seed()} sample menu items.")
    finally:
        await dispose_engine()

if __name__ == "__main__":
    asyncio.run(main())
