"""Razorpay server-side integration using the official REST API (https://razorpay.com/docs/api/)."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Mapping

import httpx

from app.core.config import get_settings
from app.integrations.base import GatewayError, GatewayOrder, GatewayPaymentInfo, RefundInfo, WebhookEvent

API = "https://api.razorpay.com/v1"


class RazorpayGateway:
    name = "razorpay"

    def __init__(self) -> None:
        self.s = get_settings()

    def is_configured(self) -> bool:
        return bool(self.s.razorpay_key_id and self.s.razorpay_key_secret)

    async def _req(self, method: str, path: str, **kw) -> dict:
        if not self.is_configured():
            raise GatewayError("Razorpay is not configured")
        try:
            async with httpx.AsyncClient(timeout=20, auth=(self.s.razorpay_key_id, self.s.razorpay_key_secret)) as c:
                r = await c.request(method, f"{API}{path}", **kw)
        except httpx.HTTPError as e:
            raise GatewayError(f"Razorpay network error: {e}", transient=True)
        if r.status_code >= 500 or r.status_code == 429:
            raise GatewayError(f"Razorpay {r.status_code}", transient=True)
        if r.status_code >= 400:
            try:
                desc = r.json().get("error", {}).get("description", r.text)
            except Exception:
                desc = r.text
            raise GatewayError(f"Razorpay error: {desc}")
        return r.json()

    async def create_order(self, *, reference, amount_paise, currency, customer, description) -> GatewayOrder:
        data = await self._req("POST", "/orders", json={
            "amount": amount_paise, "currency": currency, "receipt": reference[:40],
            "notes": {"reference": reference, "description": description[:200]}})
        return GatewayOrder(
            gateway_order_id=data["id"],
            client_payload={"key_id": self.s.razorpay_key_id, "order_id": data["id"], "amount": data["amount"],
                            "currency": data["currency"], "name": "Crescent Cafe", "prefill": {
                                "contact": customer.get("phone"), "email": customer.get("email"), "name": customer.get("name")}})

    # --- signatures
    def verify_checkout_signature(self, **f: str) -> bool:
        try:
            msg = f"{f['razorpay_order_id']}|{f['razorpay_payment_id']}"
            expected = hmac.new(self.s.razorpay_key_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, f["razorpay_signature"])
        except KeyError:
            return False

    def verify_webhook_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        secret = self.s.razorpay_webhook_secret
        sig = headers.get("x-razorpay-signature")
        if not secret or not sig:
            return False
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    # --- mapping
    @staticmethod
    def _payment_info(p: dict) -> GatewayPaymentInfo:
        st = p.get("status")
        status = {"captured": "paid", "failed": "failed", "refunded": "paid"}.get(st, "pending")
        return GatewayPaymentInfo(status=status, gateway_payment_id=p.get("id"), amount_paise=p.get("amount"),
                                  currency=p.get("currency"), failure_reason=p.get("error_description"), raw=p)

    @staticmethod
    def _refund_info(r: dict) -> RefundInfo:
        status = {"processed": "succeeded", "failed": "failed"}.get(r.get("status"), "pending")
        return RefundInfo(status=status, gateway_refund_id=r.get("id"), amount_paise=r.get("amount"), raw=r)

    def parse_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        body = json.loads(raw_body)
        event_type = body.get("event", "")
        event_id = headers.get("x-razorpay-event-id") or hashlib.sha256(raw_body).hexdigest()
        pl = body.get("payload", {})
        ev = WebhookEvent(event_id=event_id, event_type=event_type)
        if "payment" in pl:
            ent = pl["payment"]["entity"]
            ev.gateway_order_id = ent.get("order_id")
            ev.payment = self._payment_info(ent)
            if event_type == "payment.authorized":
                ev.payment.status = "pending"
        if "order" in pl and ev.gateway_order_id is None:
            ev.gateway_order_id = pl["order"]["entity"].get("id")
        if "refund" in pl:
            ent = pl["refund"]["entity"]
            ev.refund = self._refund_info(ent)
            ev.refund_ref = (ent.get("notes") or {}).get("refund_ref") if isinstance(ent.get("notes"), dict) else None
        return ev

    async def fetch_payment_status(self, gateway_order_id: str) -> GatewayPaymentInfo:
        data = await self._req("GET", f"/orders/{gateway_order_id}/payments")
        items = data.get("items", [])
        for p in items:
            if p.get("status") in ("captured", "refunded"):
                return self._payment_info(p)
        if items:
            last = items[0]
            info = self._payment_info(last)
            return info
        order = await self._req("GET", f"/orders/{gateway_order_id}")
        return GatewayPaymentInfo(status="pending", amount_paise=order.get("amount"), currency=order.get("currency"), raw=order)

    async def create_refund(self, *, gateway_order_id, gateway_payment_id, amount_paise, refund_ref, reason) -> RefundInfo:
        if not gateway_payment_id:
            raise GatewayError("No gateway payment id to refund")
        data = await self._req("POST", f"/payments/{gateway_payment_id}/refund", json={
            "amount": amount_paise, "receipt": refund_ref[:40], "notes": {"refund_ref": refund_ref, "reason": reason[:200]}})
        return self._refund_info(data)

    async def fetch_refund(self, *, gateway_order_id, gateway_payment_id, refund_ref, gateway_refund_id) -> RefundInfo:
        if not (gateway_refund_id and gateway_payment_id):
            return RefundInfo(status="pending")
        data = await self._req("GET", f"/payments/{gateway_payment_id}/refunds/{gateway_refund_id}")
        return self._refund_info(data)
