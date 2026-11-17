#!/bin/sh
# Container entrypoint: apply migrations, then serve. Safe to run on every deploy (alembic is idempotent).
set -e
if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  alembic upgrade head
fi
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*' --workers "${WEB_CONCURRENCY:-1}"
