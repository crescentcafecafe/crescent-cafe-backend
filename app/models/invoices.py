from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Created, UUIDPk


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[UUIDPk]
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), unique=True)
    invoice_number: Mapped[str] = mapped_column(String(40))
    seller: Mapped[dict]  # snapshot of cafe details (name, address, gstin, phone)
    buyer: Mapped[dict]  # snapshot of customer details
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    subtotal_paise: Mapped[int] = mapped_column(Integer)
    discount_paise: Mapped[int] = mapped_column(Integer)
    tax_paise: Mapped[int] = mapped_column(Integer)
    delivery_charge_paise: Mapped[int] = mapped_column(Integer)
    total_paise: Mapped[int] = mapped_column(Integer)
    tax_breakdown: Mapped[list | None]
    payment_method: Mapped[str] = mapped_column(String(12))
    payment_status: Mapped[str] = mapped_column(String(20))
    footer: Mapped[str | None] = mapped_column(String(500))
    issued_at: Mapped[Created]
    __table_args__ = (UniqueConstraint("branch_id", "invoice_number", name="uq_invoice_number"),)


class InvoiceItem(Base):
    __tablename__ = "invoice_items"
    id: Mapped[UUIDPk]
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    description: Mapped[str] = mapped_column(String(300))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_paise: Mapped[int] = mapped_column(Integer)
    discount_paise: Mapped[int] = mapped_column(Integer)
    tax_rate_bps: Mapped[int] = mapped_column(Integer)
    tax_paise: Mapped[int] = mapped_column(Integer)
    total_paise: Mapped[int] = mapped_column(Integer)
