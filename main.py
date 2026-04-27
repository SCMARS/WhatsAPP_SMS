import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.db.session import init_db
from app.api.routes import router
from app.api.telegram_routes import router as tg_router
from app.api.telegram_auth_routes import router as auth_router
from app.api.leads_routes import router as leads_router
from app.api.warmup_routes import router as warmup_router
from app.api.message_tracking_routes import router as msg_tracker_router
from app.services.ignore_followup import run_ignore_followup_worker
from app.services.health_monitor import run_health_monitor
from app.services.telegram.client_manager import startup_all_clients, shutdown_all_clients
from app.services.telegram.health_monitor import run_tg_health_monitor
from app.services.telegram.warmup_scheduler import run_warmup_scheduler
from app.services.telegram.message_tracker import run_message_tracker

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

class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Only /api/* routes require authentication; everything else is public
        # (static assets, SPA routes, webhooks, health, dashboard HTML)
        if not path.startswith("/api/"):
            return await call_next(request)

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
    # DISABLED: worker_task = asyncio.create_task(run_ignore_followup_worker(stop_event))
    worker_task = None
    health_task = asyncio.create_task(run_health_monitor(stop_event))

    # Connect all authorized Telegram accounts and start TG health monitor
    await startup_all_clients()
    tg_health_task = asyncio.create_task(run_tg_health_monitor(stop_event))
    warmup_task = asyncio.create_task(run_warmup_scheduler(stop_event))
        message_tracker_task = asyncio.create_task(run_message_tracker(stop_event))

    yield

    stop_event.set()
    for task in (worker_task, health_task, tg_health_task, warmup_task):
        if task:
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
app.include_router(auth_router)
app.include_router(leads_router)
app.include_router(warmup_router)
app.include_router(msg_tracker_router)


@app.get("/dashboard", include_in_schema=False)
async def serve_dashboard():
    path = os.path.join(os.path.dirname(__file__), "app", "static", "dashboard.html")
    return FileResponse(path, media_type="text/html")


@app.get("/telegram-auth", include_in_schema=False)
async def serve_telegram_auth():
    path = os.path.join(os.path.dirname(__file__), "app", "static", "telegram_auth.html")
    return FileResponse(path, media_type="text/html")


@app.get("/dashboard/config", include_in_schema=False)
async def dashboard_config():
    """Returns the API key so the dashboard JS can auth without a login form."""
    return {"api_key": settings.API_SECRET_KEY}


# ── React SPA ─────────────────────────────────────────────────────────────────
# Serve the built React frontend from /app/frontend/dist.
# Must be mounted AFTER all API routers so /api/* routes take priority.
_DIST = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.isdir(_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_react(full_path: str = ""):
        # Let /api /health /webhook /dashboard /telegram-auth pass through
        for prefix in ("/api", "/health", "/webhook", "/docs", "/openapi", "/redoc", "/dashboard", "/telegram-auth"):
            if full_path.startswith(prefix.lstrip("/")):
                from fastapi import HTTPException
                raise HTTPException(status_code=404)
        index = os.path.join(_DIST, "index.html")
        return FileResponse(index, media_type="text/html")


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=settings.DEBUG,
    )
