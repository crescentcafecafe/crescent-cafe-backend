"""Cashfree Payment Gateway integration (PG API v2023-08-01) via REST."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Mapping

import httpx

from app.core.config import get_settings
from app.integrations.base import (GatewayError, GatewayOrder, GatewayPaymentInfo, RefundInfo, WebhookEvent,
                                   paise_to_rupees, rupees_to_paise)


class CashfreeGateway:
    name = "cashfree"

    def __init__(self) -> None:
        self.s = get_settings()
        self.base = "https://api.cashfree.com/pg" if self.s.cashfree_env == "production" else "https://sandbox.cashfree.com/pg"

    def is_configured(self) -> bool:
        return bool(self.s.cashfree_client_id and self.s.cashfree_client_secret)

    def _headers(self) -> dict:
        return {"x-client-id": self.s.cashfree_client_id, "x-client-secret": self.s.cashfree_client_secret,
                "x-api-version": self.s.cashfree_api_version, "Content-Type": "application/json"}

    async def _req(self, method: str, path: str, **kw):
        if not self.is_configured():
            raise GatewayError("Cashfree is not configured")
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.request(method, f"{self.base}{path}", headers=self._headers(), **kw)
        except httpx.HTTPError as e:
            raise GatewayError(f"Cashfree network error: {e}", transient=True)
        if r.status_code >= 500 or r.status_code == 429:
            raise GatewayError(f"Cashfree {r.status_code}", transient=True)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except Exception:
                msg = r.text
            raise GatewayError(f"Cashfree error: {msg}")
        return r.json()

    async def create_order(self, *, reference, amount_paise, currency, customer, description) -> GatewayOrder:
        s = self.s
        body = {
            "order_id": reference, "order_amount": float(paise_to_rupees(amount_paise)), "order_currency": currency,
            "customer_details": {"customer_id": customer.get("id") or "guest", "customer_phone": (customer.get("phone") or "").lstrip("+")[-10:] or "9999999999",
                                 "customer_name": customer.get("name") or "Customer", **({"customer_email": customer["email"]} if customer.get("email") else {})},
            "order_note": description[:200],
            "order_meta": {"notify_url": f"{s.public_base_url}/api/v1/webhooks/cashfree",
                           **({"return_url": s.cashfree_return_url} if s.cashfree_return_url else {})},
            "order_expiry_time": None,
        }
        body.pop("order_expiry_time")
        data = await self._req("POST", "/orders", json=body)
        return GatewayOrder(gateway_order_id=data["order_id"], session_id=data["payment_session_id"],
                            client_payload={"payment_session_id": data["payment_session_id"], "order_id": data["order_id"],
                                            "environment": s.cashfree_env})

    def verify_checkout_signature(self, **fields: str) -> bool:
        return False  # Cashfree has no client callback signature; status is always verified server-side

    def verify_webhook_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        secret = self.s.cashfree_webhook_secret or self.s.cashfree_client_secret
        sig, ts = headers.get("x-webhook-signature"), headers.get("x-webhook-timestamp")
        if not (secret and sig and ts):
            return False
        digest = hmac.new(secret.encode(), ts.encode() + raw_body, hashlib.sha256).digest()
        return hmac.compare_digest(base64.b64encode(digest).decode(), sig)

    @staticmethod
    def _pay_status(p: dict) -> str:
        return {"SUCCESS": "paid", "FAILED": "failed", "USER_DROPPED": "failed", "CANCELLED": "failed"}.get(p.get("payment_status"), "pending")

    def _payment_info(self, p: dict) -> GatewayPaymentInfo:
        amt = p.get("payment_amount")
        return GatewayPaymentInfo(status=self._pay_status(p), gateway_payment_id=str(p.get("cf_payment_id")) if p.get("cf_payment_id") else None,
                                  amount_paise=rupees_to_paise(amt) if amt is not None else None, currency=p.get("payment_currency"),
                                  failure_reason=(p.get("error_details") or {}).get("error_description") if isinstance(p.get("error_details"), dict) else None, raw=p)

    @staticmethod
    def _refund_info(r: dict) -> RefundInfo:
        status = {"SUCCESS": "succeeded", "CANCELLED": "failed", "FAILED": "failed"}.get(r.get("refund_status"), "pending")
        amt = r.get("refund_amount")
        return RefundInfo(status=status, gateway_refund_id=str(r.get("cf_refund_id")) if r.get("cf_refund_id") else None,
                          amount_paise=rupees_to_paise(amt) if amt is not None else None, raw=r)

    def parse_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        body = json.loads(raw_body)
        data = body.get("data", {})
        etype = body.get("type", "")
        event_id = headers.get("x-idempotency-key") or hashlib.sha256(raw_body).hexdigest()
        ev = WebhookEvent(event_id=event_id, event_type=etype, gateway_order_id=(data.get("order") or {}).get("order_id"))
        if "payment" in data:
            ev.payment = self._payment_info(data["payment"])
        if "refund" in data:
            ev.refund = self._refund_info(data["refund"])
            ev.refund_ref = data["refund"].get("refund_id")
            ev.gateway_order_id = ev.gateway_order_id or data["refund"].get("order_id")
        return ev

    async def fetch_payment_status(self, gateway_order_id: str) -> GatewayPaymentInfo:
        pays = await self._req("GET", f"/orders/{gateway_order_id}/payments")
        for p in pays or []:
            if p.get("payment_status") == "SUCCESS":
                return self._payment_info(p)
        order = await self._req("GET", f"/orders/{gateway_order_id}")
        if order.get("order_status") == "EXPIRED":
            return GatewayPaymentInfo(status="expired", raw=order)
        if pays:
            last = self._payment_info(pays[0])
            if last.status == "failed":
                return last
        return GatewayPaymentInfo(status="pending", raw=order)

    async def create_refund(self, *, gateway_order_id, gateway_payment_id, amount_paise, refund_ref, reason) -> RefundInfo:
        data = await self._req("POST", f"/orders/{gateway_order_id}/refunds", json={
            "refund_amount": float(paise_to_rupees(amount_paise)), "refund_id": refund_ref, "refund_note": reason[:100]})
        return self._refund_info(data)

    async def fetch_refund(self, *, gateway_order_id, gateway_payment_id, refund_ref, gateway_refund_id) -> RefundInfo:
        data = await self._req("GET", f"/orders/{gateway_order_id}/refunds/{refund_ref}")
        return self._refund_info(data)
