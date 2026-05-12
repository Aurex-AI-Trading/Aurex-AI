"""
Aurex AI — Paper Trade Shadow Engine  (Phase 12)

Simulates trades in parallel with the live engine using live market data
but NEVER sending MT5 orders.  Generates PAPER_AI dataset for accelerated
AI research without any capital risk.

Design principles
-----------------
  - All real execution filters and scoring are respected (same thresholds).
  - Shadow trades coexist with live AI_AUTO trades simultaneously.
  - Resolution uses live bid/ask from the MT5 bridge on each scan cycle.
  - Completed paper trades are persisted via TradeLogger.log_paper_trade().
  - Paper trades expire after `resolution_hours` if SL/TP never hit.

Modes
-----
  OFF       — shadow engine disabled (default when not configured)
  PARALLEL  — shadow trades run alongside live AI_AUTO trades
  FULL_SIM  — shadow only; used for pure research runs (no live orders)

Logs emitted
------------
  [PAPER TRADE] OPENED  — new simulated position
  [PAPER TRADE] WIN/LOSS/EXPIRED — position resolved
  [PAPER STATS]         — periodic open-trade summary
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from aurex_ai.core.logger import get_logger
from aurex_ai.core.trade_source import PAPER_AI

log = get_logger("execution.shadow_engine")

MODE_OFF      = "OFF"
MODE_PARALLEL = "PARALLEL"
MODE_FULL_SIM = "FULL_SIM"

_DEFAULT_RESOLUTION_HOURS = 8
_DEFAULT_MAX_OPEN         = 5
_DEFAULT_MIN_CONFIDENCE   = 55.0


@dataclass
class PaperTrade:
    """In-memory representation of one open paper trade."""
    symbol:           str
    direction:        str
    tier:             int
    setup_type:       str
    confidence:       float
    entry_price:      float
    sl_price:         float
    tp_price:         float
    lot_size:         float
    atr_pips:         float
    utc_hour:         int
    session:          str
    market_state:     str
    opened_at:        str
    strategy_version: str  = "AI_FORWARD_V2"
    result:           str  = "OPEN"
    closed_at:        str  = ""
    pnl_r:            float = 0.0
    mae:              float = 0.0
    mfe:              float = 0.0
    duration_mins:    int   = 0


class ShadowEngine:
    """
    Singleton paper-trade shadow executor.

    Usage:
        se = ShadowEngine.get_instance(cfg)   # call once with cfg at startup
        se = ShadowEngine.get_instance()      # subsequent calls return same instance

        se.open_paper_trade(symbol, direction, ...)
        resolved = se.resolve_open_trades(bridge, trade_logger)
    """
    _instance: Optional["ShadowEngine"] = None

    def __init__(self, cfg=None) -> None:
        self._lock              = threading.Lock()
        self._open: List[PaperTrade] = []
        self._mode              = MODE_OFF
        self._max_open          = _DEFAULT_MAX_OPEN
        self._min_confidence    = _DEFAULT_MIN_CONFIDENCE
        self._resolution_hours  = _DEFAULT_RESOLUTION_HOURS
        if cfg is not None:
            self._configure(cfg)

    @classmethod
    def get_instance(cls, cfg=None) -> "ShadowEngine":
        if cls._instance is None:
            cls._instance = cls(cfg)
        elif cfg is not None:
            cls._instance._configure(cfg)
        return cls._instance

    def _configure(self, cfg) -> None:
        pt = getattr(cfg, "paper_trading", None)
        if pt is None:
            return
        raw_mode = str(getattr(pt, "mode", MODE_OFF)).upper()
        self._mode             = raw_mode if raw_mode in (MODE_OFF, MODE_PARALLEL, MODE_FULL_SIM) else MODE_OFF
        self._max_open         = int(getattr(pt, "max_open",          _DEFAULT_MAX_OPEN))
        self._min_confidence   = float(getattr(pt, "min_confidence",  _DEFAULT_MIN_CONFIDENCE))
        self._resolution_hours = int(getattr(pt, "resolution_hours",  _DEFAULT_RESOLUTION_HOURS))
        if self._mode != MODE_OFF:
            log.warning(
                "[PAPER TRADE] Shadow engine activated | mode=%s max_open=%d "
                "min_conf=%.0f resolution=%dh",
                self._mode, self._max_open, self._min_confidence, self._resolution_hours,
            )

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def is_active(self) -> bool:
        return self._mode != MODE_OFF

    # ── Open ──────────────────────────────────────────────────────────────────

    def open_paper_trade(
        self,
        symbol:       str,
        direction:    str,
        tier:         int,
        setup_type:   str,
        confidence:   float,
        entry_price:  float,
        sl_price:     float,
        tp_price:     float,
        lot_size:     float = 1.0,
        atr_pips:     float = 0.0,
        utc_hour:     int   = 0,
        session:      str   = "",
        market_state: str   = "",
        strategy_version: str = "AI_FORWARD_V2",
    ) -> bool:
        """
        Queue a new paper trade.

        Returns False when engine is OFF, capacity is full, or confidence
        is below the minimum threshold (same quality gate as live engine).
        """
        if not self.is_active:
            return False
        if confidence < self._min_confidence:
            return False

        with self._lock:
            if len(self._open) >= self._max_open:
                log.debug(
                    "[PAPER TRADE] Capacity full (%d/%d) — skipping %s %s",
                    len(self._open), self._max_open, symbol, direction,
                )
                return False

            pt = PaperTrade(
                symbol           = symbol,
                direction        = direction,
                tier             = tier,
                setup_type       = setup_type,
                confidence       = confidence,
                entry_price      = entry_price,
                sl_price         = sl_price,
                tp_price         = tp_price,
                lot_size         = lot_size,
                atr_pips         = atr_pips,
                utc_hour         = utc_hour,
                session          = session,
                market_state     = market_state,
                opened_at        = datetime.now(timezone.utc).isoformat(timespec="seconds"),
                strategy_version = strategy_version,
            )
            self._open.append(pt)

        log.warning(
            "[PAPER TRADE] OPENED %s %s | tier=%d conf=%.0f "
            "entry=%.5f sl=%.5f tp=%.5f open=%d",
            symbol, direction, tier, confidence,
            entry_price, sl_price, tp_price, len(self._open),
        )
        return True

    # ── Resolve ───────────────────────────────────────────────────────────────

    def resolve_open_trades(self, bridge, trade_logger) -> int:
        """
        Check all open paper trades against the current bid/ask from `bridge`.
        Closes resolved trades and persists them via trade_logger.log_paper_trade().

        Returns count of trades resolved in this call.
        """
        if not self.is_active or not self._open:
            return 0

        now           = datetime.now(timezone.utc)
        resolved_count = 0

        with self._lock:
            still_open: List[PaperTrade] = []

            for pt in self._open:
                opened_dt  = datetime.fromisoformat(pt.opened_at.replace("Z", "+00:00"))
                age_hours  = (now - opened_dt).total_seconds() / 3600.0

                try:
                    tick = bridge.get_tick(pt.symbol)
                    if tick is None:
                        still_open.append(pt)
                        continue
                    bid     = float(getattr(tick, "bid", 0.0))
                    ask     = float(getattr(tick, "ask", 0.0))
                    current = (bid + ask) / 2.0
                except Exception:
                    still_open.append(pt)
                    continue

                result = None
                pnl_r  = 0.0

                if pt.direction == "BUY":
                    pt.mfe = max(pt.mfe, current - pt.entry_price)
                    pt.mae = max(pt.mae, pt.entry_price - current)
                    if current >= pt.tp_price:
                        result = "WIN"
                        pnl_r  = abs(pt.tp_price - pt.entry_price) / max(abs(pt.entry_price - pt.sl_price), 1e-8)
                    elif current <= pt.sl_price:
                        result = "LOSS"
                        pnl_r  = -1.0
                else:
                    pt.mfe = max(pt.mfe, pt.entry_price - current)
                    pt.mae = max(pt.mae, current - pt.entry_price)
                    if current <= pt.tp_price:
                        result = "WIN"
                        pnl_r  = abs(pt.entry_price - pt.tp_price) / max(abs(pt.sl_price - pt.entry_price), 1e-8)
                    elif current >= pt.sl_price:
                        result = "LOSS"
                        pnl_r  = -1.0

                # Expire positions that have been open too long without resolution
                if result is None and age_hours >= self._resolution_hours:
                    result = "EXPIRED"
                    pnl_r  = 0.0

                if result is not None:
                    pt.result        = result
                    pt.pnl_r         = round(pnl_r, 3)
                    pt.closed_at     = now.isoformat(timespec="seconds")
                    pt.duration_mins = int((now - opened_dt).total_seconds() / 60)
                    self._log_resolved(pt, trade_logger)
                    resolved_count += 1
                else:
                    still_open.append(pt)

            self._open = still_open

        return resolved_count

    def _log_resolved(self, pt: PaperTrade, trade_logger) -> None:
        log.warning(
            "[PAPER TRADE] %s %s %s | conf=%.0f rr=%.2f "
            "mae=%.5f mfe=%.5f dur=%dm",
            pt.result, pt.symbol, pt.direction,
            pt.confidence, pt.pnl_r, pt.mae, pt.mfe, pt.duration_mins,
        )
        try:
            trade_logger.log_paper_trade(
                symbol           = pt.symbol,
                direction        = pt.direction,
                tier             = pt.tier,
                setup_type       = pt.setup_type,
                confidence       = pt.confidence,
                entry_price      = pt.entry_price,
                sl_price         = pt.sl_price,
                tp_price         = pt.tp_price,
                lot_size         = pt.lot_size,
                atr_pips         = pt.atr_pips,
                utc_hour         = pt.utc_hour,
                session          = pt.session,
                market_state     = pt.market_state,
                opened_at        = pt.opened_at,
                closed_at        = pt.closed_at,
                result           = pt.result,
                pnl_r            = pt.pnl_r,
                mae              = pt.mae,
                mfe              = pt.mfe,
                duration_mins    = pt.duration_mins,
                strategy_version = pt.strategy_version,
            )
        except Exception as exc:
            log.debug("[PAPER TRADE] persist error: %s", exc)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        with self._lock:
            return {
                "mode":       self._mode,
                "open_count": len(self._open),
                "symbols":    [p.symbol for p in self._open],
            }

    def log_stats(self) -> None:
        stats = self.get_stats()
        if self.is_active:
            log.warning(
                "[PAPER STATS] mode=%s open=%d | %s",
                stats["mode"], stats["open_count"],
                ", ".join(stats["symbols"]) or "none",
            )
