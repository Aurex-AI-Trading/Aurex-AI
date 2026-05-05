"""
Aurex AI — Manual Override API

FastAPI server for safe manual trade control.

Endpoints:
  GET  /api/trades           — list open positions (magic-filtered)
  POST /api/trade/close      — market-close a position by ticket
  POST /api/trade/partial    — partial-close a position by ratio
  POST /api/trade/breakeven  — move SL to entry price
  POST /api/system/pause     — toggle trading on/off

Start as background thread from run_live() via start_server(bridge).
Only positions owned by this bot's magic number are exposed or actionable.
"""
from __future__ import annotations

import threading
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from aurex_ai.core.logger import get_logger
from aurex_ai.core.mt5_bridge import MT5Bridge

log = get_logger("api.server")

app = FastAPI(title="Aurex AI Manual Override", version="1.0", docs_url="/docs")

_bridge: Optional[MT5Bridge] = None
_paused: bool = False


# ── Lifecycle helpers ─────────────────────────────────────────────────────────

def init(bridge: MT5Bridge) -> None:
    """Register the shared MT5Bridge instance before serving requests."""
    global _bridge
    _bridge = bridge


def is_paused() -> bool:
    """Return True when trading has been paused via the API."""
    return _paused


def start_server(bridge: MT5Bridge, host: str = "0.0.0.0", port: int = 8000) -> None:
    """Start the API server in a daemon background thread."""
    import uvicorn

    init(bridge)
    thread = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": app, "host": host, "port": port, "log_level": "warning"},
        daemon=True,
    )
    thread.start()
    log.warning("[API SERVER] manual override API started on http://%s:%d", host, port)


# ── Internal guards ───────────────────────────────────────────────────────────

def _require_bridge() -> MT5Bridge:
    if _bridge is None:
        raise HTTPException(status_code=503, detail="bridge not initialized")
    return _bridge


def _require_own_ticket(bridge: MT5Bridge, ticket: int) -> dict:
    """Return the position dict for ticket if it belongs to this bot, else 404."""
    for pos in bridge.get_open_positions():
        if pos["ticket"] == ticket:
            return pos
    raise HTTPException(
        status_code=404,
        detail=f"ticket {ticket} not found or not owned by this bot",
    )


# ── Request models ────────────────────────────────────────────────────────────

class CloseRequest(BaseModel):
    ticket: int


class PartialRequest(BaseModel):
    ticket: int
    ratio: float = Field(..., gt=0.0, lt=1.0, description="Fraction to close (0 < ratio < 1)")


class BreakevenRequest(BaseModel):
    ticket: int


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/trades")
def get_trades() -> list:
    """Return all open positions owned by this bot."""
    return _require_bridge().get_open_positions()


@app.post("/api/trade/close")
def close_trade(req: CloseRequest) -> dict:
    """Market-close a position by ticket number."""
    bridge = _require_bridge()
    pos = _require_own_ticket(bridge, req.ticket)
    log.warning(
        "[MANUAL CLOSE] ticket=%d symbol=%s direction=%s profit=%.2f",
        req.ticket, pos["symbol"], pos["type"], pos["profit"],
    )
    if not bridge.close_position(req.ticket):
        raise HTTPException(status_code=500, detail="close_position failed")
    return {"ok": True, "ticket": req.ticket}


@app.post("/api/trade/partial")
def partial_trade(req: PartialRequest) -> dict:
    """Close a fraction of a position.  ratio must be between 0 and 1 (exclusive)."""
    bridge = _require_bridge()
    pos    = _require_own_ticket(bridge, req.ticket)
    symbol = pos["symbol"]
    volume = pos["volume"]

    try:
        info     = bridge.get_symbol_info(symbol)
        vol_min  = float(info.get("volume_min",  0.01))
        vol_step = float(info.get("volume_step", 0.01))
    except Exception:
        vol_min, vol_step = 0.01, 0.01

    raw          = volume * req.ratio
    vol_to_close = round(round(raw / vol_step) * vol_step, 2)
    vol_to_close = max(vol_min, vol_to_close)

    if vol_to_close >= volume or (volume - vol_to_close) < vol_min:
        raise HTTPException(
            status_code=400,
            detail=f"ratio would leave remaining volume below minimum lot ({vol_min})",
        )

    log.warning(
        "[MANUAL PARTIAL] ticket=%d symbol=%s ratio=%.2f closing=%.2f of %.2f lots",
        req.ticket, symbol, req.ratio, vol_to_close, volume,
    )
    if not bridge.partial_close(req.ticket, vol_to_close):
        raise HTTPException(status_code=500, detail="partial_close failed")
    return {"ok": True, "ticket": req.ticket, "volume_closed": vol_to_close}


@app.post("/api/trade/breakeven")
def breakeven_trade(req: BreakevenRequest) -> dict:
    """Move SL to entry price.  No-op if SL is already at or better than entry."""
    bridge = _require_bridge()
    pos    = _require_own_ticket(bridge, req.ticket)
    entry  = pos["price_open"]
    sl     = pos["sl"]
    direction = pos["type"]

    if direction == "BUY"  and sl >= entry:
        return {"ok": True, "ticket": req.ticket, "note": "already at or better than BE"}
    if direction == "SELL" and sl <= entry:
        return {"ok": True, "ticket": req.ticket, "note": "already at or better than BE"}

    log.warning(
        "[MANUAL BE] ticket=%d symbol=%s direction=%s entry=%.5f",
        req.ticket, pos["symbol"], direction, entry,
    )
    if not bridge.modify_sl(req.ticket, new_sl=entry):
        raise HTTPException(status_code=500, detail="modify_sl failed")
    return {"ok": True, "ticket": req.ticket, "new_sl": entry}


@app.post("/api/system/pause")
def toggle_pause() -> dict:
    """Toggle trading on/off.  Trade management (BE/partial) continues while paused."""
    global _paused
    _paused = not _paused
    if _paused:
        log.warning("[SYSTEM PAUSED] new trades blocked via API")
    else:
        log.warning("[SYSTEM RESUMED] trading re-enabled via API")
    return {"ok": True, "trading_enabled": not _paused}
