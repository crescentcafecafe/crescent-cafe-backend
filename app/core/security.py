"""Password hashing (Argon2id), JWT access tokens, opaque refresh/guest/QR tokens, OTP helpers."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings

_ph: PasswordHasher | None = None
_dummy: str | None = None


def _hasher() -> PasswordHasher:
    global _ph
    if _ph is None:
        s = get_settings()
        _ph = PasswordHasher(time_cost=s.argon2_time_cost, memory_cost=s.argon2_memory_cost)
    return _ph


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def verify_password(password: str, hashed: str | None) -> bool:
    global _dummy
    if not hashed:
        # burn comparable time so account existence is not leaked through response timing
        if _dummy is None:
            _dummy = _hasher().hash("dummy-password-for-timing")
        try:
            _hasher().verify(_dummy, password)
        except Exception:
            pass
        return False
    try:
        return _hasher().verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def validate_password_strength(pw: str) -> str | None:
    if len(pw) < 10:
        return "Password must be at least 10 characters"
    if pw.lower() == pw or pw.upper() == pw or not any(c.isdigit() for c in pw):
        return "Password must mix upper/lower case letters and include a digit"
    return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------- opaque tokens
def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """SHA-256 hex digest. Tokens are high entropy so a fast hash is appropriate."""
    return hashlib.sha256(token.encode()).hexdigest()


def consteq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def hmac_sha256_hex(secret: str, msg: bytes | str) -> str:
    if isinstance(msg, str):
        msg = msg.encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


# ---------- OTP (delivery)
def new_numeric_otp(digits: int = 6) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(digits))


def hash_otp(otp: str, salt: str) -> str:
    """HMAC keyed with the server secret + per-OTP salt (6 digits are brute-forceable offline, so the
    server secret, which is not stored in the DB, is what protects a leaked table; online attempts are limited too)."""
    return hmac_sha256_hex(get_settings().jwt_secret + salt, otp)


# ---------- JWT
def create_access_token(*, subject: str, role: str, session_id: str) -> tuple[str, int]:
    s = get_settings()
    exp = now_utc() + timedelta(minutes=s.access_token_minutes)
    payload = {"sub": subject, "role": role, "sid": session_id, "typ": "access", "jti": uuid.uuid4().hex, "exp": exp, "iat": now_utc()}
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm), s.access_token_minutes * 60


def create_scoped_token(payload: dict, minutes: int) -> str:
    s = get_settings()
    body = {**payload, "exp": now_utc() + timedelta(minutes=minutes), "iat": now_utc()}
    return jwt.encode(body, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_token(token: str, expected_typ: str) -> dict:
    s = get_settings()
    data = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm], options={"require": ["exp", "sub"]})
    if data.get("typ") != expected_typ:
        raise jwt.InvalidTokenError("wrong token type")
    return data
