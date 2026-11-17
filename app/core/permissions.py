"""Authentication + authorization dependencies. Permissions are always enforced server-side."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, Unauthorized
from app.core.security import decode_token, now_utc
from app.db.session import get_db
from app.models.core import AdminUser, AuthSession, Customer, Rider

bearer = HTTPBearer(auto_error=False)
ROLE_MODEL = {"admin": AdminUser, "customer": Customer, "rider": Rider}


@dataclass
class Principal:
    role: str
    id: uuid.UUID
    session_id: uuid.UUID
    user: Any

    @property
    def branch_id(self):
        return getattr(self.user, "branch_id", None)


async def principal_from_token(db: AsyncSession, token: str) -> Principal:
    try:
        claims = decode_token(token, "access")
        role, uid, sid = claims["role"], uuid.UUID(claims["sub"]), uuid.UUID(claims["sid"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise Unauthorized("Invalid or expired token", code="invalid_token")
    model = ROLE_MODEL.get(role)
    if model is None:
        raise Unauthorized("Invalid token", code="invalid_token")
    sess = await db.get(AuthSession, sid)
    if sess is None or sess.revoked_at is not None or sess.expires_at < now_utc() or sess.user_id != uid or sess.role != role:
        raise Unauthorized("Session expired or revoked", code="session_revoked")
    user = await db.get(model, uid)
    if user is None or not user.is_active:
        raise Unauthorized("Account disabled", code="account_disabled")
    return Principal(role=role, id=uid, session_id=sid, user=user)


async def current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: AsyncSession = Depends(get_db)
) -> Principal:
    if creds is None:
        raise Unauthorized("Authentication required", code="auth_required")
    return await principal_from_token(db, creds.credentials)


async def optional_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: AsyncSession = Depends(get_db)
) -> Principal | None:
    if creds is None:
        return None
    return await principal_from_token(db, creds.credentials)


def require_role(role: str):
    async def dep(p: Principal = Depends(current_principal)) -> Principal:
        if p.role != role:
            raise Forbidden(f"{role} access required", code="wrong_role")
        return p

    return dep


current_admin = require_role("admin")
current_customer = require_role("customer")
current_rider = require_role("rider")


async def optional_customer(p: Principal | None = Depends(optional_principal)) -> Principal | None:
    if p is None:
        return None
    if p.role != "customer":
        raise Forbidden("Customer access required", code="wrong_role")
    return p


def guest_token_header(x_guest_token: str | None = Header(default=None)) -> str | None:
    return x_guest_token


async def get_admin_user(db: AsyncSession) -> AdminUser | None:
    return (await db.execute(select(AdminUser).where(AdminUser.is_primary.is_(True)))).scalar_one_or_none()
