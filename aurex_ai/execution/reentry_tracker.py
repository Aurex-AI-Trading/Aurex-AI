"""
Aurex AI — Re-Entry Tracker

Records recently closed BE/SL trades and evaluates whether the next signal
on the same symbol qualifies as a safe re-entry.

Re-entry is allowed ONLY when all four conditions pass:
  A) Exit was BE or SL  (NOT full TP)
  B) Same direction signal still active  (strategy confirms trend)
  C) Confidence >= 70  (market quality gate)
  D) Price has retraced >= 0.3 ATR below original entry (BUY)
     or >= 0.3 ATR above original entry (SELL)
  E) Within 60 minutes of the original close

If the symbol is a candidate but conditions fail, the trade is blocked and
logged as [RE-ENTRY BLOCKED].  If conditions pass, the caller executes
normally and logs [RE-ENTRY EXECUTED].

Implicit safeguards (NOT duplicated here — already enforced upstream):
  - Directional cooldown  : scan_symbol() checks this before reaching us
  - Volatility / ATR      : confidence < 70 gates this
  - Daily / open limits   : scan_symbol() hard-caps at the top
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from aurex_ai.core.logger import get_logger

log = get_logger("execution.reentry_tracker")

_WINDOW_MINUTES = 60      # candidate expires after this many minutes
_MIN_CONFIDENCE = 70.0    # minimum confidence to allow re-entry
_RETRACE_ATR    = 0.3     # price must be >= this many ATR away from original entry


@dataclass
class ClosedTrade:
    symbol:      str
    direction:   str       # "BUY" | "SELL"
    entry_price: float
    exit_reason: str       # "BE" | "SL" | "TP"
    closed_at:   datetime
    pnl:         float


class ReEntryTracker:
    """
    Stateful tracker for post-close re-entry evaluation.
    Instantiate once per session in run_live().
    """

    def __init__(self) -> None:
        self._candidates: Dict[str, ClosedTrade] = {}   # symbol -> most recent eligible close

    # ── Record a closed trade ─────────────────────────────────────────────────

    def record(self, trade: ClosedTrade) -> None:
        """
        Store a closed trade as a re-entry candidate.
        TP exits are discarded immediately — no re-entry after full TP.
        """
        if trade.exit_reason == "TP":
            return
        self._candidates[trade.symbol] = trade
        log.info(
            "[RE-ENTRY] %s %s recorded as candidate | exit=%s pnl=%.2f",
            trade.symbol, trade.direction, trade.exit_reason, trade.pnl,
        )

    # ── Evaluate a signal against re-entry conditions ─────────────────────────

    def is_candidate(self, symbol: str) -> bool:
        """Return True if symbol has a recent eligible closed trade."""
        return symbol in self._candidates

    def evaluate(
        self,
        symbol:        str,
        direction:     str,
        confidence:    float,
        atr_pips:      float,
        current_price: float,
        current_time:  datetime,
        pip:           float,
    ) -> Tuple[bool, str]:
        """
        Called when the strategy generates a BUY/SELL signal for symbol.

        Returns:
          (True,  "")         — symbol has no recent close; treat as fresh trade
          (True,  "reentry")  — re-entry conditions all pass; caller should execute
          (False, reason)     — re-entry conditions failed; caller should block
        """
        trade = self._candidates.get(symbol)
        if trade is None:
            return True, ""     # no candidate — normal fresh trade

        elapsed_min = (current_time - trade.closed_at).total_seconds() / 60.0
        if elapsed_min > _WINDOW_MINUTES:
            self._candidates.pop(symbol, None)
            return True, ""     # window expired — treat as fresh

        log.info(
            "[RE-ENTRY CHECK] %s %s | elapsed=%.0fm exit=%s pnl=%.2f conf=%.0f",
            symbol, direction, elapsed_min, trade.exit_reason, trade.pnl, confidence,
        )

        # B) Direction must match
        if direction != trade.direction:
            log.warning("[RE-ENTRY BLOCKED] %s | reason=direction_changed", symbol)
            return False, "direction_changed"

        # C) Confidence gate
        if confidence < _MIN_CONFIDENCE:
            log.warning(
                "[RE-ENTRY BLOCKED] %s | reason=confidence_too_low conf=%.0f",
                symbol, confidence,
            )
            return False, "confidence_too_low"

        # D) Price retrace: current price must be at least 0.3 ATR away from
        #    the original entry in the pullback direction.
        atr_dist    = atr_pips * pip
        min_retrace = _RETRACE_ATR * atr_dist

        if direction == "BUY":
            retrace = trade.entry_price - current_price   # positive when price below entry
        else:
            retrace = current_price - trade.entry_price   # positive when price above entry

        if retrace < min_retrace:
            log.warning(
                "[RE-ENTRY BLOCKED] %s | reason=insufficient_retrace "
                "retrace=%.5f needed=%.5f",
                symbol, retrace, min_retrace,
            )
            return False, "insufficient_retrace"

        return True, "reentry"

    # ── Housekeeping ──────────────────────────────────────────────────────────

    def clear(self, symbol: str) -> None:
        """Remove a candidate after a successful re-entry."""
        self._candidates.pop(symbol, None)

    def expire_all(self, current_time: datetime) -> None:
        """Purge candidates older than the window.  Call once per scan cycle."""
        stale = [
            s for s, t in self._candidates.items()
            if (current_time - t.closed_at).total_seconds() / 60.0 > _WINDOW_MINUTES
        ]
        for s in stale:
            del self._candidates[s]
