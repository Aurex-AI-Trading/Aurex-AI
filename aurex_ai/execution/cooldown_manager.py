"""
Aurex AI Signature Strategy — Cooldown Manager

Prevents over-trading through per-symbol, per-direction cooldowns.

Cooldown durations are adaptive based on market regime:
  Trending market  (trend.consistent=True, strength≥20):  shorter cooldown
  Ranging market   (any other state):                      longer cooldown
  After a loss:                                            extended cooldown
  After a win:                                             standard cooldown

The manager is stateful and thread-safe (asyncio single-threaded model).
Cooldown state is in-memory and resets on process restart.

Usage:
    cm = CooldownManager()
    if cm.is_on_cooldown("EURUSD.Z", "BUY"):
        return
    # ... execute trade ...
    cm.arm("EURUSD.Z", "BUY", is_trending=True, settings=cfg)
    # on trade close:
    cm.arm_after_outcome("EURUSD.Z", was_win=False, settings=cfg)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from aurex_ai.strategy.trend import TrendResult
from aurex_ai.core.logger import get_logger

log = get_logger("execution.cooldown")


@dataclass
class CooldownEntry:
    expiry:    float   # monotonic time
    reason:    str
    direction: str


class CooldownManager:
    """
    Thread-safe (asyncio-compatible) per-symbol, per-direction cooldown tracker.
    """

    def __init__(self) -> None:
        # Key: "SYMBOL:DIRECTION"  Value: CooldownEntry
        self._cooldowns: Dict[str, CooldownEntry] = {}

    # ── Query ─────────────────────────────────────────────────────────────────

    def is_on_cooldown(self, symbol: str, direction: str) -> bool:
        """Return True if this symbol+direction is still cooling down."""
        entry = self._cooldowns.get(self._key(symbol, direction))
        if entry is None:
            return False
        if time.monotonic() >= entry.expiry:
            del self._cooldowns[self._key(symbol, direction)]
            return False
        return True

    def remaining(self, symbol: str, direction: str) -> float:
        """Return seconds remaining in the current cooldown (0.0 if none)."""
        entry = self._cooldowns.get(self._key(symbol, direction))
        if entry is None:
            return 0.0
        remaining = entry.expiry - time.monotonic()
        return max(0.0, remaining)

    def all_cooldowns(self) -> Dict[str, float]:
        """Return a dict of 'SYMBOL:DIR' -> remaining seconds for active entries."""
        now = time.monotonic()
        return {
            k: max(0.0, v.expiry - now)
            for k, v in self._cooldowns.items()
            if v.expiry > now
        }

    # ── Arm ───────────────────────────────────────────────────────────────────

    def arm(
        self,
        symbol:      str,
        direction:   str,
        trend:       TrendResult,
        trending_minutes: int = 15,
        ranging_minutes:  int = 60,
    ) -> None:
        """
        Start a post-trade cooldown for this symbol+direction.

        Duration depends on market regime:
          trending (consistent + strength≥20): trending_minutes
          ranging  (all other cases):          ranging_minutes
        """
        is_trending = trend.consistent and trend.strength >= 20.0
        duration    = (trending_minutes if is_trending else ranging_minutes) * 60.0

        regime = "trending" if is_trending else "ranging"
        self._set(symbol, direction, duration, f"post_trade_{regime}")

        log.info(
            "cooldown: armed %s %s for %.0f s (%s)",
            symbol, direction, duration, regime,
        )

    def arm_after_outcome(
        self,
        symbol:          str,
        was_win:         bool,
        post_loss_minutes: int = 45,
        post_win_minutes:  int = 20,
    ) -> None:
        """
        Extend or reduce cooldown based on trade outcome.
        Applies to BOTH directions on this symbol.
        """
        duration = (post_win_minutes if was_win else post_loss_minutes) * 60.0
        outcome  = "win" if was_win else "loss"

        for direction in ("BUY", "SELL"):
            self._set(symbol, direction, duration, f"post_{outcome}")

        log.info(
            "cooldown: %s outcome -> %s %s cooldown=%.0f s",
            outcome, symbol, "both_dirs", duration,
        )

    def clear(self, symbol: str, direction: Optional[str] = None) -> None:
        """Remove cooldown for a specific direction, or all directions."""
        if direction:
            self._cooldowns.pop(self._key(symbol, direction), None)
            log.debug("cooldown: cleared %s %s", symbol, direction)
        else:
            for d in ("BUY", "SELL"):
                self._cooldowns.pop(self._key(symbol, d), None)
            log.debug("cooldown: cleared %s all directions", symbol)

    def clear_all(self) -> None:
        """Reset all cooldowns (e.g., on emergency resume)."""
        count = len(self._cooldowns)
        self._cooldowns.clear()
        log.info("cooldown: cleared all %d entries", count)

    # ── Internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _key(symbol: str, direction: str) -> str:
        return f"{symbol.upper()}:{direction.upper()}"

    def _set(self, symbol: str, direction: str, duration: float, reason: str) -> None:
        self._cooldowns[self._key(symbol, direction)] = CooldownEntry(
            expiry    = time.monotonic() + duration,
            reason    = reason,
            direction = direction,
        )
