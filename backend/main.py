"""
TorgStore Analytics — FastAPI Backend
Все расчёты аналитики выполняются здесь.
Managed PostgreSQL (Render) — хранилище.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
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

# CORS — разрешаем запросы от фронтенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # в проде заменить на конкретный домен
    allow_methods=["*"],
    allow_headers=["*"],
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
