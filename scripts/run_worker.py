"""Standalone worker (optional). By default the API process runs an embedded worker; use this when you scale to a separate worker
service (set EMBEDDED_WORKER=false on the web service). Safe to run multiple copies: jobs are claimed with FOR UPDATE SKIP LOCKED."""
import asyncio

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.services.jobs import worker_loop

if __name__ == "__main__":
    s = get_settings()
    setup_logging(s.log_level, s.log_json)
    asyncio.run(worker_loop())
