from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Created, Updated, UUIDPk


class DiningTable(Base):
    __tablename__ = "tables"
    id: Mapped[UUIDPk]
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), index=True)
    number: Mapped[str] = mapped_column(String(20))
    label: Mapped[str | None] = mapped_column(String(80))
    capacity: Mapped[int | None] = mapped_column(Integer)
    area: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    pin_hash: Mapped[str | None] = mapped_column(String(64))  # optional table PIN (future presence check)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[Created]
    updated_at: Mapped[Updated]
    __table_args__ = (
        Index("uq_table_branch_number_live", "branch_id", "number", unique=True,
              postgresql_where=text("NOT is_archived"), sqlite_where=text("NOT is_archived")),
    )


class TableQrToken(Base):
    __tablename__ = "table_qr_tokens"
    id: Mapped[UUIDPk]
    table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tables.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(12), default="active")  # active | revoked
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    revoked_at: Mapped[datetime | None]
    revoke_reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[Created]
    __table_args__ = (
        Index("uq_one_active_qr_per_table", "table_id", unique=True,
              postgresql_where=text("status = 'active'"), sqlite_where=text("status = 'active'")),
    )


class TableSession(Base):
    __tablename__ = "table_sessions"
    id: Mapped[UUIDPk]
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), index=True)
    table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tables.id"), index=True)
    qr_token_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("table_qr_tokens.id"))
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | closed
    opened_at: Mapped[Created]
    closed_at: Mapped[datetime | None]
    closed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    close_reason: Mapped[str | None] = mapped_column(String(200))
    transferred_from_table_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    __table_args__ = (
        Index("uq_one_open_session_per_table", "table_id", unique=True,
              postgresql_where=text("status = 'open'"), sqlite_where=text("status = 'open'")),
    )
