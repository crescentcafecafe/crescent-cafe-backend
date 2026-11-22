from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigPK, Created, UUIDPk

ACTIVE_ASSIGNMENT = ("offered", "accepted", "picked_up")


class RiderAssignment(Base):
    __tablename__ = "rider_assignments"
    id: Mapped[UUIDPk]
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    rider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("riders.id"), index=True)
    status: Mapped[str] = mapped_column(String(12), default="offered")
    # offered | accepted | rejected | cancelled | picked_up | completed
    assigned_by: Mapped[uuid.UUID | None] = mapped_column()
    reject_reason: Mapped[str | None] = mapped_column(String(300))
    offered_at: Mapped[Created]
    responded_at: Mapped[datetime | None]
    picked_up_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    arrival_notified_at: Mapped[datetime | None]  # the "rider is nearby" alert is sent once per assignment
    __table_args__ = (
        Index("uq_one_active_assignment_per_order", "order_id", unique=True,
              postgresql_where=text("status IN ('offered','accepted','picked_up')"),
              sqlite_where=text("status IN ('offered','accepted','picked_up')")),
        Index("ix_assignment_rider_status", "rider_id", "status"),
    )


class RiderLocation(Base):
    __tablename__ = "rider_locations"
    id: Mapped[int] = mapped_column(BigPK, primary_key=True, autoincrement=True)
    rider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("riders.id"))
    assignment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rider_assignments.id"))
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    accuracy_m: Mapped[float | None] = mapped_column(Float)
    recorded_at: Mapped[datetime]
    received_at: Mapped[Created]
    __table_args__ = (Index("ix_rider_loc_order_time", "order_id", "recorded_at"), Index("ix_rider_loc_received", "received_at"))


class DeliveryOtp(Base):
    __tablename__ = "delivery_otps"
    id: Mapped[UUIDPk]
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("rider_assignments.id"))
    otp_hash: Mapped[str] = mapped_column(String(64))
    salt: Mapped[str] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    expires_at: Mapped[datetime]
    verified_at: Mapped[datetime | None]
    invalidated_at: Mapped[datetime | None]
    created_at: Mapped[Created]
    __table_args__ = (
        Index("uq_one_active_otp_per_order", "order_id", unique=True,
              postgresql_where=text("verified_at IS NULL AND invalidated_at IS NULL"),
              sqlite_where=text("verified_at IS NULL AND invalidated_at IS NULL")),
    )
