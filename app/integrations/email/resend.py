"""Resend e-mail API (https://resend.com/docs/api-reference/emails/send-email). Server-side only."""
from __future__ import annotations

import httpx

from app.core.config import get_settings

API = "https://api.resend.com/emails"


class EmailError(Exception):
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class ResendEmailSender:
    name = "resend"

    def __init__(self) -> None:
        self.s = get_settings()

    def is_configured(self) -> bool:
        return bool(self.s.resend_api_key)

    async def send(self, *, to: str, subject: str, html: str, text: str | None = None, idempotency_key: str | None = None) -> str | None:
        """Returns the Resend message id. Raises EmailError (transient=True when a retry may help)."""
        if not self.is_configured():
            raise EmailError("Resend is not configured (RESEND_API_KEY is empty)")
        payload: dict = {"from": self.s.email_from, "to": [to], "subject": subject, "html": html}
        if text:
            payload["text"] = text
        if self.s.email_reply_to:
            payload["reply_to"] = self.s.email_reply_to
        headers = {"Authorization": f"Bearer {self.s.resend_api_key}", "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key[:256]
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(API, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise EmailError(f"Resend network error: {e}", transient=True)
        if r.status_code == 429 or r.status_code >= 500:
            raise EmailError(f"Resend {r.status_code}", transient=True)
        if r.status_code >= 400:
            try:
                detail = r.json().get("message", r.text)
            except Exception:
                detail = r.text
            raise EmailError(f"Resend rejected the email: {detail}")
        try:
            return r.json().get("id")
        except Exception:
            return None
