"""
Aurex AI Signature Strategy — Fibonacci Retracement Module

Identifies the most recent significant price impulse and computes standard
Fibonacci retracement levels.  The "golden zone" (0.50–0.705) represents
the highest-probability reversal area, aligning with institutional order-block
theory.

Key levels computed:
  0.382, 0.500, 0.618, 0.705, 0.786

Golden zone:  0.500 – 0.705

Impulse detection:
  Scans the last `lookback` bars for the most extreme high and low.
  The one that formed MORE RECENTLY defines the end of the impulse:
    - low formed after high -> bearish impulse (looking for SELL retracement)
    - high formed after low -> bullish impulse (looking for BUY retracement)

Score breakdown (max 15):
  Price inside golden zone (0.50–0.705):      9–15 pts  (15 at 0.618 exactly)
  Price near golden zone (within 1× size):     5 pts
  Price at outer levels (0.382, 0.786):        3 pts
  No Fibonacci context / insufficient move:    0 pts
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from aurex_ai.core.data_feed import Candle
from aurex_ai.core.logger import get_logger

log = get_logger("strategy.fibonacci")


@dataclass
class FibResult:
    impulse_high:        float
    impulse_low:         float
    impulse_dir:         str                # "bullish" | "bearish" | "none"
    levels:              Dict[float, float] # ratio -> retracement price
    golden_zone:         Tuple[float, float]# (lower_price, upper_price)
    price_in_zone:       bool
    score:               float              # 0–15
    nearest_ratio:       float              # closest Fibonacci ratio to current price
    reason:              str
    retrace_depth:       str               # "SHALLOW"|"MODERATE"|"DEEP"|"EXHAUSTED"|"NONE"
    continuation_aligned: bool             # True when depth is ideal for trend continuation


# ── Impulse detection ─────────────────────────────────────────────────────────

def _find_impulse(
    candles:       List[Candle],
    lookback:      int,
    min_move_pips: float,
    pip_size:      float,
) -> Tuple[float, float, str]:
    """
    Return (impulse_start, impulse_end, direction) for the last major move.

    Returns (0, 0, "none") when the move is smaller than min_move_pips.
    """
    window    = candles[-lookback:]
    min_move  = min_move_pips * pip_size

    high_idx  = max(range(len(window)), key=lambda i: window[i].high)
    low_idx   = min(range(len(window)), key=lambda i: window[i].low)
    high_price = window[high_idx].high
    low_price  = window[low_idx].low

    if abs(high_price - low_price) < min_move:
        return 0.0, 0.0, "none"

    # More recent index = end of impulse
    if high_idx > low_idx:
        return low_price, high_price, "bullish"   # BUY impulse -> look for SELL retracement or continuation
    else:
        return high_price, low_price, "bearish"   # SELL impulse -> look for BUY retracement


# ── Fibonacci levels ──────────────────────────────────────────────────────────

def _compute_levels(start: float, end: float, ratios: List[float]) -> Dict[float, float]:
    """
    Compute retracement prices.
    Level(ratio) = end - ratio × (end - start)
    For a bullish impulse (start < end): level(0.618) pulls price back toward start.
    """
    move = end - start
    return {r: round(end - r * move, 5) for r in ratios}


# ── Retracement depth classification ─────────────────────────────────────────

def _retrace_depth(ratio: float) -> str:
    """
    Classify how deeply price has retraced into the impulse move.

    < 0.382   SHALLOW   — too early; pullback may not be exhausted.
    0.382–0.705 MODERATE — golden zone; ideal continuation entry.
    0.705–0.786 DEEP     — extended retracement; reduced probability.
    > 0.786   EXHAUSTED — likely reversal, not continuation.
    0.0       NONE      — no impulse detected.
    """
    if ratio == 0.0:
        return "NONE"
    if ratio < 0.382:
        return "SHALLOW"
    if ratio <= 0.705:
        return "MODERATE"
    if ratio <= 0.786:
        return "DEEP"
    return "EXHAUSTED"


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score(
    price:      float,
    levels:     Dict[float, float],
    gz_low:     float,
    gz_high:    float,
) -> Tuple[float, float]:
    """Return (score 0-15, nearest_ratio)."""
    in_zone = gz_low <= price <= gz_high

    if in_zone:
        # Score 9–15: peaks at 0.618 (nearest to golden zone midpoint)
        level_618 = levels.get(0.618, (gz_low + gz_high) / 2.0)
        zone_size = max(gz_high - gz_low, 1e-8)
        proximity = abs(price - level_618) / zone_size   # 0 = at 0.618, 1 = at boundary
        score     = round(15.0 - proximity * 6.0, 1)
        return max(9.0, score), 0.618

    # Outside zone
    nearest_ratio = min(levels.keys(), key=lambda r: abs(levels[r] - price))
    nearest_price = levels[nearest_ratio]
    dist          = abs(price - nearest_price)
    zone_size     = max(gz_high - gz_low, 1e-8)

    if dist <= zone_size:
        return 5.0, nearest_ratio
    if dist <= 2.0 * zone_size:
        return 3.0, nearest_ratio
    return 0.0, nearest_ratio


# ── Public API ────────────────────────────────────────────────────────────────

def analyze(
    candles:       List[Candle],
    price:         float,
    symbol:        str                  = "EURUSD.Z",
    lookback:      int                  = 50,
    fib_ratios:    Optional[List[float]]= None,
    golden_zone:   Tuple[float, float]  = (0.5, 0.705),
    min_move_pips: float                = 30.0,
    pip_size:      float                = 0.0001,
) -> FibResult:
    """
    Compute Fibonacci retracement levels for the last impulse and score
    how close `price` is to the golden zone.

    Args:
        candles:       M15 candles for impulse detection.
        price:         Current market price (last close).
        lookback:      Bars to search for the impulse.
        fib_ratios:    Ratios to compute (defaults to [0.382, 0.5, 0.618, 0.705, 0.786]).
        golden_zone:   Tuple of (lower_ratio, upper_ratio) for the high-prob zone.
        min_move_pips: Minimum impulse size to consider.
        pip_size:      Pip size for this instrument.
    """
    if fib_ratios is None:
        fib_ratios = [0.382, 0.5, 0.618, 0.705, 0.786]

    # Ensure golden zone boundaries are in the ratio list
    for r in golden_zone:
        if r not in fib_ratios:
            fib_ratios.append(r)
    fib_ratios = sorted(set(fib_ratios))

    _EMPTY = FibResult(
        impulse_high=0.0, impulse_low=0.0, impulse_dir="none",
        levels={}, golden_zone=(0.0, 0.0),
        price_in_zone=False, score=0.0, nearest_ratio=0.0,
        reason="no_impulse", retrace_depth="NONE", continuation_aligned=False,
    )

    if len(candles) < lookback:
        return _EMPTY

    start, end, direction = _find_impulse(candles, lookback, min_move_pips, pip_size)
    if direction == "none":
        log.debug("fib: no qualifying impulse for %s (move < %.0f pips)", symbol, min_move_pips)
        return _EMPTY

    levels = _compute_levels(start, end, fib_ratios)

    # Golden zone in price units
    gz_prices = [levels[golden_zone[0]], levels[golden_zone[1]]]
    gz_low    = min(gz_prices)
    gz_high   = max(gz_prices)

    score, nearest = _score(price, levels, gz_low, gz_high)
    in_zone        = gz_low <= price <= gz_high
    imp_high       = max(start, end)
    imp_low        = min(start, end)

    retrace_depth       = _retrace_depth(nearest)
    continuation_aligned = retrace_depth == "MODERATE"

    reason = (
        f"impulse_{direction} [{imp_low:.5f}->{imp_high:.5f}] "
        f"golden=[{gz_low:.5f}–{gz_high:.5f}] "
        f"in_zone={in_zone} nearest={nearest:.3f} depth={retrace_depth} score={score:.1f}"
    )

    if score > 0:
        if continuation_aligned:
            log.warning(
                "[FIB CONFLUENCE] %s [RETRACE DEPTH:%s] [FIB CONTINUATION] "
                "ratio=%.3f in_zone=%s score=%.1f [CONTINUATION BIAS]",
                symbol, retrace_depth, nearest, in_zone, score,
            )
        else:
            log.info(
                "[FIB CONFLUENCE] %s [RETRACE DEPTH:%s] ratio=%.3f score=%.1f",
                symbol, retrace_depth, nearest, score,
            )
    else:
        log.debug("fib: %s", reason)

    return FibResult(
        impulse_high         = round(imp_high,  5),
        impulse_low          = round(imp_low,   5),
        impulse_dir          = direction,
        levels               = levels,
        golden_zone          = (round(gz_low, 5), round(gz_high, 5)),
        price_in_zone        = in_zone,
        score                = score,
        nearest_ratio        = nearest,
        reason               = reason,
        retrace_depth        = retrace_depth,
        continuation_aligned = continuation_aligned,
    )
