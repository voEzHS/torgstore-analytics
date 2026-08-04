"""
TorgStore Analytics — FastAPI Backend
Все расчёты аналитики выполняются здесь.
Managed PostgreSQL (Render) — хранилище.
"""
import base64
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
import os

from backend.core.database import engine, Base
from backend.core.migrate import run_startup_migration
from backend.routers import (
    imports,
    sessions,
    analytics,
    sources,
    managers,
    settings,
    period_targets,
    overrides,
    products,
    invoices,
    discounts,
    decisions,
    decline_reasons,
)
from backend.routers import ai

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await run_startup_migration()
    except Exception:
        logging.getLogger("torgstore.migrate").exception(
            "Startup migration failed — check DATABASE_URL and the migrations/ directory"
        )
        raise
    yield


app = FastAPI(
    title="TorgStore Analytics API",
    version="1.1.0",
    description="Аналитическая система продаж TorgStore",
    lifespan=lifespan,
)

# CORS — по умолчанию открыт (локальная разработка).
# В проде задай ALLOWED_ORIGINS="https://example.com,https://foo.com" в env.
_allowed_origins_env = os.environ.get("ALLOWED_ORIGINS", "").strip()
_allowed_origins = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Basic Auth (опционально) ────────────────────────────────────────────
# Активируется, только если заданы ОБА BASIC_AUTH_USER и BASIC_AUTH_PASSWORD
# в env. Если не заданы — сайт работает как раньше, без авторизации
# (локальная разработка). На Render эти переменные обязательно задать —
# сайт содержит реальные ФИО, телефоны и выручку клиентов.
_BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER")
_BASIC_AUTH_PASSWORD = os.environ.get("BASIC_AUTH_PASSWORD")
_BASIC_AUTH_ENABLED = bool(_BASIC_AUTH_USER and _BASIC_AUTH_PASSWORD)
_UNPROTECTED_PATHS = {"/api/v1/health"}


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if not _BASIC_AUTH_ENABLED:
        return await call_next(request)
    if request.method == "OPTIONS" or request.url.path in _UNPROTECTED_PATHS:
        return await call_next(request)

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            user, _, pwd = decoded.partition(":")
        except Exception:
            user, pwd = "", ""
        if secrets.compare_digest(user, _BASIC_AUTH_USER) and secrets.compare_digest(pwd, _BASIC_AUTH_PASSWORD):
            return await call_next(request)

    return Response(
        status_code=401,
        content="Unauthorized",
        headers={"WWW-Authenticate": 'Basic realm="TorgStore Analytics"'},
    )

# Все API роуты с префиксом /api/v1/
app.include_router(imports.router,        prefix="/api/v1")
app.include_router(sessions.router,       prefix="/api/v1")
app.include_router(analytics.router,      prefix="/api/v1")
app.include_router(sources.router,        prefix="/api/v1")
app.include_router(managers.router,       prefix="/api/v1")
app.include_router(settings.router,       prefix="/api/v1")
app.include_router(period_targets.router, prefix="/api/v1")
app.include_router(overrides.router,      prefix="/api/v1")
app.include_router(products.router,       prefix="/api/v1")
app.include_router(invoices.router,        prefix="/api/v1")
app.include_router(discounts.router,      prefix="/api/v1")
app.include_router(decisions.router,      prefix="/api/v1")
app.include_router(decline_reasons.router, prefix="/api/v1")
app.include_router(ai.router,             prefix="/api/v1")

# Статика: фронтенд
if os.path.exists("frontend"):
    app.mount("/static", StaticFiles(directory="frontend"), name="static")

    @app.get("/")
    async def serve_frontend():
        return FileResponse("frontend/index.html")

@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "version": "1.1.0"}
