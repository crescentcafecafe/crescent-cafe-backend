from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Conflict, Forbidden, Invalid, NotFound, Unauthorized
from app.core.security import consteq, create_scoped_token, decode_token, hash_token, new_token, now_utc
from app.models.orders import Order
from app.models.tables import DiningTable, TableQrToken, TableSession
from app.services.audit import audit
from app.services.settings import get_cafe_settings

import jwt

TERMINAL = ("rejected", "cancelled", "refunded", "completed", "delivered")


def qr_url(token: str) -> str:
    return f"{get_settings().qr_site_url.rstrip('/')}/t/{token}"


async def generate_qr(db: AsyncSession, table: DiningTable, admin_id: uuid.UUID, reason: str = "regenerated") -> dict:
    """Create a new token and revoke the previous active one. Plaintext is returned ONCE; only its hash is stored."""
    if table.is_archived or not table.is_active:
        raise Conflict("Table is disabled or archived", code="table_inactive")
    now = now_utc()
    await db.execute(update(TableQrToken).where(TableQrToken.table_id == table.id, TableQrToken.status == "active")
                     .values(status="revoked", revoked_at=now, revoked_by=admin_id, revoke_reason=reason))
    await db.flush()
    token = new_token(24)
    row = TableQrToken(table_id=table.id, token_hash=hash_token(token), created_by=admin_id)
    db.add(row)
    await db.flush()
    await audit(db, actor_role="admin", actor_id=admin_id, action="qr.generated", entity_type="table", entity_id=table.id,
                after={"qr_token_id": str(row.id)}, branch_id=table.branch_id)
    return {"table_id": table.id, "table_number": table.number, "label": table.label, "area": table.area,
            "qr_token_id": row.id, "token": token, "url": qr_url(token), "generated_at": now}


async def revoke_qr(db: AsyncSession, table: DiningTable, admin_id: uuid.UUID, reason: str) -> int:
    res = await db.execute(update(TableQrToken).where(TableQrToken.table_id == table.id, TableQrToken.status == "active")
                           .values(status="revoked", revoked_at=now_utc(), revoked_by=admin_id, revoke_reason=reason[:200]))
    await audit(db, actor_role="admin", actor_id=admin_id, action="qr.revoked", entity_type="table", entity_id=table.id,
                after={"reason": reason}, branch_id=table.branch_id)
    return res.rowcount or 0


async def _open_session(db: AsyncSession, table: DiningTable, qr_token_id: uuid.UUID | None) -> TableSession:
    q = select(TableSession).where(TableSession.table_id == table.id, TableSession.status == "open")
    sess = (await db.execute(q)).scalar_one_or_none()
    if sess:
        return sess
    sess = TableSession(branch_id=table.branch_id, table_id=table.id, qr_token_id=qr_token_id)
    try:
        async with db.begin_nested():
            db.add(sess)
            await db.flush()
    except IntegrityError:  # two customers scanned simultaneously: use the session the other request created
        sess = (await db.execute(q)).scalar_one()
    return sess


def issue_table_token(sess: TableSession, qr_token_id: uuid.UUID | None) -> str:
    return create_scoped_token({"sub": str(sess.id), "typ": "table_session", "tid": str(sess.table_id), "qt": str(qr_token_id) if qr_token_id else None},
                               get_settings().table_session_token_minutes)


async def resolve_qr(db: AsyncSession, token: str, pin: str | None = None) -> dict:
    row = (await db.execute(select(TableQrToken).where(TableQrToken.token_hash == hash_token(token)))).scalar_one_or_none()
    if row is None or row.status != "active":
        raise NotFound("This QR code is no longer valid. Please ask the staff for help.", code="qr_invalid")
    table = await db.get(DiningTable, row.table_id)
    if table is None or table.is_archived or not table.is_active:
        raise NotFound("This table is not available", code="table_inactive")
    cs = await get_cafe_settings(db, table.branch_id)
    if not cs.dine_in_enabled:
        raise Forbidden("Dine-in ordering is currently disabled", code="dine_in_disabled")
    if table.pin_hash and not (pin and consteq(hash_token(pin), table.pin_hash)):
        raise Unauthorized("Table PIN required", code="table_pin_required")
    sess = await _open_session(db, table, row.id)
    return {"table_session_token": issue_table_token(sess, row.id), "table_session_id": sess.id,
            "table": {"id": table.id, "number": table.number, "label": table.label, "area": table.area},
            "expires_in": get_settings().table_session_token_minutes * 60}


async def verify_table_token(db: AsyncSession, token: str) -> tuple[TableSession, DiningTable]:
    try:
        c = decode_token(token, "table_session")
        sid = uuid.UUID(c["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise Unauthorized("Table session expired. Please scan the QR code again.", code="table_session_invalid")
    sess = await db.get(TableSession, sid)
    if sess is None or sess.status != "open":
        raise Unauthorized("This table session has been closed. Please scan the QR code again.", code="table_session_closed")
    if c.get("qt"):
        qt = await db.get(TableQrToken, uuid.UUID(c["qt"]))
        if qt is None or qt.status != "active":
            raise Unauthorized("This QR code was replaced. Please scan the new QR code.", code="qr_invalid")
    table = await db.get(DiningTable, sess.table_id)
    if table is None or table.is_archived or not table.is_active:
        raise Conflict("Table unavailable", code="table_inactive")
    return sess, table


async def open_session_for_admin(db: AsyncSession, table: DiningTable) -> TableSession:
    return await _open_session(db, table, None)


async def close_session(db: AsyncSession, sess: TableSession, admin_id: uuid.UUID, reason: str | None) -> TableSession:
    if sess.status == "closed":
        raise Conflict("Session already closed", code="already_closed")
    sess.status, sess.closed_at, sess.closed_by, sess.close_reason = "closed", now_utc(), admin_id, (reason or "closed by admin")[:200]
    await audit(db, actor_role="admin", actor_id=admin_id, action="table_session.closed", entity_type="table_session", entity_id=sess.id,
                after={"reason": reason}, branch_id=sess.branch_id)
    return sess


async def transfer_session(db: AsyncSession, sess: TableSession, new_table: DiningTable, admin_id: uuid.UUID) -> TableSession:
    if sess.status != "open":
        raise Conflict("Only an open session can be transferred", code="session_closed")
    if new_table.id == sess.table_id:
        raise Invalid("Session is already on that table")
    if new_table.branch_id != sess.branch_id or new_table.is_archived or not new_table.is_active:
        raise Conflict("Target table unavailable", code="table_inactive")
    busy = (await db.execute(select(TableSession).where(TableSession.table_id == new_table.id, TableSession.status == "open"))).scalar_one_or_none()
    if busy:
        raise Conflict("Target table already has an open session", code="table_busy")
    old = sess.table_id
    sess.transferred_from_table_id, sess.table_id = old, new_table.id
    label = f"Table {new_table.number}" + (f" ({new_table.label})" if new_table.label else "")
    await db.execute(update(Order).where(Order.table_session_id == sess.id, Order.status.not_in(TERMINAL))
                     .values(table_id=new_table.id, table_label=label))
    await audit(db, actor_role="admin", actor_id=admin_id, action="table_session.transferred", entity_type="table_session", entity_id=sess.id,
                before={"table_id": str(old)}, after={"table_id": str(new_table.id)}, branch_id=sess.branch_id)
    return sess
