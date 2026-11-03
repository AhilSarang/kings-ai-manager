"""FastAPI app: webhooks and health endpoints."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import __version__
from app.config import get_settings
from app.db.session import db_ping, session_scope
from app.api.gmail_push import router as gmail_router
from app.api.whatsapp import router as whatsapp_router
from app.logging_setup import configure_logging
from app.services import health as health_svc
from app.services.monitor import watch_worker
from app.services.users import ensure_owner

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        with session_scope() as session:
            ensure_owner(session)
    except Exception:
        log.exception("owner seeding failed; is the database migrated?")
    watchdog = None
    if get_settings().app_env != "test":
        watchdog = asyncio.create_task(watch_worker())
    yield
    if watchdog:
        watchdog.cancel()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title="Kings AI Manager", version=__version__, docs_url=None, redoc_url=None,
                  lifespan=lifespan)

    app.include_router(whatsapp_router)
    app.include_router(gmail_router)

    @app.get("/health")
    def health() -> dict:
        """Liveness: the process is up."""
        return {"status": "ok", "version": __version__}

    @app.get("/health/ready")
    def ready() -> JSONResponse:
        """Readiness: dependencies reachable."""
        db_ok = db_ping()
        worker_ok = None
        if db_ok:
            with session_scope() as session:
                worker_ok = not health_svc.worker_is_stale(session)
        status = "ok" if db_ok and worker_ok else "degraded"
        body = {"status": status, "database": db_ok, "worker": worker_ok}
        return JSONResponse(body, status_code=200 if db_ok else 503)

    log.info("api started", extra={"env": settings.app_env, "llm_backend": settings.llm_backend})
    return app


app = create_app()
