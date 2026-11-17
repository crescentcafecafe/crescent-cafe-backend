from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, Uuid, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigPK, Created, Updated, UUIDPk


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[UUIDPk]
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"))
    order_number: Mapped[str] = mapped_column(String(32), unique=True)
    order_type: Mapped[str] = mapped_column(String(12))  # dine_in | pickup | delivery | walk_in
    source: Mapped[str] = mapped_column(String(16))  # customer_app | qr_web | admin
    status: Mapped[str] = mapped_column(String(20))
    payment_method: Mapped[str] = mapped_column(String(12))  # cash | razorpay | cashfree
    payment_status: Mapped[str] = mapped_column(String(20), default="unpaid")

    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    guest_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    customer_name: Mapped[str | None] = mapped_column(String(120))
    customer_phone: Mapped[str | None] = mapped_column(String(20))
    customer_email: Mapped[str | None] = mapped_column(String(255))

    table_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tables.id"))
    table_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("table_sessions.id"))
    table_label: Mapped[str | None] = mapped_column(String(160))  # snapshot, survives table edits

    delivery_address: Mapped[dict | None]
    delivery_postal_code: Mapped[str | None] = mapped_column(String(12))
    special_instructions: Mapped[str | None] = mapped_column(Text)

    currency: Mapped[str] = mapped_column(String(3), default="INR")
    prices_include_tax: Mapped[bool] = mapped_column(Boolean, default=False)
    subtotal_paise: Mapped[int] = mapped_column(Integer)
    discount_paise: Mapped[int] = mapped_column(Integer, default=0)
    tax_paise: Mapped[int] = mapped_column(Integer, default=0)
    delivery_charge_paise: Mapped[int] = mapped_column(Integer, default=0)
    total_paise: Mapped[int] = mapped_column(Integer)
    tax_breakdown: Mapped[list | None]
    discount_breakdown: Mapped[list | None]
    coupon_code: Mapped[str | None] = mapped_column(String(40))

    created_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    idem_scope: Mapped[str | None] = mapped_column(String(80))
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    rejection_reason: Mapped[str | None] = mapped_column(String(300))
    cancellation_reason: Mapped[str | None] = mapped_column(String(300))
    admin_alarm_acked_at: Mapped[datetime | None]
    decided_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    created_at: Mapped[Created]
    updated_at: Mapped[Updated]

    __table_args__ = (
        UniqueConstraint("idem_scope", "idempotency_key", name="uq_order_idempotency"),
        Index("ix_orders_branch_status_created", "branch_id", "status", "created_at"),
        Index("ix_orders_customer_created", "customer_id", "created_at"),
        Index("ix_orders_session", "table_session_id"),
        Index("ix_orders_phone", "customer_phone"),
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[UUIDPk]
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    menu_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("menu_items.id"))
    category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(String(160))
    variant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    variant_name: Mapped[str | None] = mapped_column(String(80))
    unit_price_paise: Mapped[int] = mapped_column(Integer)  # variant/base price + add-ons, per unit
    quantity: Mapped[int] = mapped_column(Integer)
    line_subtotal_paise: Mapped[int] = mapped_column(Integer)
    discount_paise: Mapped[int] = mapped_column(Integer, default=0)
    tax_name: Mapped[str | None] = mapped_column(String(80))
    tax_rate_bps: Mapped[int] = mapped_column(Integer, default=0)
    tax_paise: Mapped[int] = mapped_column(Integer, default=0)
    line_total_paise: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(String(300))


class OrderItemCustomization(Base):
    __tablename__ = "order_item_customizations"
    id: Mapped[UUIDPk]
    order_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("order_items.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(12))  # addon | option
    ref_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(String(120))
    value: Mapped[str | None] = mapped_column(String(200))
    price_paise: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[int] = mapped_column(Integer, default=1)


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"
    id: Mapped[int] = mapped_column(BigPK, primary_key=True, autoincrement=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    actor_role: Mapped[str] = mapped_column(String(12))  # admin | customer | rider | system | gateway
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    reason: Mapped[str | None] = mapped_column(String(300))
    meta: Mapped[dict | None]
    created_at: Mapped[Created]
