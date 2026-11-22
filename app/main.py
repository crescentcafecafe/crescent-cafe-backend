from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1 import api_router
from app.api.v1.health import router as health_router
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.logging import request_id_var, setup_logging
from app.db.session import dispose_engine
from app.websockets.manager import broker
from app.websockets.router import router as ws_router

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging(s.log_level, s.log_json)
    for problem in s.production_problems():
        log.error("CONFIG PROBLEM: %s", problem)
    if s.is_production and any("JWT_SECRET" in p for p in s.production_problems()):
        raise RuntimeError("Refusing to start in production with an insecure JWT_SECRET")
    await broker.start()
    stop = asyncio.Event()
    worker = None
    if s.embedded_worker:
        from app.services.jobs import worker_loop

        worker = asyncio.create_task(worker_loop(stop), name="embedded-worker")
    try:
        yield
    finally:
        stop.set()
        if worker:
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):
                pass
        await broker.stop()
        await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.app_name, version="1.0.0", lifespan=lifespan,
                  description="Backend for the Crescent Cafe Customer / Admin / Rider Android apps and QR ordering website.")
    install_error_handlers(app)
    if s.cors_origin_list:
        app.add_middleware(CORSMiddleware, allow_origins=s.cors_origin_list, allow_credentials=False,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                           allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Guest-Token", "X-Branch-Code"], max_age=600)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if s.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    app.include_router(health_router)
    app.include_router(api_router)
    app.include_router(ws_router, prefix="/api/v1")
    if s.storage_backend == "local":
        import os

        os.makedirs(s.local_media_dir, exist_ok=True)
        app.mount("/media", StaticFiles(directory=s.local_media_dir), name="media")
    return app


app = create_app()
