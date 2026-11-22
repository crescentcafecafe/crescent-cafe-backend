from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Created, Updated, UUIDPk


class MenuCategory(Base):
    __tablename__ = "menu_categories"
    id: Mapped[UUIDPk]
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[Created]
    updated_at: Mapped[Updated]


class MenuItem(Base):
    __tablename__ = "menu_items"
    id: Mapped[UUIDPk]
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), index=True)
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("menu_categories.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    price_paise: Mapped[int] = mapped_column(Integer)
    image_path: Mapped[str | None] = mapped_column(String(500))  # storage object path
    image_url: Mapped[str | None] = mapped_column(String(800))
    prep_minutes: Mapped[int | None] = mapped_column(Integer)
    prep_notes: Mapped[str | None] = mapped_column(Text)
    is_veg: Mapped[bool | None] = mapped_column(Boolean)
    tax_setting_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tax_settings.id"))  # null => default tax
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)  # manual Available / Unavailable switch
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[Created]
    updated_at: Mapped[Updated]
    __table_args__ = (CheckConstraint("price_paise >= 0", name="price_nonneg"),)


class MenuItemVariant(Base):
    """Absolute-priced variant (e.g. Small / Medium / Large). If an item has variants the customer must pick one."""

    __tablename__ = "menu_item_variants"
    id: Mapped[UUIDPk]
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("menu_items.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    price_paise: Mapped[int] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (CheckConstraint("price_paise >= 0", name="price_nonneg"),)


class MenuAddon(Base):
    __tablename__ = "menu_addons"
    id: Mapped[UUIDPk]
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("menu_items.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    price_paise: Mapped[int] = mapped_column(Integer, default=0)
    max_quantity: Mapped[int] = mapped_column(Integer, default=1)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (CheckConstraint("price_paise >= 0", name="price_nonneg"),)
