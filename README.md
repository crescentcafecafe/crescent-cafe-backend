# Crescent Cafe Backend

FastAPI backend for the **Crescent Cafe customer ordering website, admin workspace and rider workspace**. The same API can also serve future Android/iOS clients.

**Stack:** FastAPI · Python 3.12 · SQLAlchemy 2 async · PostgreSQL 16 · Alembic · Pydantic v2 · authenticated WebSockets · optional Redis · Firebase Cloud Messaging · Razorpay · Cashfree · background job queue.

## Repository status

This archive is the supplied backend project with its documentation conflict repaired and web-client integration notes added. The application already contains API routers and services for authentication, customers, catalog, orders, payments, riders/deliveries, tracking, chat, notifications, invoices, reporting, settings, audit logs, WebSockets and health checks. It is not a claim that external providers have been live-tested: the provider adapters need sandbox credentials and end-to-end verification before launch.

## Minimum to go live

The `.env.example` has about 70 settings. Only these are needed to take real orders. Everything else is optional tuning with a safe default.

| Variable | Why |
|---|---|
| `ENVIRONMENT=production` | Turns on the production safety checks |
| `DATABASE_URL` | Your PostgreSQL database |
| `JWT_SECRET` | 64+ random characters. Sign-in is not safe without it |
| `PUBLIC_BASE_URL` | The public address of this API (payment webhooks need it) |
| `CORS_ORIGINS` | The address of your website, for example `https://order.yourcafe.in` |
| `ADMIN_BOOTSTRAP_EMAIL`, `ADMIN_BOOTSTRAP_PASSWORD` | Creates the first admin account (run `python -m scripts.bootstrap_admin` once) |
| `RESEND_API_KEY`, `EMAIL_FROM` | **Password reset emails.** Without the key no email is sent. `EMAIL_FROM` must use a domain you verified in Resend |
| `STORAGE_BACKEND=supabase` with `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_STORAGE_BUCKET` | Keeps menu photos after a restart (local storage is for development only) |
| `FRONTEND_URL` | Website address (https, no trailing slash). The Razorpay redirect-mode callback used by the Android app sends customers back here. Empty = `QR_SITE_URL` |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` **or** `CASHFREE_CLIENT_ID`, `CASHFREE_CLIENT_SECRET` | Only for the online payment provider you use. Cash orders need neither |

Friendlier password-reset code (optional): `PASSWORD_RESET_OTP_DIGITS=6` and `PASSWORD_RESET_OTP_MINUTES=10`.

## Local development (Windows)

1. Install Python 3.12+ and Docker Desktop.
2. Copy `.env.example` to `.env` and set a strong local `JWT_SECRET` plus the admin bootstrap email/password.
3. In PowerShell, from this folder:

```powershell
Copy-Item .env.example .env
# Edit .env: ADMIN_BOOTSTRAP_EMAIL, ADMIN_BOOTSTRAP_PASSWORD, and JWT_SECRET
# Start PostgreSQL and the API:
docker compose up --build -d db api
# Apply migrations and create the single admin account:
docker compose exec api alembic upgrade head
docker compose exec api python -m scripts.bootstrap_admin
```

Local API: `http://localhost:8000`
Swagger: `http://localhost:8000/docs`
OpenAPI JSON: `http://localhost:8000/openapi.json`
Health: `http://localhost:8000/health`

Alternative Python setup (requires a reachable PostgreSQL database):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
alembic upgrade head
python -m scripts.bootstrap_admin
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The Docker Compose file starts a local PostgreSQL 16 service and mounts a persistent local media volume. For local development, `STORAGE_BACKEND=local` is set by Compose. For hosted environments, use durable object storage such as Supabase Storage.

## Sample menu for visual testing

After migrations and admin bootstrap, the optional sample catalog can be added to the local database:

```powershell
docker compose exec api python -m scripts.seed_sample_menu
```

This inserts clearly named demo menu products only when they do not already exist. Demo items and zero-percent placeholder tax are **not** launch-ready commercial configuration. Replace the sample menu and set verified tax, opening hours, delivery charges and payment settings before taking real orders.

## Tests and verification

```powershell
python -m pip install -r requirements-dev.txt
pytest -q
python -m compileall -q app scripts migrations tests
```

The authoring environment used to prepare this archive did not have network access to install missing test dependencies, so a fresh full test run could not be completed here. Static Python compilation completed successfully. Run the full test suite in the local development environment or CI before deployment.

## Documentation

- `docs/API.md` — generated REST endpoint inventory and schema discovery.
- `docs/FRONTEND_INTEGRATION.md` — customer/admin/rider website integration contract and flows.
- `docs/WEBSOCKETS.md` — authenticated socket handshake, subscriptions, event envelope and reconnect requirements.
- `docs/ARCHITECTURE.md` — service boundaries, state machine and security model.
- `docs/DATABASE.md` — schema documentation.
- `docs/SETUP.md` — database, Google, Firebase, payments and storage configuration.
- `docs/DEPLOYMENT.md` — hosting and operational checklist.
- `docs/UPGRADES.md` — known limitations and required pre-launch work.

## Important pre-launch checks

- Run successful, failed and refund payment scenarios in Razorpay and Cashfree sandbox, including signed webhooks.
- Set and verify the cafe's actual tax rates; the bootstrap creates a **0% placeholder tax**.
- Configure production `JWT_SECRET`, HTTPS, exact CORS origins, durable storage, database backups and an always-on worker/API plan.
- Test Google OAuth, Firebase browser/native delivery, notification permissions and rider GPS on target devices.
- Phone numbers are normalized but not OTP-verified in this backend version. Guest customers do not receive FCM and cannot use chat. See `docs/UPGRADES.md` for other limitations.

No real secrets or production URLs belong in this repository or frontend build.
