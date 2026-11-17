"""Common payment-gateway interface. Razorpay and Cashfree implement it; services only talk to this."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Protocol


def paise_to_rupees(p: int) -> Decimal:
    return (Decimal(p) / Decimal(100)).quantize(Decimal("0.01"))


def rupees_to_paise(v) -> int:
    return int((Decimal(str(v)) * 100).quantize(Decimal("1")))


class GatewayError(Exception):
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass
class GatewayOrder:
    gateway_order_id: str
    client_payload: dict  # only what the mobile/web SDK needs; never secrets
    session_id: str | None = None


@dataclass
class GatewayPaymentInfo:
    status: str  # paid | failed | pending | expired
    gateway_payment_id: str | None = None
    amount_paise: int | None = None
    currency: str | None = None
    failure_reason: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class RefundInfo:
    status: str  # succeeded | pending | failed
    gateway_refund_id: str | None = None
    amount_paise: int | None = None
    failure_reason: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class WebhookEvent:
    event_id: str
    event_type: str
    gateway_order_id: str | None = None
    payment: GatewayPaymentInfo | None = None
    refund: RefundInfo | None = None
    refund_ref: str | None = None  # our refund id when the gateway echoes it


class PaymentGateway(Protocol):
    name: str

    def is_configured(self) -> bool: ...
    async def create_order(self, *, reference: str, amount_paise: int, currency: str, customer: dict, description: str) -> GatewayOrder: ...
    def verify_checkout_signature(self, **fields: str) -> bool: ...
    def verify_webhook_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool: ...
    def parse_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> WebhookEvent: ...
    async def fetch_payment_status(self, gateway_order_id: str) -> GatewayPaymentInfo: ...
    async def create_refund(self, *, gateway_order_id: str, gateway_payment_id: str | None, amount_paise: int, refund_ref: str, reason: str) -> RefundInfo: ...
    async def fetch_refund(self, *, gateway_order_id: str, gateway_payment_id: str | None, refund_ref: str, gateway_refund_id: str | None) -> RefundInfo: ...
