import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.db.session import init_db
from app.api.routes import router
from app.api.telegram_routes import router as tg_router
from app.api.leads_routes import router as leads_router
from app.services.ignore_followup import run_ignore_followup_worker
from app.services.health_monitor import run_health_monitor
from app.services.telegram.client_manager import startup_all_clients, shutdown_all_clients
from app.services.telegram.health_monitor import run_tg_health_monitor

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth middleware — guards all /api/* routes at the application level.
# Routes that must stay public (health, webhooks) are explicitly excluded.
# This acts as a safety net even if a new route is added without the
# per-route `require_api_key` dependency.
# ---------------------------------------------------------------------------

_PUBLIC_PREFIXES = ("/health", "/webhook/", "/docs", "/openapi.json", "/redoc", "/dashboard")


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Allow public paths through without auth
        if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        # All other paths require a valid API key
        key = request.headers.get("x-api-key", "")
        if key != settings.API_SECRET_KEY:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting WR WhatsApp + Telegram Service...")
    await init_db()
    logger.info("Database initialized")

    # Start background workers
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(run_ignore_followup_worker(stop_event))
    health_task = asyncio.create_task(run_health_monitor(stop_event))

    # Connect all authorized Telegram accounts and start TG health monitor
    await startup_all_clients()
    tg_health_task = asyncio.create_task(run_tg_health_monitor(stop_event))

    yield

    stop_event.set()
    for task in (worker_task, health_task, tg_health_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await shutdown_all_clients()
    logger.info("Shutting down WR WhatsApp + Telegram Service")


app = FastAPI(
    title="WR WhatsApp Service",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(ApiKeyMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(tg_router)
app.include_router(leads_router)


@app.get("/dashboard", include_in_schema=False)
async def serve_dashboard():
    path = os.path.join(os.path.dirname(__file__), "app", "static", "dashboard.html")
    return FileResponse(path, media_type="text/html")


@app.get("/dashboard/config", include_in_schema=False)
async def dashboard_config():
    """Returns the API key so the dashboard JS can auth without a login form."""
    return {"api_key": settings.API_SECRET_KEY}


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=settings.DEBUG,
    )
