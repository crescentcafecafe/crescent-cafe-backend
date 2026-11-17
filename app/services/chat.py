from __future__ import annotations

import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, Invalid, NotFound
from app.core.ratelimit import limit_key
from app.core.security import now_utc
from app.models.chat import Conversation, ConversationMember, Message, MessageReadReceipt
from app.models.core import AdminUser, Customer, Rider
from app.models.delivery import RiderAssignment
from app.models.orders import Order
from app.services import events as ev
from app.websockets.manager import manager

MAX_BODY = 2000


async def _primary_admin(db: AsyncSession) -> AdminUser:
    a = (await db.execute(select(AdminUser).where(AdminUser.is_primary.is_(True)))).scalar_one_or_none()
    if a is None:
        raise NotFound("Admin not configured")
    return a


async def open_conversation(db: AsyncSession, principal, *, order_id: uuid.UUID | None = None, subject: str | None = None) -> Conversation:
    """Customer or Rider opens (or reuses) their conversation with the Admin."""
    admin = await _primary_admin(db)
    kind = {"customer": "customer_admin", "rider": "rider_admin"}.get(principal.role)
    if kind is None:
        raise Forbidden("Use the admin chat endpoints", code="wrong_role")
    order = None
    if order_id:
        order = await db.get(Order, order_id)
        ok = False
        if order is not None:
            if principal.role == "customer":
                ok = order.customer_id == principal.id
            else:
                ok = (await db.execute(select(func.count()).select_from(RiderAssignment).where(RiderAssignment.order_id == order_id, RiderAssignment.rider_id == principal.id))).scalar_one() > 0
        if not ok:
            raise NotFound("Order not found")  # cannot start chats about other people's orders
    col = Conversation.customer_id if principal.role == "customer" else Conversation.rider_id
    q = select(Conversation).where(col == principal.id, Conversation.kind == kind, Conversation.status == "open",
                                   Conversation.order_id == order_id if order_id else Conversation.order_id.is_(None))
    conv = (await db.execute(q)).scalars().first()
    if conv:
        return conv
    conv = Conversation(branch_id=admin.branch_id, kind=kind, order_id=order_id, subject=(subject or (f"Order {order.order_number}" if order else "General"))[:200],
                        customer_id=principal.id if principal.role == "customer" else None, rider_id=principal.id if principal.role == "rider" else None)
    db.add(conv)
    await db.flush()
    db.add_all([ConversationMember(conversation_id=conv.id, role=principal.role, user_id=principal.id),
                ConversationMember(conversation_id=conv.id, role="admin", user_id=admin.id)])
    await db.flush()
    return conv


async def admin_start_conversation(db: AsyncSession, admin: AdminUser, *, customer_id=None, rider_id=None, order_id=None) -> Conversation:
    if bool(customer_id) == bool(rider_id):
        raise Invalid("Provide exactly one of customer_id or rider_id")
    kind = "customer_admin" if customer_id else "rider_admin"
    target = await db.get(Customer if customer_id else Rider, customer_id or rider_id)
    if target is None:
        raise NotFound("User not found")
    col = Conversation.customer_id if customer_id else Conversation.rider_id
    q = select(Conversation).where(col == (customer_id or rider_id), Conversation.status == "open",
                                   Conversation.order_id == order_id if order_id else Conversation.order_id.is_(None))
    conv = (await db.execute(q)).scalars().first()
    if conv:
        return conv
    conv = Conversation(branch_id=admin.branch_id, kind=kind, customer_id=customer_id, rider_id=rider_id, order_id=order_id, subject="Message from cafe")
    db.add(conv)
    await db.flush()
    db.add_all([ConversationMember(conversation_id=conv.id, role="customer" if customer_id else "rider", user_id=customer_id or rider_id),
                ConversationMember(conversation_id=conv.id, role="admin", user_id=admin.id)])
    await db.flush()
    return conv


async def require_member(db: AsyncSession, conv_id: uuid.UUID, principal) -> tuple[Conversation, ConversationMember]:
    conv = await db.get(Conversation, conv_id)
    m = None
    if conv is not None:
        m = (await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == conv_id, ConversationMember.role == principal.role,
                                                               ConversationMember.user_id == principal.id))).scalar_one_or_none()
    if conv is None or m is None:
        raise NotFound("Conversation not found")  # membership is verified before anything is returned or sent
    return conv, m


def _msg(m: Message, role: str) -> dict:
    return {"id": m.id, "conversation_id": m.conversation_id, "sender_role": m.sender_role, "sender_id": m.sender_id, "body": m.body,
            "client_msg_id": m.client_msg_id, "created_at": m.created_at, **({"is_internal": m.is_internal} if role == "admin" else {})}


async def send_message(db: AsyncSession, conv: Conversation, principal, body: str, client_msg_id: str | None, internal: bool = False) -> dict:
    body = (body or "").strip()
    if not body:
        raise Invalid("Message cannot be empty", code="empty_message")
    if len(body) > MAX_BODY:
        raise Invalid(f"Message is too long (max {MAX_BODY} characters)", code="message_too_long")
    if internal and principal.role != "admin":
        raise Forbidden("Only the admin can add internal notes", code="wrong_role")
    if conv.status != "open":
        raise Forbidden("This conversation is closed", code="conversation_closed")
    await limit_key("chat", f"{principal.role}:{principal.id}", 30, 60)
    if client_msg_id:
        dup = (await db.execute(select(Message).where(Message.conversation_id == conv.id, Message.sender_role == principal.role, Message.sender_id == principal.id,
                                                       Message.client_msg_id == client_msg_id))).scalar_one_or_none()
        if dup:
            return _msg(dup, principal.role)
    m = Message(conversation_id=conv.id, sender_role=principal.role, sender_id=principal.id, body=body, client_msg_id=client_msg_id, is_internal=internal)
    try:
        async with db.begin_nested():
            db.add(m)
            await db.flush()
    except IntegrityError:
        dup = (await db.execute(select(Message).where(Message.conversation_id == conv.id, Message.client_msg_id == client_msg_id))).scalar_one()
        return _msg(dup, principal.role)
    conv.last_message_at = now_utc()
    if internal:
        return _msg(m, "admin")  # internal notes are never broadcast or pushed
    members = (await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == conv.id))).scalars().all()
    topic = f"conversation:{conv.id}"
    recipients, extra_topics = [], []
    for mem in members:
        if (mem.role, mem.user_id) == (principal.role, principal.id):
            continue
        extra_topics.append(f"account:{mem.role}:{mem.user_id}")
        if not manager.is_subscribed(topic, mem.role, str(mem.user_id)):  # online & looking at the chat => no push needed
            recipients.append({"role": mem.role, "id": str(mem.user_id)})
    await ev.emit(db, type="chat.message", topics=[topic] + extra_topics, dedupe_key=f"chat.msg:{m.id}", recipients=recipients,
                  title="New message", body=body[:120], data={"conversation_id": conv.id, "message": _msg(m, "customer")}, branch_id=conv.branch_id)
    return _msg(m, principal.role)


async def list_messages(db: AsyncSession, conv: Conversation, principal, before_id: int | None, limit: int) -> list[dict]:
    limit = max(1, min(limit, 100))
    q = select(Message).where(Message.conversation_id == conv.id)
    if principal.role != "admin":
        q = q.where(Message.is_internal.is_(False))
    if before_id:
        q = q.where(Message.id < before_id)
    rows = (await db.execute(q.order_by(Message.id.desc()).limit(limit))).scalars().all()
    return [_msg(m, principal.role) for m in rows]


async def mark_read(db: AsyncSession, conv: Conversation, member: ConversationMember, principal, up_to: int | None) -> int:
    q = select(Message).where(Message.conversation_id == conv.id, Message.id > member.last_read_message_id,
                              or_(Message.sender_role != principal.role, Message.sender_id != principal.id))
    if principal.role != "admin":
        q = q.where(Message.is_internal.is_(False))
    if up_to:
        q = q.where(Message.id <= up_to)
    msgs = (await db.execute(q)).scalars().all()
    for m in msgs:
        try:
            async with db.begin_nested():
                db.add(MessageReadReceipt(message_id=m.id, role=principal.role, user_id=principal.id))
                await db.flush()
        except IntegrityError:
            pass
    if msgs:
        member.last_read_message_id = max(m.id for m in msgs)
        ev.publish_ephemeral(db, type="chat.read", topics=[f"conversation:{conv.id}"], data={"conversation_id": conv.id, "reader_role": principal.role,
                                                                                       "read_up_to": member.last_read_message_id})
    return len(msgs)


async def unread_count(db: AsyncSession, conv: Conversation, member: ConversationMember, principal) -> int:
    q = select(func.count()).select_from(Message).where(Message.conversation_id == conv.id, Message.id > member.last_read_message_id,
                                                        or_(Message.sender_role != principal.role, Message.sender_id != principal.id))
    if principal.role != "admin":
        q = q.where(Message.is_internal.is_(False))
    return (await db.execute(q)).scalar_one()


async def list_conversations(db: AsyncSession, principal, *, status: str | None = None, kind: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    q = select(Conversation, ConversationMember).join(ConversationMember, and_(ConversationMember.conversation_id == Conversation.id,
                                                                               ConversationMember.role == principal.role, ConversationMember.user_id == principal.id))
    if status:
        q = q.where(Conversation.status == status)
    if kind:
        q = q.where(Conversation.kind == kind)
    rows = (await db.execute(q.order_by(Conversation.last_message_at.desc().nullslast(), Conversation.created_at.desc()).limit(limit).offset(offset))).all()
    out = []
    for conv, mem in rows:
        last = (await db.execute(select(Message).where(Message.conversation_id == conv.id, *([Message.is_internal.is_(False)] if principal.role != "admin" else []))
                                 .order_by(Message.id.desc()).limit(1))).scalar_one_or_none()
        who = None
        if principal.role == "admin":
            if conv.customer_id:
                c = await db.get(Customer, conv.customer_id)
                who = {"role": "customer", "id": c.id, "name": c.name, "phone": c.phone}
            elif conv.rider_id:
                r = await db.get(Rider, conv.rider_id)
                who = {"role": "rider", "id": r.id, "name": r.name, "phone": r.phone}
        out.append({"id": conv.id, "kind": conv.kind, "subject": conv.subject, "order_id": conv.order_id, "status": conv.status, "participant": who,
                    "last_message": (_msg(last, principal.role) if last else None), "last_message_at": conv.last_message_at,
                    "unread_count": await unread_count(db, conv, mem, principal)})
    return out
