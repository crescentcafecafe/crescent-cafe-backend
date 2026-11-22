"""Verify Google Sign-In ID tokens (customer login with Gmail)."""
from __future__ import annotations

import asyncio
from typing import Protocol

from app.core.config import get_settings
from app.core.errors import Unauthorized


class GoogleVerifier(Protocol):
    async def verify(self, id_token: str) -> dict: ...


class RealGoogleVerifier:
    async def verify(self, id_token: str) -> dict:
        from google.auth.transport import requests as greq
        from google.oauth2 import id_token as gid

        allowed = get_settings().google_client_id_list
        if not allowed:
            raise Unauthorized("Google sign-in is not configured", code="google_not_configured")

        def _v() -> dict:
            return gid.verify_oauth2_token(id_token, greq.Request(), audience=None)

        try:
            info = await asyncio.to_thread(_v)
        except Exception:
            raise Unauthorized("Invalid Google token", code="invalid_google_token")
        if info.get("aud") not in allowed or info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            raise Unauthorized("Google token audience mismatch", code="invalid_google_token")
        if not info.get("email_verified"):
            raise Unauthorized("Google email is not verified", code="email_unverified")
        return {"sub": info["sub"], "email": info["email"].lower(), "name": info.get("name") or info["email"].split("@")[0],
                "picture": info.get("picture")}
