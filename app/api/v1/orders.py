from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import page, public_branch
from app.core.errors import Conflict, NotFound
from app.core.permissions import Principal, current_customer, guest_token_header, optional_customer, optional_principal
from app.core.ratelimit import limit
from app.db.session import get_db
from app.models.core import Branch
from app.models.orders import Order
from app.schemas.orders import CancelRequest, OrderCreate
from app.services import order_state as st
from app.services import orders as svc

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("", status_code=201, dependencies=[Depends(limit("order", 20, 60))])
async def place_order(body: OrderCreate, branch: Branch = Depends(public_branch), customer: Principal | None = Depends(optional_customer),
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100), db: AsyncSession = Depends(get_db)):
    """Place a customer/guest order (app or QR website). Send an Idempotency-Key header to make retries safe.
    Guests receive guest_token ONCE in the response; keep it to track the order (header X-Guest-Token)."""
    order, guest_token, dup = await svc.create_order(db, branch_id=branch.id, data=body, customer=customer.user if customer else None, admin_id=None,
                                                     idempotency_key=idempotency_key)
    role = "customer" if customer else "guest"
    view = await svc.order_view(db, order, role)
    return {"order": view, "guest_token": guest_token, "duplicate": dup,
            "next_action": {"type": "pay_online", "gateways": [order.payment_method]} if order.status == "payment_pending" else None}


@router.get("")
async def my_orders(status: str | None = None, limit_: int = 20, offset: int = 0, c: Principal = Depends(current_customer), db: AsyncSession = Depends(get_db)):
    limit_, offset = page(limit_, offset)
    q = select(Order).where(Order.customer_id == c.id)
    if status:
        q = q.where(Order.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(Order.created_at.desc()).limit(limit_).offset(offset))).scalars().all()
    return {"items": [svc.order_summary(o) for o in rows], "total": total, "limit": limit_, "offset": offset}


@router.get("/{order_id}")
async def get_order(order_id: uuid.UUID, p: Principal | None = Depends(optional_principal), guest_token: str | None = Depends(guest_token_header),
                    db: AsyncSession = Depends(get_db)):
    order, role = await svc.authorize_order(db, order_id, p, guest_token)
    return await svc.order_view(db, order, role, with_history=True)


@router.post("/{order_id}/cancel")
async def cancel_order(order_id: uuid.UUID, body: CancelRequest, p: Principal | None = Depends(optional_principal),
                       guest_token: str | None = Depends(guest_token_header), db: AsyncSession = Depends(get_db)):
    """Customers/guests may cancel only before the cafe accepts the order."""
    order, role = await svc.authorize_order(db, order_id, p, guest_token)
    if role not in ("customer", "guest"):
        raise NotFound("Order not found")
    if order.status not in st.CUSTOMER_CANCELLABLE:
        raise Conflict("The order can no longer be cancelled. Please contact the cafe.", code="not_cancellable", details={"status": order.status})
    order = await st.transition(db, order, "cancelled", actor_role="customer", actor_id=order.customer_id, reason=body.reason)
    return await svc.order_view(db, order, role)
