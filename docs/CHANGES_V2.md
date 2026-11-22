# Backend changes in this release

Everything here is additive: existing endpoints keep their shapes. Apply `alembic upgrade head` (migration 0002) on deploy; `RUN_MIGRATIONS=true` does it automatically on Render.

## 1. E-mail through Resend
- `app/integrations/email/resend.py` calls the Resend REST API server-side. `RESEND_API_KEY` and `EMAIL_FROM` come from the environment.
- `app/services/email_templates.py`: branded HTML + plain text. Every value from a person is HTML-escaped.
- E-mails are queued as `send_email` jobs (retry with backoff, no data in the job except ids): welcome, order confirmation, order accepted, on the way, delivered, order rejected, password changed.
- The password-reset OTP is the one exception: it is sent from memory after the response and is never written to the job table or the logs.
- Missing key = e-mails skipped with a warning; the app keeps working.
- Order e-mails go to the customer's e-mail (guests only if they gave one). Admin-created orders send nothing.

## 2. Forgot password with an e-mailed OTP (customers)
`POST /auth/password/forgot` {email} -> `POST /auth/password/verify-otp` {email, otp} -> `POST /auth/password/reset` {reset_token, new_password}.
- OTP length 4 (configurable), expires in 5 minutes, 5 wrong tries burn it, stored only as a salted HMAC, verified once, consumed by the password change, newest code only.
- Resend cooldown 60 s, 5 codes per hour per account, per-IP and per-e-mail rate limits.
- Same answer whether or not the e-mail is registered (no account discovery).
- A successful reset signs the customer out everywhere and sends a "password changed" e-mail.
- A 4-digit code has only 10,000 possibilities. The limits above keep guessing impractical, but set `PASSWORD_RESET_OTP_DIGITS=6` if you can.

## 3. Phone number is mandatory
`POST /auth/customer/register` now requires `phone`. Google sign-in still returns `phone_required: true` until the number is saved (`PUT /customers/me/phone`).

## 4. Rider collects payment at the door
- `POST /deliveries/{assignment_id}/collect/cash` {amount_received_paise}: records cash, returns change due; repeating it returns the same result.
- `POST /deliveries/{assignment_id}/collect/upi-qr`: creates (or returns the open) single-use Razorpay UPI QR for the order amount.
- `GET /deliveries/{assignment_id}/collect/status`: payment state for the rider screen; with an open QR it asks Razorpay itself, so a missed webhook cannot leave the rider stuck.
- Payment is confirmed only by a signed webhook or a server-side fetch. Scanning proves nothing. A wrong amount is flagged, not accepted. A customer who scans after the rider took cash is refunded automatically.
- `REQUIRE_PAYMENT_BEFORE_DELIVERY` (default true): `verify-otp` answers `payment_not_collected` until the order is paid, and that attempt does not use up an OTP try.
- Razorpay setup: the **QR Codes** product must be enabled on the account, and the webhook must also subscribe to **`qr_code.credited`**.
- **Assumptions to confirm in the Razorpay sandbox:** the QR create request/response field names (`/v1/payments/qr_codes`, `image_url`, `close_by`), and that the `qr_code.credited` payload carries `qr_code.entity.id` plus `payment.entity`. The code matches the payment by the QR id and also reconciles through `GET /payments/qr_codes/{id}/payments`.

## 5. Rider cash ledger
Append-only `rider_cash_ledger`. `GET /admin/cash/summary`, `GET /admin/riders/{id}/cash-ledger`, `POST /admin/riders/{id}/cash-handover`. Reversing a rider-collected cash payment writes a reversal row.

## 6. "Rider is nearby"
When a rider's location update is within `ARRIVAL_NOTIFY_DISTANCE_M` (default 10 m) of the customer's pinned delivery location, the customer gets one push/event, calculated on the server. Skipped when GPS accuracy is poor or the order has no coordinates. Phone GPS is often 5-20 m off, so 30-50 m is more reliable in practice.

## 7. Customers
`GET/POST /customers/me/addresses`, `PATCH/DELETE /customers/me/addresses/{id}` (max 10, one default, soft delete; orders keep their own copy). Admin: `GET /admin/customers?q=`, `GET /admin/customers/{id}`, `PATCH /admin/customers/{id}/active`.

## 8. Security
`client_ip()` no longer trusts the left side of `X-Forwarded-For` (set `TRUSTED_PROXY_COUNT`; Render = 1).

## Not done in this release
Chat v2 (customer-rider chat, images, ticks), phone OTP verification, admin 2FA, delivery radius, WebSocket token out of the URL, Redis/worker split. See `docs/UPGRADES.md`.

## Verification status
Written and reviewed without a database or network: every file compiles (`python -m compileall`), but **the test suite was not run**. Run `pytest` first (see README) and fix anything environment-specific. New tests: `test_email_and_password_reset.py`, `test_rider_payments.py`, `test_addresses_and_customers.py`; `test_delivery.py` and `test_auth.py` were adjusted for the payment gate and mandatory phone.
