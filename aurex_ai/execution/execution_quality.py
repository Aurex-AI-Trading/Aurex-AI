"""
Aurex AI — Execution Quality Engine  (Phase 6)

Monitors live trade execution quality and degrades trading activity when
broker conditions are poor.  Tracks three dimensions:

  1. Slippage
       Difference between expected entry price and actual executed price.
       Positive slippage = filled better than requested (rare but possible).
       Negative slippage = filled worse (adverse fill, fast market, wide spread).

  2. Spread stability
       Compares the current spread for each symbol against its rolling average.
       A spike (> spread_spike_ratio × average) flags [SPREAD INSTABILITY].

  3. Fill quality score  (0 – 100)
       Composite: 100 = perfect fills, 0 = every trade slipped badly.
       Falls with repeated adverse fills; rises when fills are clean.

The overall quality_score is fed into the scan loop so:
  • quality_score < min_quality_score → [EXECUTION HEALTH] warning logged
  • slippage_block_pips exceeded      → symbol temporarily back-off

All state is in-memory.  No persistence needed — quality is re-evaluated
fresh from the rolling window on each process restart.

Thread-safe.  Safe to call from async context via asyncio.to_thread().

Logs emitted
------------
    [EXECUTION QUALITY]    — periodic health summary
    [SLIPPAGE DETECTED]    — adverse fill recorded
    [SPREAD INSTABILITY]   — spread spike above threshold
    [LATENCY WARNING]      — cycle duration exceeded limit
    [EXECUTION HEALTH]     — overall score below threshold
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, Optional

from aurex_ai.core.logger import get_logger

log = get_logger("execution.quality")


@dataclass
class FillRecord:
    symbol:         str
    expected_price: float
    executed_price: float
    spread_pips:    float
    direction:      str       # "BUY" | "SELL"
    timestamp:      str       # ISO-8601


@dataclass
class QualityReport:
    quality_score:         float    # 0–100; < min_quality_score = degraded
    avg_slippage_pips:     float    # rolling average (negative = adverse)
    worst_slippage_pips:   float
    spread_instability:    bool
    fill_count:            int
    degraded:              bool     # True when quality_score below threshold
    reason:                str


class ExecutionQualityEngine:
    """
    Singleton execution quality tracker.

    Usage:
        eq = ExecutionQualityEngine.get_instance()
        eq.record_fill("XAUUSD", expected=2010.50, executed=2010.80,
                       spread_pips=0.8, direction="BUY")
        report = eq.get_report(cfg)
        spread_ok = eq.check_spread("EURUSD.Z", current_spread_pips=1.8, cfg=cfg)
    """

    _instance: Optional["ExecutionQualityEngine"] = None

    def __init__(self, window: int = 20) -> None:
        self._lock                        = threading.Lock()
        self._window                      = window
        self._fills:      Deque[FillRecord]         = deque(maxlen=window)
        # Per-symbol rolling spread history (last 20 readings)
        self._spreads:    Dict[str, Deque[float]]   = {}
        self._backoff:    Dict[str, bool]            = {}   # symbol → temporarily blocked

    @classmethod
    def get_instance(cls) -> "ExecutionQualityEngine":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Fill recording ────────────────────────────────────────────────────────

    def record_fill(
        self,
        symbol:         str,
        expected_price: float,
        executed_price: float,
        spread_pips:    float,
        direction:      str,
        pip:            float = 0.0001,
        cfg=None,
    ) -> None:
        """
        Record a completed fill.  Call immediately after trade execution.

        Computes slippage = (executed − expected) × direction_sign / pip_size.
        Positive = price moved favourably (slippage in our direction).
        Negative = adverse fill.
        """
        if expected_price <= 0 or executed_price <= 0:
            return

        # Slippage in pips; negative = adverse
        sign     = 1 if direction == "BUY" else -1
        pip_size = pip if pip > 0 else 0.0001
        slippage = round((executed_price - expected_price) * sign / pip_size, 1)

        record = FillRecord(
            symbol         = symbol,
            expected_price = expected_price,
            executed_price = executed_price,
            spread_pips    = spread_pips,
            direction      = direction,
            timestamp      = datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        with self._lock:
            self._fills.append(record)

        if cfg is not None:
            eq_cfg = getattr(cfg, "execution_quality", None)
            warn_thresh  = float(getattr(eq_cfg, "slippage_warn_pips",  2.0))
            block_thresh = float(getattr(eq_cfg, "slippage_block_pips", 5.0))
        else:
            warn_thresh, block_thresh = 2.0, 5.0

        if slippage < -warn_thresh:
            log.warning(
                "[SLIPPAGE DETECTED] [EXECUTION QUALITY] %s %s | expected=%.5f executed=%.5f "
                "slippage=%.1f pips (adverse)",
                symbol, direction, expected_price, executed_price, slippage,
            )
        elif slippage > 1.0:
            log.info(
                "[EXECUTION QUALITY] %s %s | slippage=+%.1f pips (positive fill)",
                symbol, direction, slippage,
            )

        # Activate backoff if rolling average slippage exceeds block threshold
        if slippage < -block_thresh:
            with self._lock:
                self._backoff[symbol] = True
            log.warning(
                "[EXECUTION QUALITY] [EXECUTION HEALTH] %s — slippage backoff activated "
                "| slippage=%.1f pips exceeds block=%.1f pips",
                symbol, slippage, block_thresh,
            )

    # ── Spread tracking ───────────────────────────────────────────────────────

    def record_spread(self, symbol: str, spread_pips: float, cfg=None) -> bool:
        """
        Record current spread for a symbol and check for instability.

        Returns True if spread is acceptable, False if a spike is detected.
        Call once per scan cycle per symbol.
        """
        if spread_pips <= 0:
            return True

        with self._lock:
            if symbol not in self._spreads:
                self._spreads[symbol] = deque(maxlen=20)
            self._spreads[symbol].append(spread_pips)
            history = list(self._spreads[symbol])

        if len(history) < 5:
            return True   # not enough data yet

        avg_spread = sum(history[:-1]) / (len(history) - 1)
        if avg_spread <= 0:
            return True

        eq_cfg       = getattr(cfg, "execution_quality", None) if cfg else None
        spike_ratio  = float(getattr(eq_cfg, "spread_spike_ratio", 2.5))

        ratio = spread_pips / avg_spread
        if ratio > spike_ratio:
            log.warning(
                "[SPREAD INSTABILITY] [EXECUTION QUALITY] %s | spread=%.1f pips "
                "vs avg=%.1f pips (%.1f×) [LATENCY WARNING]",
                symbol, spread_pips, avg_spread, ratio,
            )
            return False

        return True

    # ── Quality report ────────────────────────────────────────────────────────

    def get_report(self, cfg=None) -> QualityReport:
        """
        Compute and return a QualityReport from the rolling fill window.
        """
        eq_cfg       = getattr(cfg, "execution_quality", None) if cfg else None
        min_score    = float(getattr(eq_cfg, "min_quality_score", 40))
        warn_thresh  = float(getattr(eq_cfg, "slippage_warn_pips", 2.0))

        with self._lock:
            fills = list(self._fills)

        if not fills:
            return QualityReport(
                quality_score       = 100.0,
                avg_slippage_pips   = 0.0,
                worst_slippage_pips = 0.0,
                spread_instability  = False,
                fill_count          = 0,
                degraded            = False,
                reason              = "no_fills",
            )

        slippages = []
        for f in fills:
            sign     = 1 if f.direction == "BUY" else -1
            pip_size = 0.01 if "JPY" in f.symbol else (0.1 if "XAU" in f.symbol else 0.0001)
            slip     = (f.executed_price - f.expected_price) * sign / pip_size
            slippages.append(round(slip, 1))

        avg_slip   = round(sum(slippages) / len(slippages), 2)
        worst_slip = round(min(slippages), 2)   # most negative = worst

        # Quality score: 100 - penalty per adverse pip averaged over window
        adverse = [s for s in slippages if s < 0]
        penalty = (sum(abs(s) for s in adverse) / len(slippages)) * 10 if slippages else 0.0
        score   = max(0.0, round(100.0 - penalty, 1))

        spread_instab = any(
            q for q in self._spreads.values()
            if len(q) >= 5 and list(q)[-1] > (sum(list(q)[:-1]) / (len(list(q)) - 1)) * 2.0
        )

        degraded = score < min_score
        reason   = (
            f"avg_slip={avg_slip:.1f}pips worst={worst_slip:.1f}pips score={score:.0f}"
        )

        if degraded:
            log.warning(
                "[EXECUTION HEALTH] [EXECUTION QUALITY] quality_score=%.0f < %.0f | %s",
                score, min_score, reason,
            )
        else:
            log.info(
                "[EXECUTION QUALITY] quality_score=%.0f | %s fills=%d",
                score, reason, len(fills),
            )

        return QualityReport(
            quality_score       = score,
            avg_slippage_pips   = avg_slip,
            worst_slippage_pips = worst_slip,
            spread_instability  = spread_instab,
            fill_count          = len(fills),
            degraded            = degraded,
            reason              = reason,
        )

    # ── Backoff queries ───────────────────────────────────────────────────────

    def is_backed_off(self, symbol: str) -> bool:
        """Return True if this symbol has been put in execution backoff."""
        with self._lock:
            return self._backoff.get(symbol, False)

    def clear_backoff(self, symbol: str) -> None:
        """Clear backoff state after conditions improve."""
        with self._lock:
            self._backoff.pop(symbol, None)
        log.info("[EXECUTION QUALITY] %s backoff cleared", symbol)

    def get_avg_spread(self, symbol: str) -> float:
        """Return rolling average spread for a symbol (0.0 if no data)."""
        with self._lock:
            hist = list(self._spreads.get(symbol, []))
        return round(sum(hist) / len(hist), 2) if hist else 0.0
