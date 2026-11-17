from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.core.permissions import Principal, guest_token_header, optional_principal
from app.db.session import get_db
from app.services import invoices as svc, orders as order_svc

router = APIRouter(prefix="/invoices", tags=["invoices"])


async def _inv(db, order_id, p, guest_token):
    order, role = await order_svc.authorize_order(db, order_id, p, guest_token)
    if role == "rider":
        raise NotFound("Order not found")
    return await svc.ensure_invoice(db, order)


@router.get("/order/{order_id}")
async def invoice_for_order(order_id: uuid.UUID, p: Principal | None = Depends(optional_principal), guest_token: str | None = Depends(guest_token_header),
                            db: AsyncSession = Depends(get_db)):
    """Stable invoice data (JSON) for the frontend to render/share. Admin or the order's own customer/guest only."""
    return await svc.invoice_data(db, await _inv(db, order_id, p, guest_token))


@router.get("/order/{order_id}/pdf")
async def invoice_pdf(order_id: uuid.UUID, p: Principal | None = Depends(optional_principal), guest_token: str | None = Depends(guest_token_header),
                      db: AsyncSession = Depends(get_db)):
    inv = await _inv(db, order_id, p, guest_token)
    pdf = svc.render_pdf(await svc.invoice_data(db, inv))
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{inv.invoice_number}.pdf"'})
