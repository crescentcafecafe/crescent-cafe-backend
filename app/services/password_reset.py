"""Forgot password with an e-mailed one-time code.

Flow: request_otp -> (e-mail) -> verify_otp (returns a short-lived reset token) -> reset_password.
Rules: the OTP is stored only as a salted HMAC, expires, allows a few wrong tries, can be verified once and is consumed by the
password change, resending has a cooldown and an hourly cap, and answers never reveal whether an e-mail is registered."""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

import jwt
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Invalid, Unauthorized
from app.core.security import (consteq, create_scoped_token, decode_token, hash_otp, hash_password, new_numeric_otp, new_token, now_utc,
                               validate_password_strength)
from app.models.accounts import PasswordResetOtp
from app.models.core import Customer
from app.services import auth as auth_svc
from app.services import email_templates as tpl
from app.services import emailing
from app.services.audit import audit

log = logging.getLogger(__name__)
GENERIC_BAD_CODE = "That code is incorrect or has expired. Request a new one."


async def _customer_by_email(db: AsyncSession, email: str) -> Customer | None:
    c = (await db.execute(select(Customer).where(Customer.email == email.lower().strip()))).scalar_one_or_none()
    return c if (c is not None and c.is_active and c.anonymized_at is None) else None


async def request_otp(db: AsyncSession, email: str, ip: str | None) -> tuple[str, str, str] | None:
    """Creates an OTP. Returns (otp, name, email) to be e-mailed, or None when nothing should be sent (unknown e-mail, cooldown, cap).
    The caller answers identically in every case."""
    s = get_settings()
    c = await _customer_by_email(db, email)
    if c is None:
        return None
    now = now_utc()
    last = (await db.execute(select(func.max(PasswordResetOtp.created_at)).where(PasswordResetOtp.customer_id == c.id))).scalar_one()
    if last and (now - last).total_seconds() < s.password_reset_resend_seconds:
        return None
    recent = (await db.execute(select(func.count()).select_from(PasswordResetOtp).where(
        PasswordResetOtp.customer_id == c.id, PasswordResetOtp.created_at > now - timedelta(hours=1)))).scalar_one()
    if recent >= s.password_reset_max_per_hour:
        log.warning("password reset cap reached for customer %s", c.id)
        return None
    await db.execute(update(PasswordResetOtp).where(PasswordResetOtp.customer_id == c.id, PasswordResetOtp.consumed_at.is_(None)).values(consumed_at=now))  # only the newest code works
    otp = new_numeric_otp(s.password_reset_otp_digits)
    salt = new_token(12)
    db.add(PasswordResetOtp(customer_id=c.id, otp_hash=hash_otp(otp, salt), salt=salt, max_attempts=s.password_reset_max_attempts,
                            expires_at=now + timedelta(minutes=s.password_reset_otp_minutes), ip=ip))
    await db.flush()
    return otp, c.name, c.email  # type: ignore[return-value]


async def send_otp_email(to: str, name: str, otp: str) -> None:
    """Runs after the response. The code exists only in memory here: it is never logged or put in the job table."""
    subject, html, text = tpl.password_otp(name, otp, get_settings().password_reset_otp_minutes)
    await emailing.deliver(to, subject, html, text, raise_on_error=False)


async def verify_otp(db: AsyncSession, email: str, otp: str) -> tuple[str, int]:
    s = get_settings()
    c = await _customer_by_email(db, email)
    if c is None:
        raise Invalid(GENERIC_BAD_CODE, code="invalid_otp")
    rec = (await db.execute(select(PasswordResetOtp).where(PasswordResetOtp.customer_id == c.id, PasswordResetOtp.consumed_at.is_(None))
                            .order_by(PasswordResetOtp.created_at.desc()).limit(1).with_for_update())).scalar_one_or_none()
    now = now_utc()
    if rec is None or rec.expires_at < now or rec.verified_at is not None:
        raise Invalid(GENERIC_BAD_CODE, code="invalid_otp")
    if not consteq(hash_otp(otp, rec.salt), rec.otp_hash):
        rec.attempts += 1
        if rec.attempts >= rec.max_attempts:
            rec.consumed_at = now  # burned: a new code must be requested
        await db.commit()  # keep the counter even though we raise
        raise Invalid(GENERIC_BAD_CODE, code="invalid_otp")
    rec.verified_at = now
    token = create_scoped_token({"typ": "pwreset", "sub": str(c.id), "otp": str(rec.id)}, s.password_reset_token_minutes)
    return token, s.password_reset_token_minutes * 60


async def reset_password(db: AsyncSession, reset_token: str, new_password: str, ip: str | None) -> None:
    try:
        claims = decode_token(reset_token, "pwreset")
        cid, oid = uuid.UUID(claims["sub"]), uuid.UUID(claims["otp"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise Unauthorized("This reset session expired. Start again.", code="invalid_reset_token")
    err = validate_password_strength(new_password)
    if err:
        raise Invalid(err, code="weak_password")  # checked before consuming so the person can try another password
    rec = (await db.execute(select(PasswordResetOtp).where(PasswordResetOtp.id == oid).with_for_update())).scalar_one_or_none()
    if rec is None or rec.customer_id != cid or rec.verified_at is None or rec.consumed_at is not None:
        raise Unauthorized("This reset session expired. Start again.", code="invalid_reset_token")  # reuse of a used token lands here
    c = await db.get(Customer, cid)
    if c is None or not c.is_active:
        raise Unauthorized("This reset session expired. Start again.", code="invalid_reset_token")
    c.password_hash = hash_password(new_password)
    rec.consumed_at = now_utc()
    await auth_svc.revoke_all_sessions(db, "customer", c.id)
    await audit(db, actor_role="customer", actor_id=c.id, action="customer.password_reset", entity_type="customer", entity_id=c.id, ip=ip)
    await emailing.queue_email(db, "password_changed", customer_id=c.id)
