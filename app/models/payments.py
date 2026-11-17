from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, Uuid, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigPK, Created, Updated, UUIDPk


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[UUIDPk]
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"))
    method: Mapped[str] = mapped_column(String(10))  # online | cash
    gateway: Mapped[str | None] = mapped_column(String(12))  # razorpay | cashfree | None
    gateway_order_id: Mapped[str | None] = mapped_column(String(80))
    gateway_payment_id: Mapped[str | None] = mapped_column(String(80))
    gateway_session_id: Mapped[str | None] = mapped_column(String(600))  # Cashfree payment_session_id (client needs it)
    amount_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[str] = mapped_column(String(20), default="created")
    # created | pending | paid | failed | expired | partially_refunded | refunded | cancelled
    verification_status: Mapped[str] = mapped_column(String(12), default="unverified")  # unverified|verified|mismatch
    failure_reason: Mapped[str | None] = mapped_column(String(300))
    refunded_paise: Mapped[int] = mapped_column(Integer, default=0)
    # cash
    confirmed_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    tendered_paise: Mapped[int | None] = mapped_column(Integer)
    change_paise: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None]
    paid_at: Mapped[datetime | None]
    verified_at: Mapped[datetime | None]
    last_gateway_payload: Mapped[dict | None]
    created_at: Mapped[Created]
    updated_at: Mapped[Updated]
    __table_args__ = (
        UniqueConstraint("gateway", "gateway_order_id", name="uq_payment_gateway_order"),
        UniqueConstraint("gateway", "gateway_payment_id", name="uq_payment_gateway_payment"),
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        Index("ix_payments_status_method", "status", "method"),
    )


class PaymentWebhookEvent(Base):
    __tablename__ = "payment_webhook_events"
    id: Mapped[UUIDPk]
    gateway: Mapped[str] = mapped_column(String(12))
    event_id: Mapped[str] = mapped_column(String(120))
    event_type: Mapped[str | None] = mapped_column(String(80))
    signature_valid: Mapped[bool]
    status: Mapped[str] = mapped_column(String(12), default="received")  # received | processed | ignored | failed
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None]
    payment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    processed_at: Mapped[datetime | None]
    created_at: Mapped[Created]
    __table_args__ = (UniqueConstraint("gateway", "event_id", name="uq_webhook_event"),)


class Refund(Base):
    __tablename__ = "refunds"
    id: Mapped[UUIDPk]
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    amount_paise: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | succeeded | failed
    method: Mapped[str] = mapped_column(String(10))  # online | cash
    gateway_refund_id: Mapped[str | None] = mapped_column(String(80))
    failure_reason: Mapped[str | None] = mapped_column(String(300))
    requested_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    processed_at: Mapped[datetime | None]
    created_at: Mapped[Created]
    updated_at: Mapped[Updated]
    __table_args__ = (CheckConstraint("amount_paise > 0", name="amount_positive"),)
