"""
Aurex AI Signature Strategy — Trend Detection

Timeframes analysed:
  H4  — higher-frame bias  (EMA50 vs EMA200 alignment)
  H1  — entry-frame bias   (price vs EMA50 vs EMA200)

Score breakdown (max 25):
  H1 fully aligned (price + EMA50 + EMA200 stacked):  10 pts
  H4 EMA50 / EMA200 aligned (macro bias):             10 pts
  H1 and H4 agree on the same direction:               5 pts

Returned TrendResult fields drive direction votes in the confluence engine
and control the adaptive cooldown (consistent = trending market).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from aurex_ai.core.data_feed import Candle
from aurex_ai.core.logger import get_logger

log = get_logger("strategy.trend")


@dataclass
class TrendResult:
    direction:  str    # "bullish" | "bearish" | "neutral"
    strength:   float  # 0–25  (used as trend_score in confluence)
    h1_aligned: bool   # H1: price > EMA50 > EMA200  (or inverse)
    h4_aligned: bool   # H4: EMA50 > EMA200           (or inverse)
    consistent: bool   # both frames agree
    ema50_h1:   float
    ema200_h1:  float
    ema50_h4:   float
    ema200_h4:  float
    price:      float
    reason:     str


# ── EMA helper (shared within this module) ───────────────────────────────────

def ema(values: List[float], period: int) -> List[float]:
    """
    Compute an Exponential Moving Average.
    Returns an empty list when fewer than `period` values are provided.
    """
    if len(values) < period:
        return []
    k    = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out  = [seed]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score(h1_aligned: bool, h4_aligned: bool, consistent: bool) -> float:
    pts = 0.0
    if h1_aligned:  pts += 10.0
    if h4_aligned:  pts += 10.0
    if consistent:  pts +=  5.0
    return pts


# ── Public API ────────────────────────────────────────────────────────────────

_NEUTRAL = TrendResult(
    direction="neutral", strength=0.0,
    h1_aligned=False, h4_aligned=False, consistent=False,
    ema50_h1=0.0, ema200_h1=0.0, ema50_h4=0.0, ema200_h4=0.0,
    price=0.0, reason="insufficient_data",
)


def analyze(
    candles_h1:  List[Candle],
    candles_h4:  List[Candle],
    ema_fast:    int = 50,
    ema_slow:    int = 200,
) -> TrendResult:
    """
    Determine trend direction and strength from H1 and H4 candle history.

    Args:
        candles_h1:  H1 candles (needs ≥ ema_slow + 5 bars for warmup).
        candles_h4:  H4 candles (same requirement).
        ema_fast:    Fast EMA period (default 50 — structure).
        ema_slow:    Slow EMA period (default 200 — macro trend).

    Returns:
        TrendResult with direction, score, and EMA snapshot values.
    """
    min_bars = ema_slow + 5
    if len(candles_h1) < min_bars:
        log.debug("trend: insufficient H1 bars (%d, need %d)", len(candles_h1), min_bars)
        return _NEUTRAL
    if len(candles_h4) < min_bars:
        log.debug("trend: insufficient H4 bars (%d, need %d)", len(candles_h4), min_bars)
        return _NEUTRAL

    closes_h1 = [c.close for c in candles_h1]
    closes_h4 = [c.close for c in candles_h4]

    fast_h1 = ema(closes_h1, ema_fast)
    slow_h1 = ema(closes_h1, ema_slow)
    fast_h4 = ema(closes_h4, ema_fast)
    slow_h4 = ema(closes_h4, ema_slow)

    if not (fast_h1 and slow_h1 and fast_h4 and slow_h4):
        return _NEUTRAL

    price  = closes_h1[-1]
    e50h1  = fast_h1[-1]
    e200h1 = slow_h1[-1]
    e50h4  = fast_h4[-1]
    e200h4 = slow_h4[-1]

    # H1: full stack alignment
    h1_bull = price > e50h1 > e200h1
    h1_bear = price < e50h1 < e200h1
    h1_aligned = h1_bull or h1_bear

    # H4: EMA alignment (price position on H4 is too noisy for structural bias)
    h4_bull = e50h4 > e200h4
    h4_bear = e50h4 < e200h4
    h4_aligned = h4_bull or h4_bear

    both_bull = h1_bull and h4_bull
    both_bear = h1_bear and h4_bear
    consistent = both_bull or both_bear

    if both_bull:
        direction = "bullish"
    elif both_bear:
        direction = "bearish"
    elif h1_bull or h4_bull:
        direction = "bullish"   # weak — only one frame aligned
    elif h1_bear or h4_bear:
        direction = "bearish"   # weak
    else:
        direction = "neutral"

    strength = _score(h1_aligned, h4_aligned, consistent)

    reason = (
        f"H1={'bull' if h1_bull else 'bear' if h1_bear else 'flat'} "
        f"H4={'bull' if h4_bull else 'bear' if h4_bear else 'flat'} "
        f"consistent={consistent}"
    )

    log.debug("trend: %s strength=%.0f | %s", direction, strength, reason)

    return TrendResult(
        direction  = direction,
        strength   = strength,
        h1_aligned = h1_aligned,
        h4_aligned = h4_aligned,
        consistent = consistent,
        ema50_h1   = round(e50h1,  5),
        ema200_h1  = round(e200h1, 5),
        ema50_h4   = round(e50h4,  5),
        ema200_h4  = round(e200h4, 5),
        price      = round(price,  5),
        reason     = reason,
    )
