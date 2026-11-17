"""FCM via Firebase Admin SDK. Credentials come only from env vars (never shipped in an APK)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from app.core.config import firebase_credentials_dict, get_settings

log = logging.getLogger(__name__)


@dataclass
class PushResult:
    ok: bool
    message_id: str | None = None
    invalid_token: bool = False
    permanent: bool = False  # do not retry
    error: str | None = None


class PushClient(Protocol):
    async def send(self, tokens: list[str], *, title: str | None, body: str | None, data: dict, high_priority: bool,
                   data_only: bool, channel_id: str) -> list[PushResult]: ...


class FirebasePushClient:
    def __init__(self) -> None:
        self._app = None
        self._ready: bool | None = None

    def _init(self) -> bool:
        if self._ready is not None:
            return self._ready
        s = get_settings()
        try:
            import firebase_admin
            from firebase_admin import credentials

            info = firebase_credentials_dict(s)
            if info:
                cred = credentials.Certificate(info)
            elif s.firebase_credentials_file:
                cred = credentials.Certificate(s.firebase_credentials_file)
            else:
                self._ready = False
                return False
            self._app = firebase_admin.initialize_app(cred, name="crescent-cafe")
            self._ready = True
        except Exception:
            log.exception("Firebase initialisation failed")
            self._ready = False
        return self._ready

    def _send_sync(self, tokens, title, body, data, high_priority, data_only, channel_id) -> list[PushResult]:
        from firebase_admin import messaging

        data = {k: str(v) for k, v in (data or {}).items()}
        msgs = []
        for t in tokens:
            android = messaging.AndroidConfig(
                priority="high" if high_priority else "normal",
                notification=None if data_only else messaging.AndroidNotification(channel_id=channel_id, sound="default"))
            notif = None if data_only else messaging.Notification(title=title, body=body)
            msgs.append(messaging.Message(token=t, data=data, notification=notif, android=android))
        resp = messaging.send_each(msgs, dry_run=get_settings().fcm_dry_run, app=self._app)
        out: list[PushResult] = []
        for r in resp.responses:
            if r.success:
                out.append(PushResult(ok=True, message_id=r.message_id))
                continue
            exc = r.exception
            invalid = isinstance(exc, (messaging.UnregisteredError, messaging.SenderIdMismatchError)) or (
                "registration token" in str(exc).lower() and "not a valid" in str(exc).lower())
            out.append(PushResult(ok=False, invalid_token=invalid, permanent=invalid, error=f"{type(exc).__name__}: {exc}"))
        return out

    async def send(self, tokens, *, title, body, data, high_priority, data_only, channel_id) -> list[PushResult]:
        if not tokens:
            return []
        if not self._init():
            return [PushResult(ok=False, permanent=True, error="firebase_not_configured") for _ in tokens]
        results: list[PushResult] = []
        for i in range(0, len(tokens), 500):
            chunk = tokens[i:i + 500]
            results += await asyncio.to_thread(self._send_sync, chunk, title, body, data, high_priority, data_only, channel_id)
        return results
