# Known limitations & recommended upgrades

## A. Must-do before real customers (gaps, not "nice to haves")
1. **Verify Razorpay and Cashfree in their sandboxes.** Tests use mocked provider responses (with real HMAC/parsing code). Run one success, one failure, one refund and one webhook delivery per gateway.
2. **Use an always-on host (Render Starter or better).** The free plan sleeps; alarms, WebSockets and the job worker stop while it sleeps.
3. **Owner/accountant must set and verify tax rates and tax-inclusive/exclusive pricing.** The seeded default tax is 0 %.
4. **Paid Supabase plan (or your own scheduled `pg_dump`)**: free projects pause when idle and have weak backups. Rehearse a restore.
5. **Android work the backend cannot do:** audible repeating alarms, notification channels, FCM token lifecycle calls, battery-optimisation exemptions (see WEBSOCKETS.md). Test on real phones with the app killed.
6. **The Android apps and the QR website are not part of this repository.**

## B. Known limitations (by design in this version)
* **Mobile numbers are collected, validated and normalised but not OTP-verified.** A fake number defeats per-customer coupon limits and gives no way to reach a guest.
* **Guests receive no push notifications** (no device). They get WebSocket/polling updates and the OTP via the tracking page; consider SMS/WhatsApp for guests.
* **Guests cannot chat** (chat requires a signed-in customer).
* **Table QR plaintext is shown once** (only a hash is stored). Re-printing means regenerating (which invalidates the old sticker). Bulk export therefore rotates tokens.
* **A rider can be assigned only when the order is `ready`.** Pre-assigning during preparation is not supported.
* **Chat push suppression** ("don't push if the recipient is looking at the chat") only knows about WebSockets on the same instance; with several instances a push may be sent even if the user is online (harmless).
* **WebSocket sessions end at access-token expiry (≤15 min)**; revocation of an already-open socket is not instant.
* **Reports are aggregated in Python** (fine for a cafe; slow for millions of orders).
* **Invoices are created when the order is accepted**; the invoice row keeps the payment method/status at creation, while the API shows the live order payment status.
* Single Admin account only (by requirement): no staff roles, no 2FA.
* Cashfree/Razorpay adapters talk to their REST APIs directly (not the vendor SDKs) to stay async; re-check field names against current provider docs when upgrading API versions.
* The Docker image and `render.yaml` were written but **not built/deployed in the authoring environment** (no Docker there). CI builds the image on every push.

## C. Recommended upgrades (rough priority)
| # | Upgrade | Why |
|---|---|---|
| 1 | Phone OTP verification (Firebase Phone Auth / SMS) | stops fake numbers, enables guest reach and safer coupon limits |
| 2 | SMS / WhatsApp order updates for guests | guests have no FCM |
| 3 | Admin TOTP 2FA, JWT key rotation (RS256 + `kid`), IP allow-list, Sentry | the single Admin account controls everything |
| 4 | Redis + separate worker service + 2 API instances | resilience, shared rate limits and WebSocket fan-out |
| 5 | Rider pre-assignment, ETA/distance (Google Maps), geo-radius delivery zones, batching | faster delivery operations (assignment stays manual) |
| 6 | Encrypted (not hashed) QR token storage so the Admin can re-download stickers | convenience without rotating |
| 7 | Thermal-printer / kitchen display (KDS) integration, per-station tickets | kitchen workflow |
| 8 | Roll-up tables / materialised views for reports, GST summary (HSN/SAC), scheduled e-mail reports, XLSX export | scale and accounting |
| 9 | Provider settlement reconciliation, chargeback/dispute handling, webhook replay UI | finance |
| 10 | Staff roles (cashier/kitchen) and multi-branch admin UI — schema is ready (`branch_id` everywhere) | growth |
| 11 | Loyalty points / wallet / tips, scheduled pre-orders, table reservations, split bill per table | revenue features |
| 12 | Ingredient-level inventory with auto-unavailable | explicitly out of scope for v1 |
| 13 | Customer data export endpoint, consent log, localisation (Hindi etc.), per-user notification preferences | privacy / UX |
| 14 | Contract tests against provider sandboxes in CI, load tests (Locust), Android instrumentation tests | quality |
| 15 | Menu caching with ETag + CDN for images; DB read replica | performance |
