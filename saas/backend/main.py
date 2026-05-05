"""
Aurex AI SaaS — FastAPI Application Entry Point
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.api import admin, analytics, auth, mt5, status, trades, users, ws
from app.core.config import settings
from app.core.database import engine
from app.models import (  # noqa: F401 — ensure all models are imported before create_all
    AnalyticsSnapshot, MT5Account, Signal, Trade, User,
)
from app.services.mt5_bridge_service import MT5BridgeService

log = logging.getLogger("aurex.saas")


# ── Application lifespan ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    from app.core.database import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("Database tables ensured")

    bridge_svc = MT5BridgeService.get_instance()
    await bridge_svc.start()
    log.info("MT5BridgeService started")

    await _seed_admin_if_needed()

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    await MT5BridgeService.get_instance().stop()
    await engine.dispose()
    log.info("Aurex AI SaaS shut down cleanly")


async def _seed_admin_if_needed() -> None:
    """Create the first admin user if no admin exists (idempotent)."""
    from sqlalchemy import select
    from app.core.database import AsyncSessionLocal
    from app.core.security import get_password_hash
    from app.models.user import User, UserRole

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.role == UserRole.ADMIN))
        if result.scalar_one_or_none():
            return

        admin = User(
            email=settings.FIRST_ADMIN_EMAIL,
            hashed_password=get_password_hash(settings.FIRST_ADMIN_PASSWORD),
            full_name="Platform Admin",
            role=UserRole.ADMIN,
            is_active=True,
            is_verified=True,
        )
        db.add(admin)
        await db.commit()
        log.warning("Seeded first admin: %s", settings.FIRST_ADMIN_EMAIL)


# ── Rate limiter ──────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)


# ── App factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Aurex AI API",
    description="Intelligent Algorithmic Trading Platform",
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(status.router,    prefix="/status",    tags=["Health"])
app.include_router(auth.router,      prefix="/auth",      tags=["Auth"])
app.include_router(users.router,     prefix="/users",     tags=["Users"])
app.include_router(mt5.router,       prefix="/mt5",       tags=["MT5"])
app.include_router(trades.router,    prefix="/trades",    tags=["Trades"])
app.include_router(analytics.router, prefix="/analytics", tags=["Analytics"])
app.include_router(admin.router,     prefix="/admin",     tags=["Admin"])
app.include_router(ws.router,                         tags=["WebSocket"])
