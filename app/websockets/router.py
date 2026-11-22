"""Authenticated WebSocket: per-topic authorization, expiry-aware, reconnect-friendly.

Connect:  wss://HOST/api/v1/ws?token=<access_jwt>          (or Authorization: Bearer header)
Guest:    wss://HOST/api/v1/ws?guest_token=<token>&order_id=<uuid>
Client -> server: {"type":"subscribe","topic":"order:<id>"} | {"type":"unsubscribe",...} | {"type":"ping"}
Server -> client: {"id": <event seq|null>, "type": "...", "topic": "...", "ts": "...", "data": {...}}
After any reconnect the client must refetch state (REST) and may call GET /notifications/events?after_id=<last id>."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select

from app.core.errors import AppError
from app.core.permissions import principal_from_token
from app.core.security import decode_token, hash_token, now_utc
from app.db.session import session_scope
from app.models.chat import ConversationMember
from app.models.delivery import RiderAssignment
from app.models.orders import Order
from app.websockets.manager import manager

log = logging.getLogger(__name__)
router = APIRouter()


async def _authenticate(ws: WebSocket):
    token = ws.query_params.get("token")
    auth = ws.headers.get("authorization", "")
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:]
    async with session_scope() as db:
        if token:
            p = await principal_from_token(db, token)
            exp = decode_token(token, "access")["exp"]
            return {"role": p.role, "id": p.id, "exp": float(exp), "guest_order": None}
        gt, oid = ws.query_params.get("guest_token"), ws.query_params.get("order_id")
        if gt and oid:
            o = await db.get(Order, uuid.UUID(oid))
            if o and o.guest_token_hash and hash_token(gt) == o.guest_token_hash:
                return {"role": "guest", "id": o.id, "exp": time.time() + 3600, "guest_order": o.id}
    raise AppError("unauthorized", status_code=401)


async def _authorize_topic(me: dict, topic: str) -> bool:
    kind, _, ref = topic.partition(":")
    role, uid = me["role"], me["id"]
    if topic == "menu":
        return True
    if topic == "admin":
        return role == "admin"
    if kind == "account":
        r, _, i = ref.partition(":")
        return r == role and i == str(uid)
    try:
        rid = uuid.UUID(ref)
    except ValueError:
        return False
    async with session_scope() as db:
        if kind in ("order", "tracking"):
            if role == "admin":
                return True
            if role == "guest":
                return me["guest_order"] == rid
            o = await db.get(Order, rid)
            if o is None:
                return False
            if role == "customer":
                return o.customer_id == uid
            if role == "rider" and kind == "order":
                n = (await db.execute(select(func.count()).select_from(RiderAssignment).where(RiderAssignment.order_id == rid, RiderAssignment.rider_id == uid,
                                                                                            RiderAssignment.status.in_(("offered", "accepted", "picked_up"))))).scalar_one()
                return n > 0
            return False
        if kind == "conversation":
            if role == "guest":
                return False
            n = (await db.execute(select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == rid, ConversationMember.role == role,
                                                                                            ConversationMember.user_id == uid))).scalar_one()
            return n > 0
    return False


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    try:
        me = await _authenticate(ws)
    except (AppError, jwt.PyJWTError, ValueError):
        await ws.close(code=4401)
        return
    await ws.accept()
    ws.state.principal_key = (me["role"], str(me["id"]))
    if me["role"] != "guest":
        manager.subscribe(ws, f"account:{me['role']}:{me['id']}")
    if me["role"] == "admin":
        manager.subscribe(ws, "admin")
    await ws.send_json({"type": "connected", "ts": now_utc().isoformat(), "role": me["role"],
                        "note": "Refetch state via REST after (re)connecting; WebSocket events are not permanent storage."})
    try:
        while True:
            remaining = me["exp"] - time.time()
            if remaining <= 0:
                await ws.send_json({"type": "auth_expired"})
                await ws.close(code=4401)
                return
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=min(remaining, 60))
            except asyncio.TimeoutError:
                continue
            t = msg.get("type") if isinstance(msg, dict) else None
            if t == "ping":
                await ws.send_json({"type": "pong", "ts": now_utc().isoformat()})
            elif t in ("subscribe", "unsubscribe") and isinstance(msg.get("topic"), str):
                topic = msg["topic"][:100]
                if t == "unsubscribe":
                    manager.unsubscribe(ws, topic)
                    await ws.send_json({"type": "unsubscribed", "topic": topic})
                elif await _authorize_topic(me, topic):
                    manager.subscribe(ws, topic)
                    await ws.send_json({"type": "subscribed", "topic": topic})
                else:
                    await ws.send_json({"type": "error", "code": "forbidden", "topic": topic})
            else:
                await ws.send_json({"type": "error", "code": "bad_message"})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket error")
    finally:
        manager.disconnect(ws)
