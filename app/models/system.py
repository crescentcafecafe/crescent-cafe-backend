from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigPK, Created, UUIDPk, utcnow


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigPK, primary_key=True, autoincrement=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_role: Mapped[str] = mapped_column(String(12))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict | None]
    after: Mapped[dict | None]
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[Created]
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)


class Job(Base):
    """Postgres-backed job queue (FOR UPDATE SKIP LOCKED). Free-tier friendly: no Redis worker required."""

    __tablename__ = "jobs"
    id: Mapped[UUIDPk]
    kind: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(10), default="queued")  # queued | running | done | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=8)
    run_at: Mapped[datetime] = mapped_column(default=utcnow)
    locked_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    created_at: Mapped[Created]
    finished_at: Mapped[datetime | None]
    __table_args__ = (Index("ix_jobs_due", "status", "run_at"),)
