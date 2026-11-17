# Architecture, API module plan and security model

## 1. Shape of the system
```
Customer app ─┐                                     ┌──────────────┐   ┌─────────────────────────┐
QR website ───┼──► HTTPS REST /api/v1 + WebSocket ─►│  FastAPI app │──►│ Supabase PostgreSQL      │ (single source of truth)
Admin app ────┤                                     │  (async)     │   └─────────────────────────┘
Rider app ────┘     ▲ FCM push (background)         │              │──►  Supabase Storage (menu images)
                    │                               │ embedded job │──►  Razorpay / Cashfree (REST + signed webhooks)
   Firebase Cloud Messaging ◄───────────────────────│ worker       │──►  Firebase Admin SDK (FCM only)
                                                    └──────┬───────┘     Redis (optional: shared rate limits + WS fan-out)
                                                           └── Postgres-backed job queue (no extra paid service)
```
* **Layers:** `api/v1` (routers, thin) → `services` (all business rules, transactions) → `models` (SQLAlchemy). `integrations/` hides Razorpay, Cashfree, Firebase, Google, Storage behind interfaces so tests swap in fakes. `websockets/` and `workers/` are separate packages.
* **Transactions:** one DB transaction per request (`get_db`), committed on success. WebSocket broadcasts are registered as *post-commit* callbacks, so clients never hear about a change that was rolled back. Jobs are enqueued inside the same transaction as the business change (a job exists iff the change committed).
* **Realtime + push:** every important change creates a durable `notification_events` row. It is (a) broadcast over WebSocket to authorised topics and (b) turned into an FCM push job. Clients refetch via REST after any reconnect and can call `GET /notifications/events?after_id=` to catch up.
* **Jobs (free-tier friendly):** table `jobs` + `FOR UPDATE SKIP LOCKED`, exponential backoff with jitter, idempotent handlers, de-duplicated periodic scheduling, stuck-job recovery, admin inspection and retry (`/admin/jobs`). The worker runs inside the API process by default (`EMBEDDED_WORKER=true`); run `python -m scripts.run_worker` as a separate service later (set `EMBEDDED_WORKER=false`).

## 2. Roles and authorisation
| Role | Created by | Login | Sees |
|---|---|---|---|
| Admin (exactly one, DB-enforced) | `python -m scripts.bootstrap_admin` only | email + password | everything for the branch |
| Rider | Admin (`POST /admin/riders`) | phone + password | own assignments, delivery details, delivery chat, own tracking |
| Customer | self-register, or Google (Gmail) sign-in; mobile number required before first order | email+password / Google | own orders, payments, chats, invoices |
| Guest | no account (name + mobile at checkout) | per-order `X-Guest-Token` (stored hashed) | that one order only |

Every protected route declares its role dependency; objects are checked for ownership (`authorize_order`, `require_member`, rider-assignment ownership). Foreign objects return **404**, not 403, so ids cannot be probed. Access tokens live 15 min; refresh tokens rotate on use and **reuse revokes the whole session**; every request checks the session row, so logout / password change / deactivation take effect immediately (WebSocket connections end at token expiry, ≤15 min).

## 3. Order state machine
```
dine_in / pickup / walk_in:  [payment_pending →] payment_verified|placed → accepted → preparing → ready → completed
delivery:                    …  ready → rider_assigned → picked_up → out_for_delivery → delivered   (delivered ONLY via OTP)
any pre-completion state → cancelled | rejected (reason required) → refund_pending → refunded   (only if money was taken)
```
* Initial status: online payment → `payment_pending` (Admin is **not** alarmed until payment is verified); customer cash order → `placed` (alarm immediately); Admin walk-in → `accepted`.
* `completed` is an added terminal state for dine-in/pickup/walk-in (served/collected). Invalid or repeated transitions return 409 and change nothing. Every change writes `order_status_history` (actor, time, reason).
* A rider is assigned to an order that is `ready` (design decision; see UPGRADES.md for pre-assignment). Rejecting an offer leaves the order `ready` for the Admin to reassign.
* `delivered` cannot be reached by any admin or rider status endpoint: `transition()` itself refuses it unless called by the OTP verifier.

## 4. API module plan (`/api/v1`)
Public/customer: `/auth`, `/customers`, `/menu`, `/qr`, `/settings/public`, `/orders` (+`/orders/quote`), `/payments`, `/webhooks`, `/offers`, `/coupons/validate`, `/deliveries/orders/{id}/otp`, `/tracking/orders/{id}`, `/chat`, `/notifications`, `/invoices`.
Rider: `/riders/me`, `/deliveries`, `/tracking/location`, `/chat`.
Admin (all under `/admin`): dashboard, orders (+kitchen status, alarm ack, assign), deliveries, riders, payments (+counter-pending, confirm/reverse cash, refunds), payment-gateways, menu (categories/items/variants/add-ons/images), tables (+QR, sessions), offers & coupons, settings (cafe/delivery/taxes), invoices, `reports/*`, audit-logs, jobs, webhook-events, notifications/broadcast.
Ops: `GET /health` (liveness), `GET /ready` (DB check), `GET /api/v1/diagnostics` (admin JWT or `X-Diagnostics-Token`; the only endpoint with infrastructure detail).
Full list: [API.md](API.md). WebSocket protocol: [WEBSOCKETS.md](WEBSOCKETS.md). Tables: [DATABASE.md](DATABASE.md).

## 5. Security checklist (what is implemented)
* Argon2id password hashes; constant-time-ish login for unknown users; account lockout after 5 failures (15 min); per-IP and per-account login rate limits.
* JWT HS256, `typ` claim separates access vs table-session tokens, `alg=none`/tampered/expired tokens rejected (tested).
* Server-side pricing, tax, discounts and availability; client totals are ignored (tested). Idempotency keys on orders and refunds.
* Payments: client "success" is never trusted. Paid status comes only from a signed webhook or a server-to-server provider fetch; amount, currency and order reference are verified; mismatches are quarantined and alert the Admin; duplicate confirmations are no-ops; double payment through two gateways is auto-refunded; late payment on a closed order triggers a refund.
* Webhooks: HMAC verified on the raw body, de-duplicated, failures retried by job.
* QR tokens: 192-bit random, only the hash is stored, regeneration/revocation immediately invalidates the token **and** table-session tokens issued from it. Optional per-table PIN.
* Delivery OTP: CSPRNG, keyed hash + salt in DB, plaintext never stored or logged, never sent to rider/admin, expiry, attempt lock, single use, row-locked verification (tested concurrently on PostgreSQL).
* Chat: membership verified on every call, internal admin notes never reach customers/riders, size and rate limits.
* Rate limits on login, registration, Google login, coupon validation, quotes, orders, payments, QR resolve, OTP, location, chat. Redis-backed when `REDIS_URL` is set.
* Uploads: type sniffed from magic bytes (not the extension), 5 MB cap. CORS allow-list. Security headers. Errors never leak stack traces. Secrets only from env vars. `docs/` deployment checklist covers HTTPS (terminated by Render).
* Privacy: account deletion anonymises PII (`DELETE /customers/me`); rider location retention job; old sessions/logs/jobs pruned; audit log for all Admin changes.
