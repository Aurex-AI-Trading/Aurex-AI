"""
Aurex AI — Breakout + Retest Strategy

Detects:
1. Consolidation range (narrow-range candles over N bars)
2. Breakout above/below the range with momentum
3. Retest of the broken level (optional confirmation)

This serves as the Tier-3 / fallback strategy when no FVG/OB setups appear.

Score (0–12):
  - Strong breakout + retest:       12
  - Strong breakout, no retest yet:  9
  - Weak breakout:                   5
  - No breakout:                     0
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from aurex_ai.core.data_feed import Candle
from aurex_ai.core.logger import get_logger

log = get_logger("strategy.breakout")


@dataclass
class BreakoutResult:
    detected:          bool
    direction:         str       # "bullish" | "bearish" | "none"
    range_high:        float
    range_low:         float
    breakout_price:    float
    is_retest:         bool
    breakout_strength: float     # impulse in pips
    score:             float     # 0–12


def analyze(
    candles:          List[Candle],
    price:            float,
    symbol:           str   = "",
    consolidation_bars: int = 8,
    lookback:         int   = 25,
    min_range_pips:   float = 5.0,
    min_breakout_pips: float = 6.0,
    pip_size:         float = 0.0001,
    max_score:        float = 12.0,
) -> BreakoutResult:
    """
    Detect breakout setups on M15.

    Strategy:
      1. Find a consolidation zone (tight range over `consolidation_bars` candles)
      2. Check if price has broken above/below with momentum
      3. Check if price is retesting the broken level

    Args:
        candles:            M15 candle list
        price:              Current price
        consolidation_bars: Number of candles to define consolidation
        lookback:           How far back to look for consolidation
        min_range_pips:     Minimum consolidation range size
        min_breakout_pips:  Minimum move required to qualify as breakout

    Returns:
        BreakoutResult with direction and score
    """
    _NONE = BreakoutResult(
        detected=False, direction="none",
        range_high=0.0, range_low=0.0,
        breakout_price=0.0, is_retest=False,
        breakout_strength=0.0, score=0.0,
    )

    window = candles[-lookback:] if len(candles) >= lookback else candles[:]
    if len(window) < consolidation_bars + 3:
        return _NONE

    min_range = min_range_pips * pip_size
    min_bo    = min_breakout_pips * pip_size

    # ── Scan for consolidation zones ──────────────────────────────────────────
    best_result: Optional[BreakoutResult] = None
    best_score = 0.0

    for start in range(len(window) - consolidation_bars - 2):
        zone = window[start : start + consolidation_bars]
        zone_high = max(c.high for c in zone)
        zone_low  = min(c.low  for c in zone)
        zone_range = zone_high - zone_low

        if zone_range < min_range:
            continue  # too tight — not a real consolidation

        # Check candles after the zone for breakout
        post_zone = window[start + consolidation_bars:]
        if not post_zone:
            continue

        # Bullish breakout
        bullish_break = any(c.close > zone_high + min_bo for c in post_zone)
        # Bearish breakout
        bearish_break = any(c.close < zone_low - min_bo for c in post_zone)

        if not bullish_break and not bearish_break:
            continue

        direction = "bullish" if bullish_break else "bearish"
        bo_price = zone_high if bullish_break else zone_low

        # Measure breakout strength (last post-zone candle)
        last_post = post_zone[-1]
        if bullish_break:
            strength = max(0.0, last_post.close - zone_high) / pip_size
        else:
            strength = max(0.0, zone_low - last_post.close) / pip_size

        # Check for retest: price comes back to broken level
        is_retest = False
        broken_level_area = zone_high if bullish_break else zone_low
        tolerance = zone_range * 0.3

        # Retest = price returned to level after initial breakout
        if len(post_zone) >= 3:
            prices_after_bo = [c.low for c in post_zone[1:]] if bullish_break else [c.high for c in post_zone[1:]]
            if bullish_break:
                is_retest = any(abs(p - zone_high) <= tolerance for p in prices_after_bo)
            else:
                is_retest = any(abs(p - zone_low) <= tolerance for p in prices_after_bo)

        # Score
        if is_retest and strength >= min_breakout_pips:
            score = max_score           # 12: breakout + retest confirmed
        elif strength >= min_breakout_pips * 1.5:
            score = max_score * 0.75    # 9: strong breakout, no retest
        elif strength >= min_breakout_pips:
            score = max_score * 0.42    # 5: marginal breakout
        else:
            score = 0.0

        score = round(score, 1)

        if score > best_score:
            best_score = score
            best_result = BreakoutResult(
                detected          = True,
                direction         = direction,
                range_high        = round(zone_high, 5),
                range_low         = round(zone_low,  5),
                breakout_price    = round(bo_price,  5),
                is_retest         = is_retest,
                breakout_strength = round(strength, 1),
                score             = score,
            )

    if best_result:
        log.debug(
            "Breakout [%s]: dir=%s retest=%s strength=%.1f pips score=%.1f",
            symbol, best_result.direction, best_result.is_retest,
            best_result.breakout_strength, best_result.score,
        )
        return best_result

    return _NONE
