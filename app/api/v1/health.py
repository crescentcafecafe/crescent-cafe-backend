from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Forbidden
from app.core.permissions import optional_principal
from app.db.session import get_db, get_engine
from app.integrations import registry
from app.models.system import Job

router = APIRouter(tags=["health"])


@router.get("/health", include_in_schema=True)
async def health():
    """Liveness only: no infrastructure detail is exposed."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(response: Response):
    """Readiness: can we reach the database?"""
    try:
        async with get_engine().connect() as c:
            await c.execute(text("select 1"))
        return {"status": "ready"}
    except Exception:
        response.status_code = 503
        return {"status": "unavailable"}


@router.get("/api/v1/diagnostics")
async def diagnostics(x_diagnostics_token: str | None = Header(default=None), p=Depends(optional_principal), db: AsyncSession = Depends(get_db)):
    """Protected: admin JWT or X-Diagnostics-Token (DIAGNOSTICS_TOKEN env)."""
    s = get_settings()
    ok = (p is not None and p.role == "admin") or (s.diagnostics_token and x_diagnostics_token and secrets.compare_digest(x_diagnostics_token, s.diagnostics_token))
    if not ok:
        raise Forbidden("Diagnostics access denied", code="forbidden")
    jobs = dict((await db.execute(select(Job.status, func.count()).group_by(Job.status))).all())
    return {"environment": s.environment, "database": "ok", "jobs": jobs, "redis_configured": bool(s.redis_url), "embedded_worker": s.embedded_worker,
            "firebase_configured": bool(s.firebase_credentials_json or s.firebase_credentials_file),
            "razorpay_configured": registry.get_gateway("razorpay").is_configured(), "cashfree_configured": registry.get_gateway("cashfree").is_configured(),
            "storage_backend": s.storage_backend, "production_problems": s.production_problems()}
