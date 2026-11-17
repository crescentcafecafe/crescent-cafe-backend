# Crescent Cafe Backend

Production-oriented backend for the **Customer**, **Admin** and **Rider** Android apps and the **QR ordering website**.
FastAPI · PostgreSQL (Supabase) · SQLAlchemy 2 (async) · Alembic · Pydantic v2 · WebSockets · FCM · Razorpay · Cashfree · Postgres-backed job queue · Docker · Render.

> Backend only: the Android apps and the QR website are separate projects that consume this API.

## What it does
* **One Admin** (bootstrapped by a protected CLI, never by public registration) runs everything: orders & kitchen workflow, tables & QR codes, menu (add products with photos, variants, add-ons, instant availability), payments (incl. cash confirmation and refunds), riders & manual assignment, live delivery tracking, chat inbox, offers & coupons, taxes/charges/hours, invoices and reports.
* **Customers** register, log in with email **or Google (Gmail)**, and must give a **mobile number** before ordering; **guests** can order via the QR site with name + mobile and a secure per-order token.
* **Four order types:** dine-in (QR), pickup, delivery, Admin walk-in. Several customers at one table = separate, separately billed orders linked to one table session.
* **Payments:** Razorpay, Cashfree (Admin can switch each on/off) and cash at counter (Admin confirms). Server-verified only; webhooks, reconciliation, idempotency, audited refunds.
* **Delivery:** manual rider assignment, accept/reject, live GPS (only during active deliveries), **OTP-only delivery completion**.
* **Realtime & push:** authenticated WebSockets + FCM (new-order and assignment alarms, chat, status updates).
* **Security-first:** Argon2, rotating refresh tokens, object-level authorisation, rate limits, signed webhooks, hashed QR/OTP/guest tokens, audit log.

## Quick start
```bash
cp .env.example .env                      # edit DATABASE_URL / JWT_SECRET
docker compose up --build                 # API http://localhost:8000/docs  (local Postgres included)
docker compose exec api python -m scripts.bootstrap_admin
```
or without Docker:
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql://USER:PASS@HOST:5432/DB     # Supabase session-pooler URL works as is
alembic upgrade head
python -m scripts.bootstrap_admin
uvicorn app.main:app --reload
```
Tests: `pytest` (SQLite, ~45 s) or `TEST_DATABASE_URL=postgresql+asyncpg://… pytest` (PostgreSQL: also runs the concurrency tests).

## Project layout
```
app/
  main.py                 app factory, middleware, lifespan (broker + embedded worker)
  core/                   config (env vars), security (Argon2/JWT/OTP), permissions, rate limit, errors, logging
  api/v1/                 routers: auth, customers, public (menu/qr/settings/offers), orders, payments(+webhooks), delivery (riders/deliveries/tracking),
                          chat, notifications, invoices, admin_ops, admin_catalog, admin_settings(+reports), health
  models/ schemas/        SQLAlchemy models (40 tables) / Pydantic request models
  services/               business logic: auth, orders, order_state, pricing, offers, payments, delivery, tables, chat, events, push, invoices, reports, jobs…
  integrations/           razorpay, cashfree, firebase (FCM), google_auth, storage (Supabase), registry (test fakes plug in here)
  websockets/             connection manager + Redis broker, authenticated /ws endpoint
  workers/handlers.py     background jobs (push, refunds, reconciliation, expiry, cleanup, invoices…)
migrations/               Alembic (async env); scripts/ bootstrap_admin, run_worker, start.sh, doc generators
tests/                    162 tests + 1 docs generator (auth, catalog/QR, orders/state machine, payments, delivery/OTP/tracking, chat/FCM, WebSockets, offers, invoices/reports, units)
docs/                     ARCHITECTURE, API (generated), DATABASE (generated), WEBSOCKETS, SETUP, DEPLOYMENT, EXAMPLES (real captured responses), REPORTING, UPGRADES
Dockerfile, docker-compose.yml, render.yaml, .github/workflows/ci.yml, .env.example
```

## Documentation
| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | architecture, state machine, API module plan, security model |
| [docs/DATABASE.md](docs/DATABASE.md) | every table, relationship, constraint and index |
| [docs/API.md](docs/API.md) · `/docs` | all 144 endpoints (generated) · interactive Swagger |
| [docs/EXAMPLES.md](docs/EXAMPLES.md) | real request/response examples for the main flows |
| [docs/WEBSOCKETS.md](docs/WEBSOCKETS.md) | topics, event catalogue, what the Android apps must do |
| [docs/SETUP.md](docs/SETUP.md) | Supabase, Firebase, Google, Razorpay, Cashfree, first-run cafe configuration |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Render deploy, production checklist, logging, backup & recovery |
| [docs/REPORTING.md](docs/REPORTING.md) | exact report definitions |
| [docs/UPGRADES.md](docs/UPGRADES.md) | **must-do items before launch, known limitations, recommended upgrades** |

## Status & honesty notes
* All **162 automated tests pass on PostgreSQL 16**, and 159 on SQLite with 3 PostgreSQL-only concurrency tests skipped. A real server was also run end-to-end (migrate → bootstrap → serve → login).
* Razorpay, Cashfree, Firebase, Google and Supabase Storage are tested **with fakes** (plus real signature/parsing code). Run sandbox checks before go-live (docs/SETUP.md, docs/UPGRADES.md §A).
* Docker/Render files were written but not built in the authoring environment; CI builds the image.
* The default tax rate is **0 %** until the owner sets it; the Admin dashboard warns until configured.
