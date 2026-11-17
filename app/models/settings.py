from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Created, Updated, UUIDPk


class CafeSettings(Base):
    __tablename__ = "cafe_settings"
    id: Mapped[UUIDPk]
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), unique=True)
    name: Mapped[str] = mapped_column(String(160), default="Crescent Cafe")
    phone: Mapped[str | None] = mapped_column(String(30))
    email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    gstin: Mapped[str | None] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    timezone: Mapped[str] = mapped_column(String(40), default="Asia/Kolkata")
    # ordering switches
    accepting_orders: Mapped[bool] = mapped_column(Boolean, default=True)
    temporarily_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    temporary_closure_message: Mapped[str | None] = mapped_column(String(300))
    dine_in_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pickup_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    delivery_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    guest_checkout_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # opening hours: {"mon": [{"open": "09:00", "close": "22:00"}], ...}; closures: [{"date": "2026-12-25", "reason": "..."}]
    opening_hours: Mapped[dict] = mapped_column(default=dict)
    closures: Mapped[list] = mapped_column(default=list)
    enforce_opening_hours: Mapped[bool] = mapped_column(Boolean, default=True)
    # tax mode
    prices_include_tax: Mapped[bool] = mapped_column(Boolean, default=False)
    # payment gateways (credentials stay in env vars; these are only on/off switches)
    razorpay_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cashfree_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cash_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cash_on_delivery_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # delivery / rider rules
    rider_single_active_assignment: Mapped[bool] = mapped_column(Boolean, default=True)
    otp_ttl_minutes: Mapped[int] = mapped_column(Integer, default=240)
    otp_max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    location_min_interval_seconds: Mapped[int] = mapped_column(Integer, default=5)
    location_update_interval_seconds: Mapped[int] = mapped_column(Integer, default=10)  # advertised to rider app
    location_retention_days: Mapped[int] = mapped_column(Integer, default=7)
    location_stale_after_seconds: Mapped[int] = mapped_column(Integer, default=90)
    coupon_reservation_minutes: Mapped[int] = mapped_column(Integer, default=20)
    # notification switches
    notify_admin_new_orders: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_customers_promotions: Mapped[bool] = mapped_column(Boolean, default=True)
    # invoice numbering
    invoice_prefix: Mapped[str] = mapped_column(String(16), default="CC")
    invoice_next_number: Mapped[int] = mapped_column(Integer, default=1)
    invoice_footer: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[Updated]


class TaxSetting(Base):
    __tablename__ = "tax_settings"
    id: Mapped[UUIDPk]
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    rate_bps: Mapped[int] = mapped_column(Integer)  # 500 = 5.00 %
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[Created]
    updated_at: Mapped[Updated]
    __table_args__ = (CheckConstraint("rate_bps >= 0 AND rate_bps <= 10000", name="rate_range"),)


class DeliverySettings(Base):
    __tablename__ = "delivery_settings"
    id: Mapped[UUIDPk]
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), unique=True)
    base_charge_paise: Mapped[int] = mapped_column(Integer, default=0)
    free_delivery_above_paise: Mapped[int | None] = mapped_column(Integer)
    min_order_paise: Mapped[int] = mapped_column(Integer, default=0)
    allowed_postal_codes: Mapped[list] = mapped_column(default=list)  # empty => no restriction
    tax_rate_bps: Mapped[int] = mapped_column(Integer, default=0)  # tax charged on the delivery fee
    updated_at: Mapped[Updated]
