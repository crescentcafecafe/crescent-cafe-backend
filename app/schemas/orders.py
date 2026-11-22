from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class AddonIn(BaseModel):
    addon_id: uuid.UUID
    quantity: int = Field(default=1, ge=1, le=20)


class OptionIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    value: str = Field(min_length=1, max_length=120)


class OrderItemIn(BaseModel):
    menu_item_id: uuid.UUID
    quantity: int = Field(ge=1, le=50)
    variant_id: uuid.UUID | None = None
    addons: list[AddonIn] = Field(default_factory=list, max_length=20)
    options: list[OptionIn] = Field(default_factory=list, max_length=10)
    notes: str | None = Field(default=None, max_length=300)


class AddressIn(BaseModel):
    line1: str = Field(min_length=3, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    landmark: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    postal_code: str = Field(min_length=3, max_length=12)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=100_000)  # GPS accuracy radius when the pin came from the phone
    location_source: str = Field(default="manual", pattern=r"^(gps|pin|manual)$")  # gps = device fix, pin = searched/placed, manual = typed only

    @model_validator(mode="after")
    def _coordinates_are_a_pair(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be sent together")
        if self.latitude is None:
            self.location_source, self.accuracy_m = "manual", None
        elif self.latitude == 0 and self.longitude == 0:
            raise ValueError("That location is not valid")  # (0, 0) is what broken GPS reports
        return self


class QuoteRequest(BaseModel):
    order_type: Literal["dine_in", "pickup", "delivery", "walk_in"]
    items: list[OrderItemIn] = Field(min_length=1, max_length=40)
    coupon_code: str | None = Field(default=None, max_length=40)
    delivery_address: AddressIn | None = None
    customer_phone: str | None = Field(default=None, max_length=20)  # lets guests check per-customer coupon limits


class OrderCreate(QuoteRequest):
    payment_method: Literal["cash", "razorpay", "cashfree"]
    source: Literal["customer_app", "qr_web"] = "customer_app"
    table_session_token: str | None = Field(default=None, max_length=2000)
    table_id: uuid.UUID | None = None  # admin walk-in seated at a table
    special_instructions: str | None = Field(default=None, max_length=500)
    customer_name: str | None = Field(default=None, max_length=120)
    customer_email: str | None = Field(default=None, max_length=255)

    @field_validator("coupon_code")
    @classmethod
    def _up(cls, v):
        return v.strip().upper() if v else v


class CancelRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class AdminDecision(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


class AdminStatusChange(BaseModel):
    status: Literal["preparing", "ready", "completed"]


class InitiatePayment(BaseModel):
    gateway: Literal["razorpay", "cashfree"]


class RazorpayVerify(BaseModel):
    razorpay_order_id: str = Field(max_length=80)
    razorpay_payment_id: str = Field(max_length=80)
    razorpay_signature: str = Field(max_length=200)
