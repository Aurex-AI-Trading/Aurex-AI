"""
Aurex AI SaaS — MT5 Connection Routes
POST /mt5/connect
POST /mt5/disconnect
GET  /mt5/status
GET  /mt5/account
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.security import decrypt_credential, encrypt_credential
from app.models.mt5_account import MT5Account, MT5ConnectionStatus
from app.models.user import User
from app.schemas.mt5 import (
    MT5AccountOut,
    MT5ConnectRequest,
    MT5DisconnectResponse,
    MT5StatusOut,
)
from app.services.mt5_bridge_service import MT5BridgeService

router = APIRouter()


def _bridge_service() -> MT5BridgeService:
    return MT5BridgeService.get_instance()


@router.post("/connect", response_model=MT5AccountOut, status_code=status.HTTP_200_OK)
async def connect_mt5(
    payload: MT5ConnectRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    bridge_svc: MT5BridgeService = Depends(_bridge_service),
):
    """
    Connect user's MT5 account.
    Credentials are encrypted (AES-256) before storage.
    A live connection is attempted via the bridge service.
    """
    user_id = str(current_user.id)

    ok, error = await bridge_svc.connect_user(
        user_id=user_id,
        account_number=payload.account_number,
        password=payload.password,
        server=payload.server,
    )

    # Upsert MT5Account record
    result = await db.execute(
        select(MT5Account).where(MT5Account.user_id == current_user.id)
    )
    account = result.scalar_one_or_none()

    if account is None:
        account = MT5Account(user_id=current_user.id)
        db.add(account)

    account.account_number = payload.account_number
    account.encrypted_password = encrypt_credential(payload.password)
    account.server = payload.server
    account.connection_status = (
        MT5ConnectionStatus.CONNECTED if ok else MT5ConnectionStatus.ERROR
    )
    account.last_error = error
    if ok:
        from datetime import datetime, timezone
        account.last_connected_at = datetime.now(timezone.utc)
        account.reconnect_attempts = 0

    await db.flush()

    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error or "MT5 connection failed",
        )

    return account


@router.post("/disconnect", response_model=MT5DisconnectResponse)
async def disconnect_mt5(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    bridge_svc: MT5BridgeService = Depends(_bridge_service),
):
    user_id = str(current_user.id)
    await bridge_svc.disconnect_user(user_id)

    result = await db.execute(
        select(MT5Account).where(MT5Account.user_id == current_user.id)
    )
    account = result.scalar_one_or_none()
    if account:
        account.connection_status = MT5ConnectionStatus.DISCONNECTED

    return MT5DisconnectResponse(success=True, message="MT5 disconnected successfully")


@router.get("/status", response_model=MT5StatusOut)
async def mt5_status(
    current_user: User = Depends(get_current_user),
    bridge_svc: MT5BridgeService = Depends(_bridge_service),
):
    user_id = str(current_user.id)
    session = bridge_svc.get_user_session(user_id)
    account_info = await bridge_svc.get_account_info(user_id)

    return MT5StatusOut(
        user_id=user_id,
        account_number=session.account_number if session else None,
        server=session.server if session else None,
        connected=bridge_svc.is_connected(user_id),
        last_heartbeat=session.last_heartbeat if session else None,
        balance=account_info.get("balance") if account_info else None,
        equity=account_info.get("equity") if account_info else None,
        error=session.last_error if session else None,
    )


@router.get("/account", response_model=MT5AccountOut)
async def get_mt5_account(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(MT5Account).where(MT5Account.user_id == current_user.id)
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="No MT5 account configured")
    return account


# ── Live positions management ──────────────────────────────────────────────────

@router.get("/positions")
async def get_open_positions(
    current_user: User = Depends(get_current_user),
    bridge_svc: MT5BridgeService = Depends(_bridge_service),
):
    """Return all open MT5 positions for the current user's session."""
    import asyncio
    user_id = str(current_user.id)
    bridge = bridge_svc.get_user_bridge(user_id)
    if not bridge:
        return []
    try:
        positions = await asyncio.to_thread(bridge.get_open_positions)
        return positions or []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/positions/{ticket}/close")
async def close_position(
    ticket: int,
    current_user: User = Depends(get_current_user),
    bridge_svc: MT5BridgeService = Depends(_bridge_service),
):
    """Market-close an open position by ticket."""
    import asyncio
    user_id = str(current_user.id)
    bridge = bridge_svc.get_user_bridge(user_id)
    if not bridge:
        raise HTTPException(status_code=503, detail="MT5 not connected")
    ok = await asyncio.to_thread(bridge.close_position, ticket)
    if not ok:
        raise HTTPException(status_code=500, detail="close_position failed")
    return {"ok": True, "ticket": ticket}


@router.post("/positions/{ticket}/breakeven")
async def breakeven_position(
    ticket: int,
    current_user: User = Depends(get_current_user),
    bridge_svc: MT5BridgeService = Depends(_bridge_service),
):
    """Move SL to entry price for an open position."""
    import asyncio
    user_id = str(current_user.id)
    bridge = bridge_svc.get_user_bridge(user_id)
    if not bridge:
        raise HTTPException(status_code=503, detail="MT5 not connected")
    positions = await asyncio.to_thread(bridge.get_open_positions)
    pos = next((p for p in (positions or []) if p.get("ticket") == ticket), None)
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found")
    entry = pos.get("price_open", 0.0)
    ok = await asyncio.to_thread(bridge.modify_sl, ticket, entry)
    if not ok:
        raise HTTPException(status_code=500, detail="modify_sl failed")
    return {"ok": True, "ticket": ticket, "new_sl": entry}
