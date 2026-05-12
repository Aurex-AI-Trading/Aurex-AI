"""
Aurex AI — Market Condition Classifier  (Phase 3: Strategy Focus)

Classifies the market environment before confluence analysis so strategy
preference can adapt instead of applying identical logic to every condition.

States
------
  TRENDING              — clear directional bias; trend continuation entries preferred.
  RANGING               — oscillating price; execution scaled back, sweeps ignored.
  VOLATILE_EXPANSION    — high-ATR impulsive move (news/spike); size reduced.
  VOLATILITY_COMPRESSION— ATR contracting < 50% of recent average; breakout imminent.
  REVERSAL_ENVIRONMENT  — sharp counter-trend move detected; trend bias invalidated.
  DEAD                  — ATR below threshold; already blocked by main ATR gate.

Classification inputs (all derived from already-computed data in main.py):
  - atr_pips          : current 14-period ATR on M15 in pips
  - trend_direction   : "bullish" | "bearish" | "neutral" (from TrendResult)
  - trend_strength    : 0–25 float (from TrendResult.strength)
  - candles_m15       : for directional consistency and EMA-deviation calculation

Execution multipliers
---------------------
  TRENDING              → 1.00  (full participation — home territory)
  RANGING               → 0.80  (20% reduction — choppy entries lose frequently)
  VOLATILE_EXPANSION    → 0.70  (30% reduction — spike risk, wider spreads)
  VOLATILITY_COMPRESSION→ 0.75  (25% reduction — low range; breakout not yet confirmed)
  REVERSAL_ENVIRONMENT  → 0.65  (35% reduction — trend bias invalid; high whipsaw risk)
  DEAD                  → 0.00  (already blocked upstream; returned for completeness)

Logging tags
------------
  [MARKET REGIME]   [TREND REGIME]   [RANGE REGIME]
  [LOW QUALITY ENVIRONMENT]   [VOLATILE EXPANSION]
  [VOLATILITY COMPRESSION]    [REVERSAL ENVIRONMENT]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from aurex_ai.core.data_feed import Candle
from aurex_ai.strategy.trend import ema as _ema
from aurex_ai.core.logger import get_logger

log = get_logger("strategy.market_state")

# ATR thresholds for expansion detection (in pips, pip-class-agnostic)
_ATR_HIGH_FOREX = 25.0   # forex pairs: ATR > 25 pips = volatile expansion
_ATR_HIGH_GOLD  = 200.0  # XAUUSD: ATR > 200 pips = volatile expansion

# Trend strength gate: minimum TrendResult.strength to call it TRENDING
_MIN_TREND_STRENGTH_TRENDING = 10.0

# Directional consistency: fraction of candles making H/H-L or L/H-L in trend direction
_CONSISTENT_TRENDING = 0.55   # ≥55% of recent bars agree with trend direction
_CONSISTENT_RANGING  = 0.40   # ≤40% → market is oscillating without conviction


@dataclass
class MarketStateResult:
    """Output of classify()."""
    state:           str    # "TRENDING" | "RANGING" | "VOLATILE_EXPANSION" | "DEAD"
    trend_strength:  float  # directional consistency ratio 0.0–1.0
    range_score:     float  # 1 - trend_strength (higher = more ranging)
    atr_pips:        float
    exec_mult:       float  # recommended lot size multiplier for this state
    reason:          str


_RANGING_RESULT_TMPL = "atr={atr:.1f} dir={direction} consistency={cs:.2f} ema_dev={ed:.2f}"


def _directional_consistency(
    candles: List[Candle],
    direction: str,
    window: int = 15,
) -> float:
    """
    Return fraction of candles in `window` that make progress in `direction`.

    For "bullish": count candles with higher high AND higher low.
    For "bearish": count candles with lower high AND lower low.
    For "neutral": count max(bullish, bearish) / total.
    """
    w = candles[-window:] if len(candles) >= window else candles[:]
    n = len(w)
    if n < 2:
        return 0.5

    bull = sum(
        1 for i in range(1, n)
        if w[i].high > w[i - 1].high and w[i].low > w[i - 1].low
    )
    bear = sum(
        1 for i in range(1, n)
        if w[i].high < w[i - 1].high and w[i].low < w[i - 1].low
    )

    if direction == "bullish":
        return bull / (n - 1)
    if direction == "bearish":
        return bear / (n - 1)
    return max(bull, bear) / (n - 1)


def _ema_deviation(
    candles: List[Candle],
    atr_pips: float,
    pip: float,
    ema_period: int = 50,
) -> float:
    """
    Price deviation from EMA50 normalised by ATR.

    Trending markets: price is consistently away from EMA (deviation > 0.5 ATR).
    Ranging markets: price oscillates close to EMA (deviation < 0.5 ATR).
    """
    if len(candles) < ema_period or atr_pips <= 0 or pip <= 0:
        return 0.5  # neutral when insufficient data

    closes = [c.close for c in candles]
    ema_vals = _ema(closes, ema_period)
    if not ema_vals:
        return 0.5

    price = closes[-1]
    deviation_pips = abs(price - ema_vals[-1]) / pip
    return deviation_pips / atr_pips     # ATR-normalised deviation


def classify(
    candles_m15:      List[Candle],
    atr_pips:         float,
    pip:              float,
    trend_direction:  str   = "neutral",   # from TrendResult
    trend_strength:   float = 0.0,         # from TrendResult.strength (0–25)
    symbol:           str   = "",
) -> MarketStateResult:
    """
    Classify market condition from M15 data and existing trend analysis.

    Args:
        candles_m15:     M15 candle list (minimum 15 bars).
        atr_pips:        Current ATR in pips.
        pip:             Pip size for this symbol.
        trend_direction: "bullish" | "bearish" | "neutral" from TrendResult.
        trend_strength:  Numeric strength score from TrendResult (0–25).
        symbol:          Instrument name for logging and ATR threshold selection.

    Returns:
        MarketStateResult with state, multipliers, and diagnostic reason.
    """
    _DEAD = MarketStateResult(
        state="DEAD", trend_strength=0.0, range_score=1.0,
        atr_pips=atr_pips, exec_mult=0.0,
        reason=f"atr_dead({atr_pips:.1f}pips)",
    )

    if atr_pips <= 3.0:
        log.warning("[MARKET REGIME] %s DEAD [LOW QUALITY ENVIRONMENT] | atr=%.1f", symbol, atr_pips)
        return _DEAD

    # ATR-based volatile expansion detection (symbol-class-aware)
    _atr_high = _ATR_HIGH_GOLD if ("XAU" in symbol.upper() or "GOLD" in symbol.upper()) \
                else _ATR_HIGH_FOREX
    is_high_atr = atr_pips >= _atr_high

    # Directional consistency of recent candles
    consistency = _directional_consistency(candles_m15, trend_direction)

    # EMA50 deviation (normalised by ATR)
    ema_dev = _ema_deviation(candles_m15, atr_pips, pip)

    range_score = 1.0 - consistency

    # ── ATR compression detection (Phase 6: VOLATILITY_COMPRESSION) ──────────
    # Compare recent 5-bar ATR average to prior 15-bar ATR average.
    # Compression < 50% of baseline = market coiling before a breakout.
    _is_compressed = False
    if len(candles_m15) >= 20:
        _recent_ranges = [c.high - c.low for c in candles_m15[-5:]]
        _prior_ranges  = [c.high - c.low for c in candles_m15[-20:-5]]
        _recent_avg    = sum(_recent_ranges) / 5
        _prior_avg     = sum(_prior_ranges)  / 15
        _is_compressed = _prior_avg > 0 and _recent_avg < _prior_avg * 0.50

    # ── Reversal detection (Phase 6: REVERSAL_ENVIRONMENT) ───────────────────
    # Detected when a strong trend exists (trend_strength >= threshold) but
    # recent candle consistency sharply contradicts it — price is fighting the trend.
    _is_reversal = (
        trend_strength >= _MIN_TREND_STRENGTH_TRENDING
        and trend_direction != "neutral"
        and consistency < _CONSISTENT_RANGING
        and ema_dev > 1.0    # price significantly displaced against trend EMA
    )

    # ── Classification logic ──────────────────────────────────────────────────

    if is_high_atr:
        # Very high ATR: news/spike environment — caution regardless of direction
        state    = "VOLATILE_EXPANSION"
        exec_mult = 0.70
        reason   = (
            f"volatile_expansion | atr={atr_pips:.1f} >= threshold={_atr_high:.0f} "
            f"consistency={consistency:.2f}"
        )
        log.warning(
            "[MARKET REGIME] %s [VOLATILE EXPANSION] [LOW QUALITY ENVIRONMENT] "
            "| atr=%.1f threshold=%.0f exec_mult=%.2f",
            symbol, atr_pips, _atr_high, exec_mult,
        )

    elif _is_reversal:
        # Sharp counter-trend move — trend bias invalidated
        state     = "REVERSAL_ENVIRONMENT"
        exec_mult = 0.65
        reason    = (
            f"reversal_environment | dir={trend_direction} strength={trend_strength:.0f} "
            f"consistency={consistency:.2f} ema_dev={ema_dev:.2f}"
        )
        log.warning(
            "[MARKET REGIME] %s [REVERSAL ENVIRONMENT] [LOW QUALITY ENVIRONMENT] "
            "| strength=%.0f consistency=%.2f ema_dev=%.2f exec_mult=0.65",
            symbol, trend_strength, consistency, ema_dev,
        )

    elif _is_compressed:
        # Low-range coiling — potential breakout but direction unconfirmed
        state     = "VOLATILITY_COMPRESSION"
        exec_mult = 0.75
        reason    = (
            f"volatility_compression | recent_range={_recent_avg:.5f} "
            f"prior_range={_prior_avg:.5f} consistency={consistency:.2f}"
        )
        log.warning(
            "[MARKET REGIME] %s [VOLATILITY COMPRESSION] [LOW QUALITY ENVIRONMENT] "
            "| range_ratio=%.2f exec_mult=0.75",
            symbol,
            _recent_avg / _prior_avg if _prior_avg > 0 else 0,
        )

    elif trend_direction != "neutral" and trend_strength >= _MIN_TREND_STRENGTH_TRENDING \
            and consistency >= _CONSISTENT_TRENDING:
        # Trending: directional structure confirmed by both TrendResult and candle pattern
        state     = "TRENDING"
        exec_mult = 1.00
        reason    = (
            f"trending | dir={trend_direction} strength={trend_strength:.0f} "
            f"consistency={consistency:.2f} ema_dev={ema_dev:.2f}"
        )
        log.warning(
            "[MARKET REGIME] [TREND REGIME] %s [TRENDING] | dir=%s strength=%.0f "
            "consistency=%.2f ema_dev=%.2f exec_mult=1.00 [CONTINUATION BIAS]",
            symbol, trend_direction, trend_strength, consistency, ema_dev,
        )

    elif consistency <= _CONSISTENT_RANGING or \
            (trend_direction == "neutral" and ema_dev < 0.5):
        # Ranging: low directional consistency or neutral trend with price near EMA
        state     = "RANGING"
        exec_mult = 0.80
        reason    = (
            f"ranging | consistency={consistency:.2f} ema_dev={ema_dev:.2f} "
            f"dir={trend_direction}"
        )
        log.warning(
            "[MARKET REGIME] [RANGE REGIME] %s [RANGING] [LOW QUALITY ENVIRONMENT] "
            "| consistency=%.2f ema_dev=%.2f exec_mult=0.80",
            symbol, consistency, ema_dev,
        )

    else:
        # Moderate trend — partially directional but not confirmed
        state     = "TRENDING"
        exec_mult = 0.90
        reason    = (
            f"moderate_trend | dir={trend_direction} strength={trend_strength:.0f} "
            f"consistency={consistency:.2f} (below threshold={_CONSISTENT_TRENDING:.2f})"
        )
        log.warning(
            "[MARKET REGIME] [TREND REGIME] %s [TRENDING] (moderate) | "
            "consistency=%.2f exec_mult=0.90",
            symbol, consistency,
        )

    return MarketStateResult(
        state          = state,
        trend_strength = round(consistency, 3),
        range_score    = round(range_score, 3),
        atr_pips       = atr_pips,
        exec_mult      = exec_mult,
        reason         = reason,
    )
