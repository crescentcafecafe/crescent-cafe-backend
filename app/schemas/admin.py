from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    sort_order: int = 0
    is_active: bool = True


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class ReorderIn(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)  # in desired display order


class VariantIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    price_paise: int = Field(ge=0, le=10_000_000)
    sort_order: int = 0
    is_available: bool = True


class AddonIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    price_paise: int = Field(default=0, ge=0, le=10_000_000)
    max_quantity: int = Field(default=1, ge=1, le=20)
    sort_order: int = 0
    is_available: bool = True


class ItemIn(BaseModel):
    category_id: uuid.UUID
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    price_paise: int = Field(ge=0, le=10_000_000)
    prep_minutes: int | None = Field(default=None, ge=0, le=600)
    prep_notes: str | None = Field(default=None, max_length=500)
    is_veg: bool | None = None
    tax_setting_id: uuid.UUID | None = None
    sort_order: int = 0
    is_available: bool = True
    variants: list[VariantIn] = Field(default_factory=list, max_length=20)
    addons: list[AddonIn] = Field(default_factory=list, max_length=30)


class ItemPatch(BaseModel):
    category_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    price_paise: int | None = Field(default=None, ge=0, le=10_000_000)
    prep_minutes: int | None = Field(default=None, ge=0, le=600)
    prep_notes: str | None = None
    is_veg: bool | None = None
    tax_setting_id: uuid.UUID | None = None
    sort_order: int | None = None
    is_available: bool | None = None


class AvailabilityIn(BaseModel):
    is_available: bool


class VariantPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    price_paise: int | None = Field(default=None, ge=0, le=10_000_000)
    sort_order: int | None = None
    is_available: bool | None = None
    is_archived: bool | None = None


class AddonPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    price_paise: int | None = Field(default=None, ge=0, le=10_000_000)
    max_quantity: int | None = Field(default=None, ge=1, le=20)
    sort_order: int | None = None
    is_available: bool | None = None
    is_archived: bool | None = None


class TableIn(BaseModel):
    number: str = Field(min_length=1, max_length=20)
    label: str | None = Field(default=None, max_length=80)
    capacity: int | None = Field(default=None, ge=1, le=200)
    area: str | None = Field(default=None, max_length=80)
    notes: str | None = Field(default=None, max_length=500)
    pin: str | None = Field(default=None, min_length=3, max_length=12)
    is_active: bool = True


class TablePatch(BaseModel):
    number: str | None = Field(default=None, min_length=1, max_length=20)
    label: str | None = None
    capacity: int | None = Field(default=None, ge=1, le=200)
    area: str | None = None
    notes: str | None = None
    pin: str | None = Field(default=None, max_length=12)  # empty string removes the PIN
    is_active: bool | None = None
    is_archived: bool | None = None


class BulkQrIn(BaseModel):
    table_ids: list[uuid.UUID] | None = None  # null => all active tables
    confirm_rotate: bool = Field(default=False, description="Must be true: bulk export rotates tokens (old QR codes stop working)")


class RevokeIn(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


class CloseSessionIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class TransferIn(BaseModel):
    new_table_id: uuid.UUID


class RiderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=6, max_length=20)
    email: EmailStr | None = None
    vehicle_info: str | None = Field(default=None, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class RiderPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = Field(default=None, min_length=6, max_length=20)
    email: EmailStr | None = None
    vehicle_info: str | None = None
    is_active: bool | None = None


class ResetPasswordIn(BaseModel):
    new_password: str = Field(min_length=1, max_length=200)


class AvailabilityToggle(BaseModel):
    available: bool


class AssignIn(BaseModel):
    rider_id: uuid.UUID


class RejectAssignment(BaseModel):
    reason: str = Field(min_length=2, max_length=300)


class CashConfirmIn(BaseModel):
    amount_received_paise: int = Field(gt=0, le=100_000_000)


class CashReverseIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class RefundIn(BaseModel):
    amount_paise: int | None = Field(default=None, gt=0)  # None => full remaining balance
    reason: str = Field(min_length=3, max_length=300)


class GatewayConfigIn(BaseModel):
    razorpay_enabled: bool | None = None
    cashfree_enabled: bool | None = None
    cash_enabled: bool | None = None
    cash_on_delivery_enabled: bool | None = None


class OfferIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    discount_type: Literal["percent", "fixed"]
    value: int = Field(gt=0, description="percent in basis points (1000 = 10%) or paise for fixed")
    min_order_paise: int = Field(default=0, ge=0)
    max_discount_paise: int | None = Field(default=None, gt=0)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    total_redemption_limit: int | None = Field(default=None, gt=0)
    per_customer_limit: int | None = Field(default=None, gt=0)
    item_ids: list[uuid.UUID] = Field(default_factory=list)
    category_ids: list[uuid.UUID] = Field(default_factory=list)
    order_types: list[Literal["dine_in", "pickup", "delivery", "walk_in"]] = Field(default_factory=list)
    first_order_only: bool = False
    stackable: bool = False
    is_listed: bool = True
    branch_specific: bool = False
    coupon_code: str | None = Field(default=None, min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("coupon_code")
    @classmethod
    def _up(cls, v):
        return v.upper() if v else v


class OfferPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = None
    value: int | None = Field(default=None, gt=0)
    min_order_paise: int | None = Field(default=None, ge=0)
    max_discount_paise: int | None = Field(default=None, gt=0)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    total_redemption_limit: int | None = Field(default=None, gt=0)
    per_customer_limit: int | None = Field(default=None, gt=0)
    item_ids: list[uuid.UUID] | None = None
    category_ids: list[uuid.UUID] | None = None
    order_types: list[Literal["dine_in", "pickup", "delivery", "walk_in"]] | None = None
    first_order_only: bool | None = None
    stackable: bool | None = None
    is_listed: bool | None = None
    status: Literal["active", "paused", "expired"] | None = None


class CouponIn(BaseModel):
    code: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("code")
    @classmethod
    def _up(cls, v):
        return v.upper()


class CafeSettingsPatch(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    gstin: str | None = Field(default=None, max_length=20)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    timezone: str | None = None
    accepting_orders: bool | None = None
    temporarily_closed: bool | None = None
    temporary_closure_message: str | None = None
    dine_in_enabled: bool | None = None
    pickup_enabled: bool | None = None
    delivery_enabled: bool | None = None
    guest_checkout_enabled: bool | None = None
    opening_hours: dict | None = None
    closures: list[dict] | None = None
    enforce_opening_hours: bool | None = None
    prices_include_tax: bool | None = None
    rider_single_active_assignment: bool | None = None
    otp_ttl_minutes: int | None = Field(default=None, ge=5, le=1440)
    otp_max_attempts: int | None = Field(default=None, ge=3, le=10)
    location_min_interval_seconds: int | None = Field(default=None, ge=1, le=300)
    location_update_interval_seconds: int | None = Field(default=None, ge=3, le=300)
    location_retention_days: int | None = Field(default=None, ge=1, le=365)
    location_stale_after_seconds: int | None = Field(default=None, ge=10, le=3600)
    coupon_reservation_minutes: int | None = Field(default=None, ge=5, le=120)
    notify_admin_new_orders: bool | None = None
    notify_customers_promotions: bool | None = None
    invoice_prefix: str | None = Field(default=None, max_length=16)
    invoice_footer: str | None = None


class DeliverySettingsPatch(BaseModel):
    base_charge_paise: int | None = Field(default=None, ge=0)
    free_delivery_above_paise: int | None = Field(default=None, ge=0)
    min_order_paise: int | None = Field(default=None, ge=0)
    allowed_postal_codes: list[str] | None = None
    tax_rate_bps: int | None = Field(default=None, ge=0, le=10000)


class TaxIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    rate_bps: int = Field(ge=0, le=10000)
    is_default: bool = False
    is_active: bool = True


class TaxPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    rate_bps: int | None = Field(default=None, ge=0, le=10000)
    is_default: bool | None = None
    is_active: bool | None = None


class BroadcastIn(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=300)


class AdminStartChat(BaseModel):
    customer_id: uuid.UUID | None = None
    rider_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None


class ChatOpen(BaseModel):
    order_id: uuid.UUID | None = None
    subject: str | None = Field(default=None, max_length=200)


class ChatSend(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    client_msg_id: str | None = Field(default=None, max_length=64)
    internal: bool = False


class ChatRead(BaseModel):
    up_to_message_id: int | None = None


class DeviceIn(BaseModel):
    install_id: str = Field(min_length=6, max_length=100)
    fcm_token: str = Field(min_length=20, max_length=1024)
    platform: Literal["android", "ios", "web"] = "android"
    device_model: str | None = Field(default=None, max_length=120)
    os_version: str | None = Field(default=None, max_length=40)
    app_version: str | None = Field(default=None, max_length=40)
    locale: str | None = Field(default=None, max_length=20)
    notifications_enabled: bool = True


class DeviceTokenRefresh(BaseModel):
    fcm_token: str = Field(min_length=20, max_length=1024)
    notifications_enabled: bool | None = None


class LocationIn(BaseModel):
    assignment_id: uuid.UUID
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    recorded_at: datetime | None = None


class OtpVerifyIn(BaseModel):
    otp: str = Field(pattern=r"^\d{6}$")


class CollectCashIn(BaseModel):
    amount_received_paise: int = Field(gt=0, le=10_000_000)


class CashHandoverIn(BaseModel):
    amount_paise: int = Field(gt=0, le=100_000_000)
    note: str | None = Field(default=None, max_length=300)


class CustomerAddressIn(BaseModel):
    label: str = Field(default="home", pattern=r"^(home|work|other)$")
    line1: str = Field(min_length=3, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    landmark: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=80)
    postal_code: str = Field(min_length=3, max_length=12)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_source: str = Field(default="manual", pattern=r"^(gps|pin|manual)$")
    is_default: bool = False

    @model_validator(mode="after")
    def _coordinates_are_a_pair(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be sent together")
        if self.latitude is None:
            self.location_source = "manual"
        elif self.latitude == 0 and self.longitude == 0:
            raise ValueError("That location is not valid")
        return self


class CustomerAddressPatch(BaseModel):
    label: str | None = Field(default=None, pattern=r"^(home|work|other)$")
    line1: str | None = Field(default=None, min_length=3, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    landmark: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, min_length=3, max_length=12)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_source: str | None = Field(default=None, pattern=r"^(gps|pin|manual)$")
    is_default: bool | None = None
