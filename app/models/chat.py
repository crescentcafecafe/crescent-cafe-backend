from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, Uuid, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime

from app.db.base import Base, BigPK, Created, UUIDPk


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[UUIDPk]
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # customer_admin | rider_admin
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"), index=True)
    rider_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("riders.id"), index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"), index=True)
    subject: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(10), default="open")
    last_message_at: Mapped[datetime | None]
    created_at: Mapped[Created]
    __table_args__ = (Index("ix_conv_last", "last_message_at"),)


class ConversationMember(Base):
    __tablename__ = "conversation_members"
    id: Mapped[UUIDPk]
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(10))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    last_read_message_id: Mapped[int] = mapped_column(BigPK, default=0)
    __table_args__ = (UniqueConstraint("conversation_id", "role", "user_id", name="uq_conv_member"),
                      Index("ix_member_user", "role", "user_id"))


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(BigPK, primary_key=True, autoincrement=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    sender_role: Mapped[str] = mapped_column(String(10))  # admin | customer | rider | system
    sender_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    body: Mapped[str] = mapped_column(Text)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)  # admin-only note, never shown to customer/rider
    client_msg_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[Created]
    __table_args__ = (
        Index("ix_msg_conv_id", "conversation_id", "id"),
        UniqueConstraint("conversation_id", "sender_role", "sender_id", "client_msg_id", name="uq_msg_client_id"),
    )


class MessageReadReceipt(Base):
    __tablename__ = "message_read_receipts"
    id: Mapped[int] = mapped_column(BigPK, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(10))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    read_at: Mapped[Created]
    __table_args__ = (UniqueConstraint("message_id", "role", "user_id", name="uq_receipt"),)
