from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Conflict, Invalid, NotFound, Unauthorized
from app.core.security import (create_access_token, hash_password, hash_token, new_token, now_utc,
                               validate_password_strength, verify_password)
from app.integrations import registry
from app.models.core import AdminUser, AuthSession, Customer, Rider
from app.services.audit import audit
from app.utils.phone import normalize_phone

MAX_FAILED = 5
LOCK_MINUTES = 15


async def issue_session(db: AsyncSession, role: str, user_id: uuid.UUID, *, ip: str | None = None, device_label: str | None = None) -> dict:
    s = get_settings()
    refresh = new_token(48)
    sess = AuthSession(role=role, user_id=user_id, token_hash=hash_token(refresh), ip=ip, device_label=(device_label or "")[:200] or None,
                       expires_at=now_utc() + timedelta(days=s.refresh_token_days), last_used_at=now_utc())
    db.add(sess)
    await db.flush()
    access, expires_in = create_access_token(subject=str(user_id), role=role, session_id=str(sess.id))
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "expires_in": expires_in, "role": role,
            "session_id": str(sess.id)}


def _check_lock(user) -> None:
    if user.locked_until and user.locked_until > now_utc():
        raise Unauthorized("Account temporarily locked due to repeated failed logins. Try again later.", code="account_locked")


async def _fail(db: AsyncSession, user) -> None:
    user.failed_logins += 1
    if user.failed_logins >= MAX_FAILED:
        user.locked_until = now_utc() + timedelta(minutes=LOCK_MINUTES)
        user.failed_logins = 0
    await db.commit()  # persist the counter even though we raise afterwards


async def login_admin(db: AsyncSession, email: str, password: str, ip: str | None, device: str | None) -> dict:
    admin = (await db.execute(select(AdminUser).where(AdminUser.email == email.lower().strip()))).scalar_one_or_none()
    if admin is not None:
        _check_lock(admin)
    if admin is None or not admin.is_active or not verify_password(password, admin.password_hash if admin else None):
        if admin is not None:
            await _fail(db, admin)
        raise Unauthorized("Invalid email or password", code="invalid_credentials")
    admin.failed_logins, admin.last_login_at = 0, now_utc()
    tokens = await issue_session(db, "admin", admin.id, ip=ip, device_label=device)
    await audit(db, actor_role="admin", actor_id=admin.id, action="admin.login", entity_type="admin", entity_id=admin.id, ip=ip, branch_id=admin.branch_id)
    return tokens


async def login_rider(db: AsyncSession, phone: str, password: str, ip: str | None, device: str | None) -> dict:
    try:
        phone = normalize_phone(phone)
    except Invalid:
        raise Unauthorized("Invalid phone or password", code="invalid_credentials")
    rider = (await db.execute(select(Rider).where(Rider.phone == phone))).scalar_one_or_none()
    if rider is not None:
        _check_lock(rider)
    if rider is None or not rider.is_active or not verify_password(password, rider.password_hash if rider else None):
        if rider is not None:
            await _fail(db, rider)
        raise Unauthorized("Invalid phone or password", code="invalid_credentials")
    rider.failed_logins = 0
    tokens = await issue_session(db, "rider", rider.id, ip=ip, device_label=device)
    tokens["must_change_password"] = rider.must_change_password
    return tokens


async def register_customer(db: AsyncSession, *, name: str, email: str, password: str, phone: str | None, ip: str | None) -> dict:
    err = validate_password_strength(password)
    if err:
        raise Invalid(err, code="weak_password")
    email = email.lower().strip()
    if (await db.execute(select(Customer).where(Customer.email == email))).scalar_one_or_none():
        raise Conflict("An account with this email already exists", code="email_taken")
    c = Customer(name=name.strip(), email=email, password_hash=hash_password(password), phone=normalize_phone(phone) if phone else None)
    db.add(c)
    await db.flush()
    tokens = await issue_session(db, "customer", c.id, ip=ip)
    tokens["phone_required"] = c.phone is None
    return tokens


async def login_customer(db: AsyncSession, email: str, password: str, ip: str | None, device: str | None) -> dict:
    c = (await db.execute(select(Customer).where(Customer.email == email.lower().strip()))).scalar_one_or_none()
    if c is None or not c.is_active or not verify_password(password, c.password_hash):
        raise Unauthorized("Invalid email or password", code="invalid_credentials")
    tokens = await issue_session(db, "customer", c.id, ip=ip, device_label=device)
    tokens["phone_required"] = c.phone is None
    return tokens


async def google_login(db: AsyncSession, id_token: str, ip: str | None, device: str | None) -> dict:
    info = await registry.get_google_verifier().verify(id_token)
    c = (await db.execute(select(Customer).where(Customer.google_sub == info["sub"]))).scalar_one_or_none()
    if c is None:
        c = (await db.execute(select(Customer).where(Customer.email == info["email"]))).scalar_one_or_none()
        if c is not None:
            c.google_sub = info["sub"]  # link Google to the existing email account (email is verified by Google)
        else:
            c = Customer(name=info["name"], email=info["email"], google_sub=info["sub"], avatar_url=info.get("picture"))
            db.add(c)
            await db.flush()
    if not c.is_active:
        raise Unauthorized("Account disabled", code="account_disabled")
    tokens = await issue_session(db, "customer", c.id, ip=ip, device_label=device)
    tokens["phone_required"] = c.phone is None  # app must collect the mobile number before first order
    return tokens


async def refresh_session(db: AsyncSession, refresh_token: str) -> dict:
    h = hash_token(refresh_token)
    sess = (await db.execute(select(AuthSession).where(AuthSession.token_hash == h).with_for_update())).scalar_one_or_none()
    if sess is None:
        reused = (await db.execute(select(AuthSession).where(AuthSession.prev_token_hash == h))).scalar_one_or_none()
        if reused is not None and reused.revoked_at is None:
            reused.revoked_at = now_utc()  # refresh-token reuse => assume theft, kill the session
            await db.commit()
        raise Unauthorized("Invalid refresh token", code="invalid_refresh_token")
    if sess.revoked_at is not None or sess.expires_at < now_utc():
        raise Unauthorized("Refresh token expired", code="invalid_refresh_token")
    from app.core.permissions import ROLE_MODEL

    user = await db.get(ROLE_MODEL[sess.role], sess.user_id)
    if user is None or not user.is_active:
        raise Unauthorized("Account disabled", code="account_disabled")
    new_refresh = new_token(48)
    sess.prev_token_hash, sess.token_hash, sess.last_used_at = h, hash_token(new_refresh), now_utc()
    access, expires_in = create_access_token(subject=str(sess.user_id), role=sess.role, session_id=str(sess.id))
    return {"access_token": access, "refresh_token": new_refresh, "token_type": "bearer", "expires_in": expires_in,
            "role": sess.role, "session_id": str(sess.id)}


async def revoke_session(db: AsyncSession, session_id: uuid.UUID, role: str, user_id: uuid.UUID) -> None:
    sess = await db.get(AuthSession, session_id)
    if sess is None or sess.role != role or sess.user_id != user_id:
        raise NotFound("Session not found")
    sess.revoked_at = sess.revoked_at or now_utc()
    await _drop_devices_for_session(db, sess)


async def _drop_devices_for_session(db: AsyncSession, sess: AuthSession) -> None:
    return None  # device unlink is explicit via /notifications/devices (a user may stay registered across sessions)


async def revoke_all_sessions(db: AsyncSession, role: str, user_id: uuid.UUID, except_session: uuid.UUID | None = None) -> int:
    q = update(AuthSession).where(AuthSession.role == role, AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    if except_session:
        q = q.where(AuthSession.id != except_session)
    return (await db.execute(q.values(revoked_at=now_utc()))).rowcount or 0


async def change_password(db: AsyncSession, principal, current: str, new: str) -> None:
    user = principal.user
    if not verify_password(current, user.password_hash):
        raise Unauthorized("Current password is incorrect", code="invalid_credentials")
    err = validate_password_strength(new)
    if err:
        raise Invalid(err, code="weak_password")
    user.password_hash = hash_password(new)
    if hasattr(user, "must_change_password"):
        user.must_change_password = False
    await revoke_all_sessions(db, principal.role, principal.id, except_session=principal.session_id)
    if principal.role == "admin":
        await audit(db, actor_role="admin", actor_id=user.id, action="admin.password_changed", entity_type="admin", entity_id=user.id)
