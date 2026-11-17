"""Test harness. Uses PostgreSQL when TEST_DATABASE_URL is set (CI / docker-compose), otherwise a temporary SQLite file.
All third parties (FCM, Razorpay, Cashfree, Google, Supabase Storage) are replaced by in-memory fakes: no real payment or push is ever made."""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid

_TMP = tempfile.mkdtemp(prefix="cafe-tests-")
os.environ.setdefault("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{_TMP}/test.db")
os.environ.update({
    "DATABASE_URL": os.environ["TEST_DATABASE_URL"], "ENVIRONMENT": "test", "EMBEDDED_WORKER": "false", "RATE_LIMIT_ENABLED": "false",
    "JWT_SECRET": "test-secret-test-secret-test-secret-0123456789", "ARGON2_TIME_COST": "1", "ARGON2_MEMORY_COST": "8192", "LOG_JSON": "false",
    "DB_USE_PGBOUNCER": "false", "STORAGE_BACKEND": "local", "LOCAL_MEDIA_DIR": f"{_TMP}/media", "GOOGLE_CLIENT_IDS": "test-client",
    "RAZORPAY_KEY_ID": "rzp_test_key", "RAZORPAY_KEY_SECRET": "rzp_secret", "RAZORPAY_WEBHOOK_SECRET": "rzp_whsec",
    "CASHFREE_CLIENT_ID": "cf_id", "CASHFREE_CLIENT_SECRET": "cf_secret", "PUBLIC_BASE_URL": "http://testserver", "QR_SITE_URL": "https://order.example.com",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import app.models  # noqa: E402,F401
from app.core import ratelimit  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.integrations import registry  # noqa: E402
from app.integrations.base import GatewayOrder, GatewayPaymentInfo, RefundInfo, WebhookEvent  # noqa: E402
from app.integrations.firebase.client import PushResult  # noqa: E402

DB_URL = os.environ["TEST_DATABASE_URL"]
IS_PG = DB_URL.startswith("postgresql")


def _run(coro):
    return asyncio.run(coro)


async def _schema(create: bool):
    eng = create_async_engine(DB_URL, poolclass=NullPool)
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        if create:
            await c.run_sync(Base.metadata.create_all)
    await eng.dispose()


async def _truncate():
    eng = create_async_engine(DB_URL, poolclass=NullPool)
    async with eng.begin() as c:
        names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        if IS_PG:
            await c.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        else:
            await c.execute(text("PRAGMA foreign_keys=OFF"))
            for t in reversed(Base.metadata.sorted_tables):
                await c.execute(text(f'DELETE FROM "{t.name}"'))
    await eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def _db_schema():
    _run(_schema(True))
    yield
    _run(_schema(False))


# ------------------------------------------------------------------ fakes
class FakePush:
    def __init__(self):
        self.sent: list[dict] = []
        self.invalid_tokens: set[str] = set()
        self.fail_transient = False

    async def send(self, tokens, *, title, body, data, high_priority, data_only, channel_id):
        out = []
        for t in tokens:
            if t in self.invalid_tokens:
                out.append(PushResult(ok=False, invalid_token=True, permanent=True, error="UnregisteredError"))
            elif self.fail_transient:
                out.append(PushResult(ok=False, error="Unavailable"))
            else:
                self.sent.append({"token": t, "title": title, "body": body, "data": data, "high_priority": high_priority, "data_only": data_only})
                out.append(PushResult(ok=True, message_id=f"msg-{len(self.sent)}"))
        return out


class FakeGateway:
    def __init__(self, name: str):
        self.name, self.configured, self.n = name, True, 0
        self.statuses: dict[str, GatewayPaymentInfo] = {}
        self.refund_status, self.refunds = "pending", []

    def is_configured(self): return self.configured

    async def create_order(self, *, reference, amount_paise, currency, customer, description):
        self.n += 1
        oid = f"{self.name}_order_{self.n}"
        return GatewayOrder(oid, {"order_id": oid, "amount": amount_paise, "currency": currency}, "sess_x" if self.name == "cashfree" else None)

    def verify_checkout_signature(self, **f): return f.get("razorpay_signature") == "valid-signature"
    def verify_webhook_signature(self, raw, headers): return headers.get("x-test-signature") == "ok"

    def parse_webhook(self, raw, headers):
        import json
        b = json.loads(raw)
        ev = WebhookEvent(event_id=b["event_id"], event_type=b["type"], gateway_order_id=b.get("order_id"))
        if "status" in b:
            ev.payment = GatewayPaymentInfo(status=b["status"], gateway_payment_id=b.get("payment_id"), amount_paise=b.get("amount"), currency=b.get("currency", "INR"))
        if "refund_status" in b:
            ev.refund = RefundInfo(status=b["refund_status"], gateway_refund_id=b.get("gateway_refund_id"))
            ev.refund_ref = b.get("refund_ref")
        return ev

    async def fetch_payment_status(self, oid): return self.statuses.get(oid, GatewayPaymentInfo(status="pending"))

    async def create_refund(self, *, gateway_order_id, gateway_payment_id, amount_paise, refund_ref, reason):
        self.refunds.append((gateway_payment_id, amount_paise, refund_ref))
        return RefundInfo(status=self.refund_status, gateway_refund_id=f"rf_{len(self.refunds)}")

    async def fetch_refund(self, *, gateway_order_id, gateway_payment_id, refund_ref, gateway_refund_id):
        return RefundInfo(status=self.refund_status, gateway_refund_id=gateway_refund_id)


class FakeGoogle:
    async def verify(self, id_token):
        if id_token.startswith("bad"):
            from app.core.errors import Unauthorized
            raise Unauthorized("Invalid Google token", code="invalid_google_token")
        sub = id_token.replace("good-", "")
        return {"sub": f"sub-{sub}", "email": f"{sub}@gmail.com", "name": sub.title(), "picture": None}


class FakeStorage:
    def __init__(self): self.files: dict[str, bytes] = {}
    async def upload(self, path, data, content_type):
        self.files[path] = data
        return f"https://cdn.test/{path}"
    async def delete(self, path): self.files.pop(path, None)


class Ctx:
    """Convenience wrapper used by all tests."""

    def __init__(self, client: TestClient, push, gw, storage):
        self.client, self.push, self.gw, self.storage = client, push, gw, storage
        self.admin_email, self.admin_password = "owner@example.com", "Str0ngPassw0rd!"
        self.admin: dict = {}
        self.ids: dict = {}

    def req(self, method, path, h=None, **kw):
        return self.client.request(method, "/api/v1" + path if not path.startswith("/api") and not path.startswith("/health") and not path.startswith("/ready") else path, headers=h, **kw)

    def get(self, path, h=None, **kw): return self.req("GET", path, h, **kw)
    def post(self, path, json=None, h=None, **kw): return self.req("POST", path, h, json=json, **kw)
    def patch(self, path, json=None, h=None, **kw): return self.req("PATCH", path, h, json=json, **kw)
    def put(self, path, json=None, h=None, **kw): return self.req("PUT", path, h, json=json, **kw)
    def delete(self, path, h=None, **kw): return self.req("DELETE", path, h, **kw)

    def call(self, fn, *a, **kw):
        """Run an async function inside the app's event loop (same DB engine)."""
        return self.client.portal.call(lambda: fn(*a, **kw))

    def run_jobs(self, rounds: int = 3):
        from app.services.jobs import run_once
        import app.workers.handlers  # noqa: F401
        total = 0
        for _ in range(rounds):
            total += self.call(run_once)
        return total

    def bootstrap_admin(self):
        from scripts.bootstrap_admin import bootstrap
        self.call(bootstrap, self.admin_email, self.admin_password)
        r = self.post("/auth/admin/login", {"email": self.admin_email, "password": self.admin_password})
        assert r.status_code == 200, r.text
        self.admin = {"Authorization": f"Bearer {r.json()['access_token']}"}
        return self.admin

    def customer(self, name="Asha", email=None, phone="9876543210", password="Passw0rdPass1"):
        email = email or f"{name.lower()}{uuid.uuid4().hex[:5]}@example.com"
        r = self.post("/auth/customer/register", {"name": name, "email": email, "password": password, "phone": phone})
        assert r.status_code == 201, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def rider(self, name="Ravi", phone="9000000001", password="RiderPass123"):
        r = self.post("/admin/riders", {"name": name, "phone": phone, "password": password}, h=self.admin)
        assert r.status_code == 201, r.text
        rid = r.json()["id"]
        lr = self.post("/auth/rider/login", {"phone": phone, "password": password})
        assert lr.status_code == 200, lr.text
        h = {"Authorization": f"Bearer {lr.json()['access_token']}"}
        assert self.patch("/riders/me/availability", {"available": True}, h=h).status_code == 200
        return rid, h

    def seed(self):
        a = self.admin
        self.post("/admin/payment-gateways", None, h=a)
        r = self.put("/admin/payment-gateways", {"razorpay_enabled": True, "cashfree_enabled": True}, h=a)
        assert r.status_code == 200, r.text
        taxes = self.get("/admin/settings/taxes", h=a).json()
        self.patch(f"/admin/settings/taxes/{taxes[0]['id']}", {"name": "GST 5%", "rate_bps": 500, "is_default": True}, h=a)
        self.patch("/admin/settings/delivery", {"base_charge_paise": 3000, "free_delivery_above_paise": 50000, "min_order_paise": 0, "allowed_postal_codes": ["560001"]}, h=a)
        cat = self.post("/admin/menu/categories", {"name": "Coffee"}, h=a).json()["id"]
        latte = self.post("/admin/menu/items", {"category_id": cat, "name": "Latte", "price_paise": 15000,
                                                "variants": [{"name": "Small", "price_paise": 15000}, {"name": "Large", "price_paise": 20000}]}, h=a).json()
        cake = self.post("/admin/menu/items", {"category_id": cat, "name": "Cake", "price_paise": 10000,
                                               "addons": [{"name": "Extra cream", "price_paise": 2000, "max_quantity": 2}]}, h=a).json()
        soup = self.post("/admin/menu/items", {"category_id": cat, "name": "Soup", "price_paise": 9000, "is_available": False}, h=a).json()
        table = self.post("/admin/tables", {"number": "1", "label": "Window", "capacity": 4, "area": "Ground"}, h=a).json()
        qr = self.post(f"/admin/tables/{table['id']}/qr", h=a).json()
        self.ids = {"cat": cat, "latte": latte["id"], "latte_small": next(v["id"] for v in latte["variants"] if v["name"] == "Small"), "latte_large": next(v["id"] for v in latte["variants"] if v["name"] == "Large"), "cake": cake["id"],
                    "addon": cake["addons"][0]["id"], "soup": soup["id"], "table": table["id"], "qr": qr["token"]}
        return self.ids

    def table_token(self):
        r = self.post("/qr/resolve", {"token": self.ids["qr"]})
        assert r.status_code == 200, r.text
        return r.json()["table_session_token"]

    def cart(self, kind="cake", qty=1, **extra):
        i = self.ids
        item = {"cake": {"menu_item_id": i["cake"], "quantity": qty}, "latte": {"menu_item_id": i["latte"], "variant_id": i["latte_small"], "quantity": qty}}[kind]
        return [{**item, **extra}]

    def place(self, order_type="pickup", payment="cash", h=None, kind="cake", qty=1, **extra):
        body = {"order_type": order_type, "payment_method": payment, "items": self.cart(kind, qty), **extra}
        if order_type == "delivery":
            body.setdefault("delivery_address", {"line1": "12 MG Road", "postal_code": "560001"})
        if order_type == "dine_in":
            body.setdefault("table_session_token", self.table_token())
        return self.post("/orders", body, h=h)

    def walk_in(self, payment="cash", kind="cake", **extra):
        return self.post("/admin/orders", {"order_type": "walk_in", "payment_method": payment, "items": self.cart(kind), **extra}, h=self.admin)

    def advance(self, order_id, *statuses):
        for s in statuses:
            r = self.post(f"/admin/orders/{order_id}/status", {"status": s}, h=self.admin)
            assert r.status_code == 200, r.text


@pytest.fixture()
def ctx():
    ratelimit.reset_local()
    _run(_truncate())
    push, storage = FakePush(), FakeStorage()
    gw = {"razorpay": FakeGateway("razorpay"), "cashfree": FakeGateway("cashfree")}
    registry.override("push", push)
    registry.override("gw:razorpay", gw["razorpay"])
    registry.override("gw:cashfree", gw["cashfree"])
    registry.override("google", FakeGoogle())
    registry.override("storage", storage)
    from app.main import app
    with TestClient(app) as client:
        c = Ctx(client, push, gw, storage)
        yield c
    for k in ("push", "gw:razorpay", "gw:cashfree", "google", "storage"):
        registry.override(k, None)


@pytest.fixture()
def cafe(ctx):
    ctx.bootstrap_admin()
    ctx.seed()
    return ctx
