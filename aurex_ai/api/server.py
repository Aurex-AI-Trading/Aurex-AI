"""
Aurex AI — Manual Override API  (Phase 9: Production-grade port management)

FastAPI server for safe manual trade control during live operation.

Endpoints:
  GET  /health               — health check (unauthenticated; for monitoring)
  GET  /api/trades           — list open positions (magic-filtered)
  POST /api/trade/close      — market-close a position by ticket
  POST /api/trade/partial    — partial-close a position by ratio
  POST /api/trade/breakeven  — move SL to entry price
  POST /api/system/pause     — toggle trading on/off
  GET  /api/system/status    — engine status, pause state, open count

Port / config resolution order (first match wins):
  1. cfg.api section  (settings.yaml  api: { host, port, enabled, auto_port })
  2. Environment vars (API_HOST, API_PORT, API_ENABLED)
  3. Hard-coded defaults: host=0.0.0.0  port=8001  enabled=true

Default port is 8001 (intentionally avoids the SaaS backend which uses 8000).

Startup guarantees:
  - Single-instance guard: only one uvicorn thread can ever run.
  - Port-in-use detection before binding; auto-increments to next free port.
  - Startup probe confirms the port is bound before emitting [API READY].
  - Trading engine continues normally even if the API fails to start.

Shutdown:
  - stop_server() sets uvicorn.Server.should_exit = True for a clean exit.
  - Daemon-thread fallback: thread is killed when the main process exits.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from aurex_ai.core.logger import get_logger
from aurex_ai.core.mt5_bridge import MT5Bridge

log = get_logger("api.server")

# ── FastAPI application ───────────────────────────────────────────────────────
app = FastAPI(
    title       = "Aurex AI Manual Override",
    version     = "1.0",
    description = "Live trade control API for Aurex AI institutional trading engine",
    docs_url    = "/docs",
    redoc_url   = None,
)

# ── Module-level state ────────────────────────────────────────────────────────
_bridge:         Optional[MT5Bridge] = None
_paused:         bool                = False
_server_started: threading.Event     = threading.Event()
_uvicorn_server                      = None   # uvicorn.Server — kept for clean shutdown


# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_HOST      = "0.0.0.0"
_DEFAULT_PORT      = 8001   # avoids clash with SaaS backend (8000)
_DEFAULT_ENABLED   = True
_DEFAULT_AUTO_PORT = True   # auto-increment when preferred port is busy
_PORT_SCAN_LIMIT   = 20     # max ports to try before giving up


# ── Port utilities ────────────────────────────────────────────────────────────

def is_port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    """
    Return True if the given host:port is already bound.

    Uses a TCP connect probe.  0.0.0.0 is mapped to 127.0.0.1 for the check
    because you cannot connect_ex to 0.0.0.0 on Windows.
    """
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((probe_host, port)) == 0


def find_free_port(host: str, preferred: int, max_tries: int = _PORT_SCAN_LIMIT) -> int:
    """
    Return the first free port in [preferred, preferred + max_tries).

    Logs a warning for each busy port skipped.
    Raises RuntimeError if no free port is found within the scan range.
    """
    for candidate in range(preferred, preferred + max_tries):
        if not is_port_in_use(host, candidate):
            return candidate
        log.warning(
            "[API PORT CHECK] Port %d busy — trying %d",
            candidate, candidate + 1,
        )
    raise RuntimeError(
        f"No free port found in range {preferred}–{preferred + max_tries - 1}. "
        "Check for stale processes or increase the scan range."
    )


# ── Config resolution ─────────────────────────────────────────────────────────

def _resolve_config(cfg=None) -> tuple[str, int, bool, bool]:
    """
    Return (host, port, enabled, auto_port) from the config hierarchy.

    Priority:
      1. cfg.api  — settings.yaml api: section (runtime object)
      2. Env vars — API_HOST / API_PORT / API_ENABLED / API_AUTO_PORT
      3. Defaults — 0.0.0.0 / 8001 / true / true
    """
    host      = _DEFAULT_HOST
    port      = _DEFAULT_PORT
    enabled   = _DEFAULT_ENABLED
    auto_port = _DEFAULT_AUTO_PORT

    # Layer 1: settings.yaml api: section
    if cfg is not None:
        api_cfg = getattr(cfg, "api", None)
        if api_cfg is not None:
            host      = str( getattr(api_cfg, "host",      host))
            port      = int( getattr(api_cfg, "port",      port))
            enabled   = bool(getattr(api_cfg, "enabled",   enabled))
            auto_port = bool(getattr(api_cfg, "auto_port", auto_port))

    # Layer 2: environment variables
    host      = os.environ.get("API_HOST",      host)
    port      = int(os.environ.get("API_PORT",  port))
    enabled   = os.environ.get("API_ENABLED",   str(enabled)).lower() not in ("0", "false", "no")
    auto_port = os.environ.get("API_AUTO_PORT", str(auto_port)).lower() not in ("0", "false", "no")

    return host, port, enabled, auto_port


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def init(bridge: MT5Bridge) -> None:
    """Register the shared MT5Bridge instance before serving requests."""
    global _bridge
    _bridge = bridge


def is_paused() -> bool:
    """Return True when trading has been paused via the API."""
    return _paused


def stop_server() -> None:
    """
    Signal the uvicorn server to exit gracefully.

    Sets uvicorn.Server.should_exit = True so the server drains active
    connections and releases the port.  Safe to call if the server never
    started (no-op).
    """
    global _uvicorn_server
    if _uvicorn_server is not None:
        try:
            _uvicorn_server.should_exit = True
            log.info("[API SERVER] Shutdown signal sent — uvicorn exiting.")
        except Exception as exc:
            log.warning("[API SERVER] Error sending shutdown signal: %s", exc)
    _server_started.clear()


def _probe_startup(host: str, port: int, timeout: float = 5.0) -> None:
    """
    Non-blocking startup probe: logs [API READY] once the port is confirmed bound.

    Runs in a short-lived daemon thread so the trading engine is never blocked.
    """
    def _check() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if is_port_in_use(host, port):
                log.warning(
                    "[API READY] API operational on port %d | docs=http://127.0.0.1:%d/docs",
                    port, port,
                )
                return
            time.sleep(0.25)
        log.warning(
            "[API SERVER] Port %d not confirmed bound after %.1fs — "
            "uvicorn may have failed. Check logs for errors.",
            port, timeout,
        )

    threading.Thread(target=_check, name="aurex-api-probe", daemon=True).start()


def start_server(bridge: MT5Bridge, cfg=None) -> bool:
    """
    Start the manual override API in a daemon background thread.

    Returns True if the server was started, False if it was already running,
    disabled via config, or failed to find a free port.

    The trading engine continues normally on any failure — this function
    never raises.
    """
    import uvicorn  # lazy import — not installed in all environments

    global _uvicorn_server

    # ── Single-instance guard ─────────────────────────────────────────────────
    if _server_started.is_set():
        log.warning("[API SERVER] Already running — skipping duplicate startup.")
        return False

    # ── Resolve host / port / enabled from config hierarchy ──────────────────
    host, preferred_port, enabled, auto_port = _resolve_config(cfg)

    if not enabled:
        log.warning(
            "[API SERVER] Disabled via config (api.enabled=false) — API will not start. "
            "Trading continues normally."
        )
        return False

    log.warning(
        "[API SERVER] Starting | preferred=http://%s:%d auto_port=%s",
        host, preferred_port, auto_port,
    )

    # ── Port conflict detection ───────────────────────────────────────────────
    try:
        if auto_port:
            port = find_free_port(host, preferred_port)
        else:
            if is_port_in_use(host, preferred_port):
                log.error(
                    "[API SERVER] Port %d is already in use and auto_port=false. "
                    "Change api.port in settings.yaml or set API_PORT env var. "
                    "Trading continues normally.",
                    preferred_port,
                )
                return False
            port = preferred_port

        if port != preferred_port:
            log.warning(
                "[API PORT CHECK] Port %d busy — switched to %d",
                preferred_port, port,
            )
        else:
            log.info("[API PORT CHECK] Port %d is free.", port)

    except RuntimeError as exc:
        log.error(
            "[API SERVER] Cannot find a free port: %s — API will not start. "
            "Trading continues normally.",
            exc,
        )
        return False

    # ── Register bridge ───────────────────────────────────────────────────────
    init(bridge)

    # ── Build uvicorn.Server (module-level ref enables clean shutdown) ────────
    uvi_cfg = uvicorn.Config(
        app       = app,
        host      = host,
        port      = port,
        log_level = "warning",
        # Disable access log noise in production; errors still surface
        access_log = False,
    )
    _uvicorn_server = uvicorn.Server(uvi_cfg)

    def _run() -> None:
        global _uvicorn_server
        try:
            log.warning("[API SERVER] Uvicorn starting on http://%s:%d", host, port)
            _uvicorn_server.run()
            log.info("[API SERVER] Uvicorn exited cleanly.")
        except Exception as exc:
            log.exception("[API SERVER] Uvicorn crashed: %s — trading unaffected.", exc)
        finally:
            _server_started.clear()
            _uvicorn_server = None

    thread = threading.Thread(target=_run, name="aurex-api", daemon=True)
    thread.start()
    _server_started.set()

    log.warning("[API SERVER] Host=%s Port=%d | thread started.", host, port)

    # Non-blocking probe: logs [API READY] once uvicorn is confirmed bound
    _probe_startup(host, port)

    return True


# ── Internal guards ───────────────────────────────────────────────────────────

def _require_bridge() -> MT5Bridge:
    if _bridge is None:
        raise HTTPException(status_code=503, detail="Bridge not initialized — trading engine starting up.")
    return _bridge


def _require_own_ticket(bridge: MT5Bridge, ticket: int) -> dict:
    """Return the position dict for ticket if it belongs to this bot, else 404."""
    for pos in bridge.get_open_positions():
        if pos["ticket"] == ticket:
            return pos
    raise HTTPException(
        status_code=404,
        detail=f"Ticket {ticket} not found or not owned by this bot (wrong magic number?).",
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

@app.get("/health", tags=["Health"])
def health_check() -> dict:
    """Unauthenticated health check — for uptime monitors and load balancers."""
    return {
        "status":   "ok",
        "service":  "aurex-ai-override-api",
        "paused":   _paused,
        "bridge":   _bridge is not None,
    }


@app.get("/api/system/status", tags=["System"])
def system_status() -> dict:
    """Return engine pause state and open position count."""
    bridge = _require_bridge()
    return {
        "trading_enabled": not _paused,
        "open_positions":  len(bridge.get_open_positions()),
    }


@app.get("/api/trades", tags=["Trades"])
def get_trades() -> list:
    """Return all open positions owned by this bot."""
    return _require_bridge().get_open_positions()


@app.post("/api/trade/close", tags=["Trades"])
def close_trade(req: CloseRequest) -> dict:
    """Market-close a position by ticket number."""
    bridge = _require_bridge()
    pos    = _require_own_ticket(bridge, req.ticket)
    log.warning(
        "[MANUAL CLOSE] ticket=%d symbol=%s direction=%s profit=%.2f",
        req.ticket, pos["symbol"], pos["type"], pos["profit"],
    )
    if not bridge.close_position(req.ticket):
        raise HTTPException(status_code=500, detail="close_position failed")
    return {"ok": True, "ticket": req.ticket}


@app.post("/api/trade/partial", tags=["Trades"])
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


@app.post("/api/trade/breakeven", tags=["Trades"])
def breakeven_trade(req: BreakevenRequest) -> dict:
    """Move SL to entry price.  No-op if SL is already at or better than entry."""
    bridge    = _require_bridge()
    pos       = _require_own_ticket(bridge, req.ticket)
    entry     = pos["price_open"]
    sl        = pos["sl"]
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


@app.post("/api/system/pause", tags=["System"])
def toggle_pause() -> dict:
    """Toggle trading on/off.  Trade management (BE/partial/trail) continues while paused."""
    global _paused
    _paused = not _paused
    if _paused:
        log.warning("[SYSTEM PAUSED] New trades blocked via API")
    else:
        log.warning("[SYSTEM RESUMED] Trading re-enabled via API")
    return {"ok": True, "trading_enabled": not _paused}
