from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import public_branch
from app.core.errors import NotFound
from app.core.permissions import Principal, guest_token_header, optional_principal
from app.core.ratelimit import limit
from app.db.session import get_db
from app.models.core import Branch
from app.models.payments import Payment
from app.schemas.orders import InitiatePayment, RazorpayVerify
from app.services import orders as order_svc, payments as svc, settings as cfg

router = APIRouter(prefix="/payments", tags=["payments"])
webhooks = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/methods")
async def methods(branch: Branch = Depends(public_branch), db: AsyncSession = Depends(get_db)):
    """Payment choices currently offered to customers (disabled / unconfigured gateways are never listed)."""
    cs = await cfg.get_cafe_settings(db, branch.id)
    return {"methods": cfg.available_payment_methods(cs)}


async def _order(db, order_id, p, guest_token):
    order, role = await order_svc.authorize_order(db, order_id, p, guest_token)
    if role not in ("customer", "guest", "admin"):
        raise NotFound("Order not found")
    return order


@router.post("/orders/{order_id}/initiate", dependencies=[Depends(limit("pay-init", 15, 60))])
async def initiate(order_id: uuid.UUID, body: InitiatePayment, p: Principal | None = Depends(optional_principal),
                   guest_token: str | None = Depends(guest_token_header), db: AsyncSession = Depends(get_db)):
    """Create (or reuse) a gateway payment for an order awaiting payment. Returns only the client-SDK payload, never secrets."""
    order = await _order(db, order_id, p, guest_token)
    return await svc.initiate_online(db, order, body.gateway)


@router.post("/orders/{order_id}/verify/razorpay", dependencies=[Depends(limit("pay-verify", 30, 60))])
async def verify_razorpay(order_id: uuid.UUID, body: RazorpayVerify, p: Principal | None = Depends(optional_principal),
                          guest_token: str | None = Depends(guest_token_header), db: AsyncSession = Depends(get_db)):
    """Optional fast-path after the Razorpay checkout callback. The result still comes from a server-side status fetch."""
    order = await _order(db, order_id, p, guest_token)
    pay = await svc.verify_razorpay_callback(db, order, razorpay_order_id=body.razorpay_order_id, razorpay_payment_id=body.razorpay_payment_id,
                                             razorpay_signature=body.razorpay_signature)
    return {"payment_id": pay.id, "status": pay.status, "order_payment_status": order.payment_status}


@router.post("/orders/{order_id}/sync", dependencies=[Depends(limit("pay-sync", 30, 60))])
async def sync(order_id: uuid.UUID, p: Principal | None = Depends(optional_principal), guest_token: str | None = Depends(guest_token_header),
               db: AsyncSession = Depends(get_db)):
    """Poll after returning from a gateway page (Cashfree redirect etc.): refreshes status straight from the provider."""
    order = await _order(db, order_id, p, guest_token)
    pays = (await db.execute(select(Payment).where(Payment.order_id == order.id, Payment.method == "online",
                                                   Payment.status.in_(("created", "pending", "failed"))))).scalars().all()
    for pay in pays:
        await svc.sync_payment(db, pay, "customer_sync")
    await db.refresh(order)
    return {"order_status": order.status, "payment_status": order.payment_status}


@router.get("/orders/{order_id}")
async def order_payments(order_id: uuid.UUID, p: Principal | None = Depends(optional_principal), guest_token: str | None = Depends(guest_token_header),
                         db: AsyncSession = Depends(get_db)):
    order = await _order(db, order_id, p, guest_token)
    rows = (await db.execute(select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at))).scalars().all()
    return [{"id": r.id, "method": r.method, "gateway": r.gateway, "status": r.status, "amount_paise": r.amount_paise, "currency": r.currency,
             "refunded_paise": r.refunded_paise, "paid_at": r.paid_at, "created_at": r.created_at} for r in rows]


async def _hook(name: str, request: Request, db: AsyncSession) -> dict:
    raw = await request.body()
    return await svc.handle_webhook(db, name, raw, {k.lower(): v for k, v in request.headers.items()})


@webhooks.post("/razorpay")
async def razorpay_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    return await _hook("razorpay", request, db)


@webhooks.post("/cashfree")
async def cashfree_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    return await _hook("cashfree", request, db)
