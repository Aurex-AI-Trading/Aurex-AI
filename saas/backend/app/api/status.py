"""
Aurex AI SaaS — System Status Route
GET /status  — health check (no auth required)
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("")
async def system_status():
    return JSONResponse({
        "status":  "operational",
        "service": "Aurex AI API",
        "version": "1.0.0",
        "time":    datetime.now(timezone.utc).isoformat(),
    })
