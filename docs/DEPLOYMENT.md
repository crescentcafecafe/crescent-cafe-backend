# Deployment (Render + Supabase), operations and recovery

## Local development
```bash
cp .env.example .env            # set DATABASE_URL, JWT_SECRET; STORAGE_BACKEND=local is fine locally
docker compose up --build       # API on :8000 with a local Postgres; docs at http://localhost:8000/docs
docker compose exec api python -m scripts.bootstrap_admin     # creates the single Admin (prompts for email/password)
pytest                          # SQLite (fast). For the real thing:  docker compose --profile test run --rm tests
```
Without Docker: `pip install -r requirements-dev.txt && alembic upgrade head && uvicorn app.main:app --reload`.

## Deploy to Render
1. Push this repository to GitHub. Render → **New → Blueprint** → select the repo (`render.yaml`).
2. Fill the `sync: false` variables in the dashboard (see `.env.example`). `JWT_SECRET` is generated for you. Use the Supabase **session pooler** `DATABASE_URL`.
3. First deploy runs `alembic upgrade head` automatically (`scripts/start.sh`). Then open a **Shell** on the service and run `python -m scripts.bootstrap_admin` once (set `ADMIN_BOOTSTRAP_EMAIL/PASSWORD` temporarily, or answer the prompts), then **delete those two variables**.
4. Set `PUBLIC_BASE_URL` to the Render URL, `QR_SITE_URL` to your QR website, `CORS_ORIGINS` to the QR website origin(s). Register webhook URLs with Razorpay/Cashfree (SETUP.md).
5. Check `GET /ready` and (as Admin) `GET /api/v1/diagnostics`; the response lists `production_problems` (it must be `[]`).

### Free plan caveat (important)
Render's **free web service sleeps after ~15 minutes without traffic** and takes ~30-60 s to wake. While asleep the embedded worker, WebSockets and the new-order alarm path are down, so a real cafe **must use at least the Starter plan** (always on). The blueprint ships with `plan: free` only so you can test; change it to `starter` before opening to customers. A free cron-pinger is not a substitute: it does not give you WebSocket stability or restart guarantees. Scale later by raising `WEB_CONCURRENCY`, adding a Redis (Render Key Value) `REDIS_URL` so rate limits and WebSocket fan-out are shared, and optionally a separate worker service (`python -m scripts.run_worker`, `EMBEDDED_WORKER=false` on the web service).

## Production checklist
- [ ] `ENVIRONMENT=production`, strong `JWT_SECRET` (the app refuses to start otherwise), `/api/v1/diagnostics` shows no `production_problems`
- [ ] Plan is not `free`; HTTPS only (Render terminates TLS; HSTS header is sent)
- [ ] Supabase: session-pooler URL, TLS on, `menu-images` bucket is public, service-role key stored only in Render
- [ ] Admin bootstrapped; bootstrap env vars deleted; Admin password stored in a password manager
- [ ] **Tax rates verified by the owner/accountant**; `prices_include_tax` chosen deliberately; invoice prefix/GSTIN set
- [ ] Razorpay/Cashfree LIVE keys + webhooks registered; one real small payment and one refund tested end-to-end per gateway
- [ ] Firebase service account set; test push received on Admin and Rider phones with the app closed; battery-optimisation exemption done on those phones
- [ ] Android: `new_orders` channel HIGH importance + sound; Do-Not-Disturb exception granted on the Admin/Rider phones
- [ ] Google OAuth client IDs set (Android SHA-1 of the release key)
- [ ] CORS origins limited to the QR website; `RATE_LIMIT_ENABLED=true`
- [ ] Opening hours, delivery area/charges, gateways, riders, tables + printed QR codes configured
- [ ] Backups enabled and a restore rehearsed (below); uptime monitor on `/ready`; log drain/alerts configured
- [ ] Privacy notice in the apps (phone number, location of riders during delivery, push notifications); retention values reviewed (`location_retention_days`)

## Logging & monitoring
* JSON logs to stdout with a per-request `request_id` (also returned as `X-Request-ID`); Render keeps/streams them. Failures of jobs, webhooks and gateway calls are logged with stack traces; secrets and OTPs are never logged.
* `/health` (liveness), `/ready` (DB), `/api/v1/diagnostics` (job counts: look for a growing `failed`/`queued`), `GET /admin/jobs?status=failed`, `GET /admin/webhook-events?status=failed`, `GET /admin/audit-logs`.
* Suggested alerts: `/ready` non-200; failed jobs > 0 for 10 min; `payment.mismatch` events; refund `failed` events.

## Backup & recovery
* **Data:** all state is in Supabase PostgreSQL (images in Supabase Storage). Free projects have no point-in-time recovery; for a live cafe use a paid Supabase plan with daily backups/PITR, **and** keep your own logical dump: `pg_dump "$DIRECT_DB_URL" -Fc -f crescent_$(date +%F).dump` from a scheduled job (GitHub Action/cron) stored outside Supabase, retained ≥30 days. Also periodically copy the `menu-images` bucket.
* **Restore rehearsal (do this before launch):** create an empty Supabase project → `pg_restore --no-owner -d "$NEW_URL" crescent_YYYY-MM-DD.dump` → point `DATABASE_URL` at it → `alembic upgrade head` (no-op if current) → `GET /ready` → sign in as Admin.
* **Disaster scenarios:** Render service lost → redeploy from GitHub (stateless; env vars are in the dashboard/blueprint). Lost Admin password → shell on the server: `python -m scripts.bootstrap_admin --reset-password` (needs deployment access). Leaked JWT secret → rotate `JWT_SECRET` (signs everyone out). Leaked table QR → `POST /admin/tables/{id}/qr/revoke` or regenerate. Payment webhook outage → the reconcile job (every minute) pulls status from the providers, so paid orders are still picked up.
* **Rolling back a deploy:** Alembic downgrade is supported for the initial migration; for later migrations write and test the `downgrade()` before shipping, and prefer additive migrations.

## Data retention (defaults, adjustable)
Rider GPS points: 7 days (`location_retention_days`). Expired/revoked sessions and notification logs: 30/60 days. Finished jobs: 7 days. Delivery OTP rows: 30 days. Orders, payments, invoices and audit logs are kept (accounting); customer PII is anonymised on account deletion.
