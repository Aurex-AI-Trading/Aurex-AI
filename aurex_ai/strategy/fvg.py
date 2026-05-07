"""
Aurex AI Signature Strategy — Fair Value Gap (FVG) Detection

A Fair Value Gap (price imbalance) forms when a strong directional move
leaves a gap between candle[i-2]'s extreme and candle[i]'s extreme:

  Bullish FVG:  candle[i-2].high  <  candle[i].low
                (gap above — buyers never returned to cover the imbalance)

  Bearish FVG:  candle[i-2].low   >  candle[i].high
                (gap below — sellers left an unmitigated imbalance)

A FVG is "open" (tradeable) until price re-enters its zone.  Once filled,
it is discarded from the active list.

Score breakdown (max 20):
  Price currently inside the nearest open FVG zone:  20 pts  (strong)
  FVG exists but price is outside the zone:          10 pts  (weak)
  No open FVG found in lookback:                      0 pts
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from aurex_ai.core.data_feed import Candle
from aurex_ai.core.logger import get_logger

log = get_logger("strategy.fvg")


@dataclass
class FVGZone:
    high:    float
    low:     float
    bullish: bool
    bar_idx: int     # index of the middle (impulse) candle
    filled:  bool = False

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0

    @property
    def size(self) -> float:
        return self.high - self.low


@dataclass
class FVGResult:
    present:      bool
    zones:        List[FVGZone]          # all open FVGs found
    active_zone:  Optional[FVGZone]      # nearest / best zone
    score:        float                  # 0 | 10 | 20
    price_in_zone: bool
    strength:     str                    # "strong" | "weak" | "none"
    reason:       str


# ── Detection ─────────────────────────────────────────────────────────────────

def _find_fvgs(candles: List[Candle], min_size: float) -> List[FVGZone]:
    """
    Scan all 3-bar windows for FVG patterns.
    Marks zones as filled when a subsequent candle re-enters the gap.
    Returns only open (unfilled) zones, newest first.
    """
    zones: List[FVGZone] = []

    for i in range(2, len(candles)):
        c1, c3 = candles[i - 2], candles[i]

        # Bullish FVG: gap between c1.high and c3.low
        if c3.low > c1.high and (c3.low - c1.high) >= min_size:
            zones.append(FVGZone(high=c3.low, low=c1.high, bullish=True, bar_idx=i - 1))

        # Bearish FVG: gap between c3.high and c1.low
        elif c1.low > c3.high and (c1.low - c3.high) >= min_size:
            zones.append(FVGZone(high=c1.low, low=c3.high, bullish=False, bar_idx=i - 1))

    # Mark zones that have been filled by a subsequent candle.
    # Start at bar_idx+2 to skip c3 (the gap-defining candle whose extreme IS the zone boundary).
    for zone in zones:
        for fc in candles[zone.bar_idx + 2:]:
            if fc.low <= zone.high and fc.high >= zone.low:
                zone.filled = True
                break

    return [z for z in zones if not z.filled]


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_zone(price: float, zone: FVGZone) -> Tuple[float, bool, str]:
    """Return (score, price_in_zone, strength)."""
    inside = zone.low <= price <= zone.high
    if inside:
        return 20.0, True, "strong"
    return 10.0, False, "weak"


# ── Public API ────────────────────────────────────────────────────────────────

def analyze(
    candles:       List[Candle],
    price:         float,
    symbol:        str   = "EURUSD.Z",
    lookback:      int   = 50,
    min_size_pips: float = 1.0,
    pip_size:      float = 0.0001,
) -> FVGResult:
    """
    Find all open FVGs in the last `lookback` candles and score the nearest one.

    Args:
        candles:       M15 (or M5) candles.
        price:         Current market price (last close).
        lookback:      Maximum bars to scan.
        min_size_pips: Minimum gap size to qualify as an FVG.
        pip_size:      Pip value in price units for this instrument.
    """
    _EMPTY = FVGResult(
        present=False, zones=[], active_zone=None,
        score=0.0, price_in_zone=False, strength="none", reason="no_fvg",
    )

    if len(candles) < 3:
        return FVGResult(present=False, zones=[], active_zone=None,
                         score=0.0, price_in_zone=False, strength="none",
                         reason="insufficient_data")

    window   = candles[-lookback:]
    min_size = min_size_pips * pip_size
    zones    = _find_fvgs(window, min_size)

    if not zones:
        return _EMPTY

    # Select nearest zone to current price
    def _dist(z: FVGZone) -> float:
        if z.low <= price <= z.high:
            return 0.0
        return min(abs(price - z.high), abs(price - z.low))

    best                  = min(zones, key=_dist)
    score, in_zone, strength = _score_zone(price, best)
    fvg_type = "bull" if best.bullish else "bear"

    log.warning(
        "[FVG] %s detected zone=[%.5f–%.5f] %s in_zone=%s strength=%s score=%.0f",
        symbol, best.low, best.high, fvg_type, in_zone, strength, score,
    )

    return FVGResult(
        present       = True,
        zones         = zones,
        active_zone   = best,
        score         = score,
        price_in_zone = in_zone,
        strength      = strength,
        reason        = (
            f"{fvg_type}_fvg "
            f"[{best.low:.5f}–{best.high:.5f}] "
            f"in_zone={in_zone} strength={strength} score={score:.0f}"
        ),
    )
