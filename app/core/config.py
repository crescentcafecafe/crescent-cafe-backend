"""Application configuration. Every secret / deployment value comes from environment variables."""
from __future__ import annotations

import base64
import json
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Accept Supabase/Render style URLs (postgres://, postgresql://) and force the asyncpg driver."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- general
    app_name: str = "Crescent Cafe API"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    public_base_url: str = "http://localhost:8000"
    frontend_url: str = ""  # website base URL the payment callback sends customers back to; empty = use QR_SITE_URL
    qr_site_url: str = "http://localhost:3000"  # QR ordering website / Android App Link host; QR URLs are {qr_site_url}/t/{token}
    cors_origins: str = ""  # comma separated list of allowed origins
    diagnostics_token: str = ""  # if empty, only an admin JWT can read diagnostics

    # --- database (Supabase Postgres)
    database_url: str = "postgresql+asyncpg://cafe:cafe@localhost:5432/cafe_dev"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_use_pgbouncer: bool = True  # disables prepared statement cache (needed for Supabase transaction pooler)
    db_ssl_require: bool = False  # set true for Supabase when the URL has no ssl parameter

    # --- auth
    jwt_secret: str = "change-me-in-production-please-use-64-random-bytes"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    table_session_token_minutes: int = 240
    argon2_time_cost: int = 3
    argon2_memory_cost: int = 65536

    # --- admin bootstrap (used ONLY by scripts/bootstrap_admin.py)
    admin_bootstrap_email: str = ""
    admin_bootstrap_password: str = ""
    admin_bootstrap_name: str = "Cafe Admin"

    # --- google sign-in for customers
    google_client_ids: str = ""  # comma separated OAuth client IDs (Android / Web)

    # --- redis (optional: pub/sub across workers + shared rate limits)
    redis_url: str = ""

    # --- payments
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    cashfree_client_id: str = ""
    cashfree_client_secret: str = ""
    cashfree_webhook_secret: str = ""  # Cashfree signs with the client secret; leave empty to reuse it
    cashfree_env: Literal["sandbox", "production"] = "sandbox"
    cashfree_api_version: str = "2023-08-01"
    cashfree_return_url: str = ""
    payment_timeout_minutes: int = 20

    # --- firebase (FCM only)
    firebase_credentials_json: str = ""  # raw JSON or base64 encoded JSON
    firebase_credentials_file: str = ""
    fcm_android_channel_new_order: str = "new_orders"
    fcm_android_channel_default: str = "general"
    # Extra Android channels used by the native app (created in the APK; ids must match NotificationChannels.kt).
    fcm_android_channel_order_updates: str = "order_updates"
    fcm_android_channel_messages: str = "messages"
    fcm_android_channel_delivery: str = "delivery_updates"
    fcm_android_channel_promotions: str = "promotions"
    fcm_android_channel_payments: str = "payments"
    fcm_dry_run: bool = False

    # --- storage (menu images)
    storage_backend: Literal["supabase", "local"] = "local"
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_storage_bucket: str = "menu-images"
    local_media_dir: str = "./media"
    max_image_bytes: int = 5 * 1024 * 1024

    # --- jobs / worker (Postgres-backed queue, no extra paid service needed)
    embedded_worker: bool = True
    job_poll_seconds: float = 2.0
    job_max_attempts: int = 8

    # --- email (Resend, https://resend.com). The key stays on the server; never expose it to the website or the Android app.
    resend_api_key: str = ""
    email_from: str = "Crescent Cafe <noreply@example.com>"  # must be an address on a domain verified in Resend
    email_reply_to: str = ""

    # --- password reset (email OTP)
    password_reset_otp_digits: int = 4
    password_reset_otp_minutes: int = 5
    password_reset_max_attempts: int = 5  # wrong tries per OTP before it is burned
    password_reset_resend_seconds: int = 60
    password_reset_max_per_hour: int = 5  # OTPs per account per hour
    password_reset_token_minutes: int = 10  # time to choose the new password after a correct OTP

    # --- delivery rules
    require_payment_before_delivery: bool = True  # a delivery cannot be completed until it is paid or cash is recorded
    arrival_notify_distance_m: int = 100  # tell the customer the rider is nearby at this distance (Swiggy/Zomato-style; phone GPS is often 5-30 m off)
    rider_qr_minutes: int = 10  # how long a rider-generated payment QR stays valid (Razorpay needs at least 2)

    # --- geocoding (turns a GPS fix into a street address, and searches addresses). Any Nominatim-compatible API works
    # (OpenStreetMap Nominatim, LocationIQ, a self-hosted instance). The public OSM server is for light use only.
    geocoder_url: str = "https://nominatim.openstreetmap.org"
    geocoder_user_agent: str = "CrescentCafe/1.0 (set GEOCODER_USER_AGENT to your contact email)"
    geocoder_api_key: str = ""  # sent as ?key= when set (LocationIQ style)
    geocoder_country_codes: str = "in"  # restrict address search to these countries (comma separated, empty = worldwide)
    geocoder_timeout_s: float = 6.0

    # --- reverse proxy
    trusted_proxy_count: int = 1  # proxies in front of the API that append to X-Forwarded-For (Render = 1). 0 = ignore the header.

    rate_limit_enabled: bool = True
    default_timezone: str = "Asia/Kolkata"

    @field_validator("database_url")
    @classmethod
    def _norm_db(cls, v: str) -> str:
        return normalize_database_url(v)

    @property
    def frontend_base(self) -> str:
        return (self.frontend_url or self.qr_site_url).rstrip("/")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def google_client_id_list(self) -> list[str]:
        return [o.strip() for o in self.google_client_ids.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def production_problems(self) -> list[str]:
        problems: list[str] = []
        if self.is_production:
            if self.jwt_secret.startswith("change-me") or len(self.jwt_secret) < 32:
                problems.append("JWT_SECRET must be a long random value in production")
            if self.storage_backend == "local":
                problems.append("STORAGE_BACKEND=local is not durable on Render; use supabase")
            if not self.cors_origin_list:
                problems.append("CORS_ORIGINS is empty; the QR website will be blocked by browsers")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


def firebase_credentials_dict(s: Settings) -> dict | None:
    raw = s.firebase_credentials_json.strip()
    if not raw:
        return None
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode()
    return json.loads(raw)
