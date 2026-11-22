from __future__ import annotations

import uuid

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigPK, Created, UUIDPk


class NotificationEvent(Base):
    """Durable business event. Source of truth for push + realtime; clients can refetch by id after reconnecting."""

    __tablename__ = "notification_events"
    id: Mapped[int] = mapped_column(BigPK, primary_key=True, autoincrement=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id"))
    type: Mapped[str] = mapped_column(String(50), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    recipients: Mapped[list] = mapped_column(default=list)  # [{"role": "admin", "id": "..."|None}]
    title: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(String(500))
    data: Mapped[dict] = mapped_column(default=dict)
    high_priority: Mapped[bool] = mapped_column(Boolean, default=False)
    data_only: Mapped[bool] = mapped_column(Boolean, default=False)
    push: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[Created]
    __table_args__ = (Index("ix_events_created", "created_at"),)


class NotificationDeliveryLog(Base):
    __tablename__ = "notification_delivery_logs"
    id: Mapped[UUIDPk]
    event_id: Mapped[int] = mapped_column(ForeignKey("notification_events.id", ondelete="CASCADE"), index=True)
    fcm_token_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fcm_tokens.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16))  # sent | failed | invalid_token
    fcm_message_id: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[Created]


class NotificationReceipt(Base):
    """Per-person state of one notification in the in-app notification centre (read / cleared). Events themselves are shared."""

    __tablename__ = "notification_receipts"
    id: Mapped[int] = mapped_column(BigPK, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("notification_events.id", ondelete="CASCADE"), index=True)
    owner_role: Mapped[str] = mapped_column(String(12))
    owner_id: Mapped[uuid.UUID] = mapped_column()
    read_at: Mapped[datetime | None] = mapped_column()
    cleared_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (UniqueConstraint("event_id", "owner_role", "owner_id", name="uq_receipt_event_owner"),
                      Index("ix_receipt_owner", "owner_role", "owner_id"))
