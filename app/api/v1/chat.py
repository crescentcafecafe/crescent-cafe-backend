from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden
from app.core.permissions import Principal, current_admin, current_principal
from app.db.session import get_db
from app.schemas.admin import AdminStartChat, ChatOpen, ChatRead, ChatSend
from app.schemas.common import OK
from app.services import chat as svc

router = APIRouter(prefix="/chat", tags=["chat"])


def _conv(c) -> dict:
    return {"id": c.id, "kind": c.kind, "subject": c.subject, "order_id": c.order_id, "status": c.status, "customer_id": c.customer_id if False else None,
            "last_message_at": c.last_message_at, "created_at": c.created_at}


@router.get("/conversations")
async def conversations(status: str | None = None, kind: str | None = None, limit: int = 50, offset: int = 0, p: Principal = Depends(current_principal),
                        db: AsyncSession = Depends(get_db)):
    """Admin: the single centralized inbox (all conversations). Customer/Rider: only their own."""
    return await svc.list_conversations(db, p, status=status, kind=kind, limit=max(1, min(limit, 100)), offset=max(0, offset))


@router.post("/conversations", status_code=201)
async def open_conversation(body: ChatOpen, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    if p.role == "admin":
        raise Forbidden("Admins start chats with POST /chat/conversations/admin", code="wrong_role")
    return _conv(await svc.open_conversation(db, p, order_id=body.order_id, subject=body.subject))


@router.post("/conversations/admin", status_code=201)
async def admin_start(body: AdminStartChat, p: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return _conv(await svc.admin_start_conversation(db, p.user, customer_id=body.customer_id, rider_id=body.rider_id, order_id=body.order_id))


@router.get("/conversations/{conversation_id}/messages")
async def messages(conversation_id: uuid.UUID, before_id: int | None = None, limit: int = 30, p: Principal = Depends(current_principal),
                   db: AsyncSession = Depends(get_db)):
    conv, _m = await svc.require_member(db, conversation_id, p)
    msgs = await svc.list_messages(db, conv, p, before_id, limit)
    return {"items": msgs, "next_before_id": msgs[-1]["id"] if len(msgs) == max(1, min(limit, 100)) else None}


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def send(conversation_id: uuid.UUID, body: ChatSend, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    conv, _m = await svc.require_member(db, conversation_id, p)
    return await svc.send_message(db, conv, p, body.body, body.client_msg_id, internal=body.internal)


@router.post("/conversations/{conversation_id}/read")
async def read(conversation_id: uuid.UUID, body: ChatRead | None = None, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    conv, m = await svc.require_member(db, conversation_id, p)
    n = await svc.mark_read(db, conv, m, p, body.up_to_message_id if body else None)
    return {"marked": n}


@router.post("/conversations/{conversation_id}/close", response_model=OK)
async def close(conversation_id: uuid.UUID, p: Principal = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    conv, _m = await svc.require_member(db, conversation_id, p)
    conv.status = "closed"
    return OK()
