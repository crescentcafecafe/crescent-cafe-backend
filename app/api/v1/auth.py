from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Principal, current_principal
from app.core.ratelimit import client_ip, limit, limit_key
from app.db.session import get_db
from app.models.core import AuthSession, Device
from app.schemas.auth import (AdminLogin, ChangePassword, CustomerLogin, CustomerRegister, GoogleLogin, RefreshRequest, RiderLogin, TokenResponse)
from app.schemas.common import OK
from app.services import auth as svc

router = APIRouter(prefix="/auth", tags=["auth"])


class LogoutIn(BaseModel):
    install_id: str | None = None  # also unlink this device's push tokens


@router.post("/admin/login", response_model=TokenResponse, dependencies=[Depends(limit("login", 10, 60))])
async def admin_login(body: AdminLogin, request: Request, db: AsyncSession = Depends(get_db)):
    await limit_key("login-email", body.email.lower(), 8, 300)
    return await svc.login_admin(db, body.email, body.password, client_ip(request), body.device_label)


@router.post("/rider/login", response_model=TokenResponse, dependencies=[Depends(limit("login", 10, 60))])
async def rider_login(body: RiderLogin, request: Request, db: AsyncSession = Depends(get_db)):
    await limit_key("login-phone", body.phone, 8, 300)
    return await svc.login_rider(db, body.phone, body.password, client_ip(request), body.device_label)


@router.post("/customer/register", response_model=TokenResponse, status_code=201, dependencies=[Depends(limit("register", 10, 3600))])
async def customer_register(body: CustomerRegister, request: Request, db: AsyncSession = Depends(get_db)):
    return await svc.register_customer(db, name=body.name, email=body.email, password=body.password, phone=body.phone, ip=client_ip(request))


@router.post("/customer/login", response_model=TokenResponse, dependencies=[Depends(limit("login", 10, 60))])
async def customer_login(body: CustomerLogin, request: Request, db: AsyncSession = Depends(get_db)):
    await limit_key("login-email", body.email.lower(), 8, 300)
    return await svc.login_customer(db, body.email, body.password, client_ip(request), body.device_label)


@router.post("/customer/google", response_model=TokenResponse, dependencies=[Depends(limit("google", 20, 60))])
async def customer_google(body: GoogleLogin, request: Request, db: AsyncSession = Depends(get_db)):
    """Sign in with Google (Gmail). The response's phone_required=true means the app must collect the mobile number next."""
    return await svc.google_login(db, body.id_token, client_ip(request), body.device_label)


@router.post("/refresh", response_model=TokenResponse, dependencies=[Depends(limit("refresh", 60, 60))])
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    return await svc.refresh_session(db, body.refresh_token)


@router.post("/logout", response_model=OK)
async def logout(body: LogoutIn | None = None, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    await svc.revoke_session(db, p.session_id, p.role, p.id)
    if body and body.install_id:
        from app.api.v1.notifications import unlink_device

        await unlink_device(db, p, body.install_id)
    return OK()


@router.post("/logout-all", response_model=OK)
async def logout_all(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    await svc.revoke_all_sessions(db, p.role, p.id)
    return OK()


@router.post("/change-password", response_model=OK, dependencies=[Depends(limit("chpw", 10, 600))])
async def change_password(body: ChangePassword, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    await svc.change_password(db, p, body.current_password, body.new_password)
    return OK()


@router.get("/sessions")
async def sessions(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AuthSession).where(AuthSession.role == p.role, AuthSession.user_id == p.id, AuthSession.revoked_at.is_(None))
                             .order_by(AuthSession.created_at.desc()))).scalars().all()
    return [{"id": s.id, "device_label": s.device_label, "ip": s.ip, "created_at": s.created_at, "last_used_at": s.last_used_at,
             "expires_at": s.expires_at, "current": s.id == p.session_id} for s in rows]


@router.delete("/sessions/{session_id}", response_model=OK)
async def revoke(session_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    await svc.revoke_session(db, session_id, p.role, p.id)
    return OK()
