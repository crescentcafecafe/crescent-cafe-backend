"""Single place that hands out integration clients; tests swap them with fakes via override()."""
from __future__ import annotations

from app.core.config import get_settings
from app.core.errors import AppError
from app.integrations.base import PaymentGateway

_overrides: dict[str, object] = {}
_cache: dict[str, object] = {}


def override(name: str, obj: object | None) -> None:
    if obj is None:
        _overrides.pop(name, None)
    else:
        _overrides[name] = obj


def _get(name: str, factory):
    if name in _overrides:
        return _overrides[name]
    if name not in _cache:
        _cache[name] = factory()
    return _cache[name]


def get_gateway(name: str) -> PaymentGateway:
    if name == "razorpay":
        from app.integrations.razorpay.client import RazorpayGateway
        return _get("gw:razorpay", RazorpayGateway)  # type: ignore[return-value]
    if name == "cashfree":
        from app.integrations.cashfree.client import CashfreeGateway
        return _get("gw:cashfree", CashfreeGateway)  # type: ignore[return-value]
    raise AppError(f"Unknown payment gateway {name}", code="unknown_gateway")


def get_push_client():
    from app.integrations.firebase.client import FirebasePushClient
    return _get("push", FirebasePushClient)


def get_google_verifier():
    from app.integrations.google_auth import RealGoogleVerifier
    return _get("google", RealGoogleVerifier)


def get_email_sender():
    from app.integrations.email.resend import ResendEmailSender
    return _get("email", ResendEmailSender)


def get_storage():
    if "storage" in _overrides:
        return _overrides["storage"]
    from app.integrations.storage import LocalStorage, SupabaseStorage
    return SupabaseStorage() if get_settings().storage_backend == "supabase" else LocalStorage()
