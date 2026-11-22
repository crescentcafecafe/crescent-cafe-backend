"""E-mail queue + delivery. Business code calls queue_email(); the worker renders and sends through Resend.
OTP e-mails are the exception: the code must never be written to the job table, so they are sent from memory (see password_reset)."""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import registry
from app.integrations.email.resend import EmailError
from app.services import jobs

log = logging.getLogger(__name__)


async def queue_email(db: AsyncSession, template: str, **ids: uuid.UUID | str) -> None:
    """ids are references only (customer_id, order_id); the handler re-reads the data so jobs hold no personal data or secrets."""
    payload = {"template": template, **{k: str(v) for k, v in ids.items()}}
    key = f"email:{template}:" + ":".join(str(v) for v in ids.values())
    await jobs.enqueue(db, "send_email", payload, key=key)


async def deliver(to: str, subject: str, html: str, text: str | None, *, key: str | None = None, raise_on_error: bool = True) -> bool:
    """Send one e-mail. Returns True when handed to Resend. With raise_on_error=False failures are logged, never raised."""
    sender = registry.get_email_sender()
    if not sender.is_configured():
        log.warning("e-mail not sent (RESEND_API_KEY is empty): %s", subject)
        return False
    try:
        await sender.send(to=to, subject=subject, html=html, text=text, idempotency_key=key)
        return True
    except EmailError as err:
        log.warning("e-mail failed (%s): %s", subject, err)
        if raise_on_error:
            raise
        return False
