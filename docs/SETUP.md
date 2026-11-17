# Third-party setup

## 1. Supabase (database + menu image storage)
1. Create a project at supabase.com. **Project Settings → Database → Connection string**: copy the **Session pooler** URI (port 5432; works over IPv4, which Render needs). Put it in `DATABASE_URL` (the `postgresql://` prefix is converted for you). Keep `DB_USE_PGBOUNCER=true` and `DB_SSL_REQUIRE=true`.
2. Run migrations once: `alembic upgrade head` (the Docker image does this on every start).
3. **Storage → New bucket** named `menu-images`, set it **Public** (customers load images by URL). Put `SUPABASE_URL` and the `service_role` key (Project Settings → API) into the environment. The backend uploads images itself; the key never reaches any app. Do not enable Supabase's auto-generated REST/RLS access for these tables: only this backend talks to the database. If you want belt-and-braces, enable RLS on all tables with no policies (the backend connects as the `postgres` role and bypasses RLS).
4. Supabase free projects pause after a week of inactivity and have limited backups: see DEPLOYMENT.md (backups).

## 2. Firebase Cloud Messaging (push only)
1. Firebase console → create a project → **Add Android app** for each of the three apps (Customer, Admin, Rider) and put each `google-services.json` in that Android app. FCM standard messaging has no per-message fee.
2. **Project settings → Service accounts → Generate new private key**. Paste the JSON (or its base64) into `FIREBASE_CREDENTIALS_JSON` on the server. Never commit it, never put it in an APK.
3. Android: create notification channels `new_orders` (Admin and Rider apps, importance HIGH, custom sound) and `general`. The names are configurable (`FCM_ANDROID_CHANNEL_*`).
4. Verify: register a device (`POST /notifications/devices`), place an order, watch `GET /api/v1/diagnostics` (`firebase_configured: true`) and the `notification_delivery_logs` table.

## 3. Google Sign-In (customers log in with Gmail)
1. Google Cloud console → APIs & Services → Credentials → create **OAuth client IDs**: one *Web* client and one *Android* client per Customer app/signing key (SHA-1).
2. Put all client IDs, comma separated, in `GOOGLE_CLIENT_IDS`. The app sends the Google **ID token** to `POST /auth/customer/google`; the backend verifies signature, audience, issuer and `email_verified`.
3. After Google login the response contains `phone_required: true` until the customer saves a mobile number (`PUT /customers/me/phone`). Orders are refused (`phone_required`) without one.

## 4. Razorpay
1. Dashboard → Settings → API Keys → generate **Test** keys first. Set `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`.
2. Settings → Webhooks → add `https://YOUR-API/api/v1/webhooks/razorpay`, choose a secret → `RAZORPAY_WEBHOOK_SECRET`. Subscribe to `payment.captured`, `payment.failed`, `payment.authorized`, `order.paid`, `refund.processed`, `refund.failed`.
3. Keep **payment auto-capture ON** (Settings → Payment Capture) so payments reach `captured` without a manual capture call.
4. In the Android/web checkout use the `client_payload` returned by `POST /payments/orders/{id}/initiate`, then optionally call `POST /payments/orders/{id}/verify/razorpay` with the three callback fields. The backend re-fetches the status server-side; the webhook is the safety net.
5. Admin: `PUT /admin/payment-gateways {"razorpay_enabled": true}` (refused until the keys exist).

## 5. Cashfree
1. Cashfree dashboard → Developers → API keys. Set `CASHFREE_CLIENT_ID`, `CASHFREE_CLIENT_SECRET`, `CASHFREE_ENV=sandbox` (switch to `production` with live keys), `CASHFREE_RETURN_URL` (your QR site's order page).
2. Developers → Webhooks → `https://YOUR-API/api/v1/webhooks/cashfree`, API version 2023-08-01. Cashfree signs webhooks with your client secret (override with `CASHFREE_WEBHOOK_SECRET` if you configured a separate one).
3. The client opens Cashfree checkout with `client_payload.payment_session_id`; after redirect call `POST /payments/orders/{id}/sync` to refresh status from Cashfree.
4. `PUT /admin/payment-gateways {"cashfree_enabled": true}`.

> **Both gateways were exercised in automated tests only with mocked provider responses plus real signature/webhook-parsing code. Before going live, run a sandbox payment, a failed payment, a refund and a webhook delivery for each gateway and confirm the dashboard shows the events as processed (`GET /admin/webhook-events`).** Provider payload/header details can change; the adapters are isolated in `app/integrations/`.

## 6. Cafe configuration (Admin app, first login)
1. Run `python -m scripts.bootstrap_admin`, sign in, change nothing else until: **taxes** (`PATCH /admin/settings/taxes/{id}`: the seeded default is 0 % and the dashboard warns you; **the owner/accountant must confirm correct GST rates and whether menu prices include tax**), **opening hours**, **delivery settings** (charge, minimum, postal codes), **cafe profile / GSTIN / invoice prefix**, **payment gateways**.
2. Create categories, items (with photos), tables and print QR codes (`POST /admin/tables/{id}/qr` or bulk).
3. Create riders (`POST /admin/riders`); the backend marks new and reset riders `must_change_password: true` at login; the Rider app should force a password change (`POST /auth/change-password`) before anything else.
