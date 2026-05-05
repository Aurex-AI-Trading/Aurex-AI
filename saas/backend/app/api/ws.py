"""
Aurex AI SaaS — WebSocket Real-Time Feed

WS /ws?token=<access_token>

Pushes account + trade status updates to authenticated clients.

Message types:
  status  — MT5 connection, balance, equity, open_trades count
  ping    — keepalive every 30s
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from jose import JWTError

from app.core.security import decode_token
from app.services.mt5_bridge_service import MT5BridgeService

log = logging.getLogger("aurex.saas.ws")
router = APIRouter()


async def _user_from_token(token: str) -> str | None:
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            return None
        return payload.get("sub")
    except JWTError:
        return None


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
):
    user_id = await _user_from_token(token)
    if not user_id:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    bridge_svc = MT5BridgeService.get_instance()
    log.info("WebSocket connected: user=%s", user_id)

    try:
        while True:
            account_info = await bridge_svc.get_account_info(user_id)

            open_count = 0
            bridge = bridge_svc.get_user_bridge(user_id)
            if bridge:
                try:
                    positions = await asyncio.to_thread(bridge.get_open_positions)
                    open_count = len(positions) if positions else 0
                except Exception:
                    pass

            await websocket.send_json({
                "type":        "status",
                "connected":   bridge_svc.is_connected(user_id),
                "balance":     account_info.get("balance") if account_info else None,
                "equity":      account_info.get("equity") if account_info else None,
                "open_trades": open_count,
                "timestamp":   datetime.now(timezone.utc).isoformat(),
            })

            await asyncio.sleep(30)

    except WebSocketDisconnect:
        log.info("WebSocket disconnected: user=%s", user_id)
    except Exception as exc:
        log.warning("WebSocket error: user=%s error=%s", user_id, exc)
