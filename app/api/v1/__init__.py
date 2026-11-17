from fastapi import APIRouter

from app.api.v1 import (admin_catalog, admin_ops, admin_settings, auth, chat, customers, delivery, invoices, notifications, orders, payments, public)

api_router = APIRouter(prefix="/api/v1")
for r in (auth.router, customers.router, public.menu_router, public.qr_router, public.settings_router, public.offers_router, orders.router,
          payments.router, payments.webhooks, delivery.riders_router, delivery.deliveries_router, delivery.tracking_router, chat.router,
          notifications.router, invoices.router, admin_ops.router, admin_catalog.router, admin_settings.router, admin_settings.reports_router):
    api_router.include_router(r)
