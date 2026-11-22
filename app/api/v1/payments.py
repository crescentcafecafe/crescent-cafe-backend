from __future__ import annotations

import logging
import uuid
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import public_branch
from app.core.config import get_settings
from app.core.errors import AppError, NotFound, Unauthorized
from app.core.permissions import Principal, guest_token_header, optional_principal
from app.core.ratelimit import limit
from app.db.session import get_db
from app.models.core import Branch
from app.models.orders import Order
from app.models.payments import Payment
from app.schemas.orders import InitiatePayment, RazorpayVerify
from app.services import orders as order_svc, payments as svc, settings as cfg

log = logging.getLogger(__name__)
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


def _back_to_site(order_id: uuid.UUID | None, outcome: str, reason: str | None = None) -> RedirectResponse:
    """303 so the browser turns Razorpay's POST into a GET of our order page. Only a fixed site URL + a UUID + a short encoded reason go in it."""
    base = get_settings().frontend_base
    path = f"/orders/{order_id}" if order_id else "/orders"
    q = {"payment": outcome}
    if reason:
        q["reason"] = reason[:160]
    return RedirectResponse(f"{base}{path}?{urlencode(q)}", status_code=303)


@router.api_route("/razorpay/callback/{order_id}", methods=["GET", "POST"], include_in_schema=False,
                  dependencies=[Depends(limit("pay-callback", 60, 60))])
async def razorpay_redirect_callback(order_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Razorpay "redirect mode" return URL, used by the Android WebView app where checkout popups are unreliable.
    Razorpay POSTs razorpay_payment_id / razorpay_order_id / razorpay_signature here after a successful payment (or error[...] fields after
    a failed one). Nothing here is trusted on its own: the signature is checked AND the status is re-fetched from Razorpay. The customer is
    always sent back to the website's order page, which shows the real result."""
    raw = await request.body()
    fields: dict[str, str] = {k: v[0] for k, v in parse_qs(raw[:16384].decode("utf-8", "replace"), keep_blank_values=True).items()}
    for k, v in request.query_params.items():
        fields.setdefault(k, v)
    order = await db.get(Order, order_id)
    if order is None:
        return _back_to_site(None, "error", "We could not find this order.")
    pid, oid, sig = fields.get("razorpay_payment_id"), fields.get("razorpay_order_id"), fields.get("razorpay_signature")
    if pid and oid and sig:
        try:
            await svc.verify_razorpay_callback(db, order, razorpay_order_id=oid, razorpay_payment_id=pid, razorpay_signature=sig)
            await db.refresh(order)
            return _back_to_site(order.id, "paid" if order.payment_status == "paid" else "pending")
        except Unauthorized:
            return _back_to_site(order.id, "error", "We could not verify this payment. If money was deducted it will be matched automatically.")
        except (AppError, NotFound) as e:
            log.warning("razorpay callback could not confirm order=%s: %s", order_id, e)
            return _back_to_site(order.id, "pending")
    # Failed or cancelled: show the customer Razorpay's reason, and refresh our record from Razorpay (best effort).
    reason = fields.get("error[description]") or fields.get("error_description") or "The payment was not completed."
    outcome = "cancelled" if "cancel" in reason.lower() else "failed"
    try:
        pays = (await db.execute(select(Payment).where(Payment.order_id == order.id, Payment.gateway == "razorpay",
                                                       Payment.status.in_(("created", "pending"))))).scalars().all()
        for pay in pays:
            await svc.sync_payment(db, pay, "redirect_callback")
        await db.refresh(order)
        if order.payment_status == "paid":
            outcome = "paid"
    except Exception:  # noqa: BLE001 - the redirect must always happen
        log.warning("razorpay callback sync failed order=%s", order_id, exc_info=True)
    return _back_to_site(order.id, outcome, None if outcome == "paid" else reason)


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
