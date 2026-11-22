from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, Uuid, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Created, Updated, UUIDPk


class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[UUIDPk]
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id"), index=True)  # null => all branches
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    discount_type: Mapped[str] = mapped_column(String(10))  # percent | fixed
    value: Mapped[int] = mapped_column(Integer)  # percent in basis points (1000 = 10%) or paise for fixed
    min_order_paise: Mapped[int] = mapped_column(Integer, default=0)
    max_discount_paise: Mapped[int | None] = mapped_column(Integer)
    starts_at: Mapped[datetime | None]
    ends_at: Mapped[datetime | None]
    total_redemption_limit: Mapped[int | None] = mapped_column(Integer)
    per_customer_limit: Mapped[int | None] = mapped_column(Integer)
    item_ids: Mapped[list] = mapped_column(default=list)  # empty + empty categories => whole cart
    category_ids: Mapped[list] = mapped_column(default=list)
    order_types: Mapped[list] = mapped_column(default=list)  # empty => all
    first_order_only: Mapped[bool] = mapped_column(Boolean, default=False)
    stackable: Mapped[bool] = mapped_column(Boolean, default=False)
    is_listed: Mapped[bool] = mapped_column(Boolean, default=True)  # show offer (and its code) in customer offer lists
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | paused | expired
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[Created]
    updated_at: Mapped[Updated]


class Coupon(Base):
    """A code that unlocks an Offer. Offers without any coupon are applied automatically when eligible."""

    __tablename__ = "coupons"
    id: Mapped[UUIDPk]
    offer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("offers.id"), index=True)
    code: Mapped[str] = mapped_column(String(40))  # stored upper-case
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[Created]
    __table_args__ = (UniqueConstraint("code", name="uq_coupon_code"),)


class CouponRedemption(Base):
    __tablename__ = "coupon_redemptions"
    id: Mapped[UUIDPk]
    offer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("offers.id"), index=True)
    coupon_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("coupons.id"))
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    redeemer_key: Mapped[str] = mapped_column(String(80))  # "c:<customer uuid>" or "p:<phone>"
    discount_paise: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), default="reserved")  # reserved | consumed | released
    reserved_until: Mapped[datetime | None]
    consumed_at: Mapped[datetime | None]
    released_at: Mapped[datetime | None]
    created_at: Mapped[Created]
    __table_args__ = (
        UniqueConstraint("offer_id", "order_id", name="uq_redemption_offer_order"),
        Index("ix_redemption_offer_status", "offer_id", "status"),
        Index("ix_redemption_redeemer", "offer_id", "redeemer_key"),
    )
