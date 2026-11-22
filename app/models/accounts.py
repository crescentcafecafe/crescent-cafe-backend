"""Customer account extras: password-reset OTPs and saved delivery addresses."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Created, Updated, UUIDPk


class PasswordResetOtp(Base):
    """One row per OTP sent. Only a salted hash is stored. An OTP can be verified once, then exchanged for one password change."""
    __tablename__ = "password_reset_otps"
    id: Mapped[UUIDPk]
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    otp_hash: Mapped[str] = mapped_column(String(64))
    salt: Mapped[str] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    expires_at: Mapped[datetime]
    verified_at: Mapped[datetime | None]  # set when the correct OTP was entered
    consumed_at: Mapped[datetime | None]  # set when the password was changed, or the OTP was burned/superseded
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[Created]
    __table_args__ = (Index("ix_pwreset_customer_created", "customer_id", "created_at"),)


class CustomerAddress(Base):
    __tablename__ = "customer_addresses"
    id: Mapped[UUIDPk]
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    label: Mapped[str] = mapped_column(String(20), default="home")  # home | work | other
    line1: Mapped[str] = mapped_column(String(200))
    line2: Mapped[str | None] = mapped_column(String(200))
    landmark: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(80))
    postal_code: Mapped[str] = mapped_column(String(12))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    location_source: Mapped[str] = mapped_column(String(8), default="manual")  # gps | pin | manual
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None]
    created_at: Mapped[Created]
    updated_at: Mapped[Updated]
