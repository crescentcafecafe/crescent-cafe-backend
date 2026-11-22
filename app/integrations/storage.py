"""Menu image storage: Supabase Storage (production) or local disk (development)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

import httpx

from app.core.config import get_settings
from app.core.errors import AppError


class Storage(Protocol):
    async def upload(self, path: str, data: bytes, content_type: str) -> str: ...
    async def delete(self, path: str) -> None: ...


class SupabaseStorage:
    def __init__(self) -> None:
        s = get_settings()
        if not (s.supabase_url and s.supabase_service_role_key):
            raise AppError("Supabase storage is not configured", code="storage_not_configured", status_code=503)
        self.base = s.supabase_url.rstrip("/") + "/storage/v1"
        self.bucket = s.supabase_storage_bucket
        self.h = {"Authorization": f"Bearer {s.supabase_service_role_key}", "apikey": s.supabase_service_role_key}

    async def upload(self, path: str, data: bytes, content_type: str) -> str:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base}/object/{self.bucket}/{path}", content=data,
                             headers={**self.h, "Content-Type": content_type, "x-upsert": "true", "cache-control": "max-age=31536000"})
        if r.status_code >= 400:
            raise AppError(f"Image upload failed ({r.status_code})", code="storage_error", status_code=502)
        return f"{self.base}/object/public/{self.bucket}/{path}"  # bucket must be PUBLIC for customer menu images

    async def delete(self, path: str) -> None:
        async with httpx.AsyncClient(timeout=20) as c:
            await c.request("DELETE", f"{self.base}/object/{self.bucket}", headers=self.h, json={"prefixes": [path]})


class LocalStorage:
    def __init__(self) -> None:
        s = get_settings()
        self.root = Path(s.local_media_dir)
        self.base = s.public_base_url.rstrip("/") + "/media"

    async def upload(self, path: str, data: bytes, content_type: str) -> str:
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return f"{self.base}/{path}"

    async def delete(self, path: str) -> None:
        try:
            os.remove(self.root / path)
        except FileNotFoundError:
            pass
