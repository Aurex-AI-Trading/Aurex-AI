"""
Aurex AI Signature Strategy — Scoring Model

Computes the two sub-scores that live in the execution layer (not in strategy
modules) because they require the resolved trade direction:

  1. EMA Entry Score (0–10)
     How close is current price to EMA21?
     Closer = higher score.  No hard rejection — distance decays score smoothly.

  2. Confirmation Score (0–10)
     Quality of the most recent candle pattern in the signal direction:
       strong_engulfing  -> 10
       engulfing         -> 8
       rejection_wick    -> 7
       momentum_candle   -> 6
       no_pattern        -> 0

Both scores feed directly into ConfluenceResult via confluence.combine().
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Tuple

from aurex_ai.core.data_feed import Candle
from aurex_ai.strategy.trend import ema as _ema_fn   # reuse the same EMA function
from aurex_ai.core.logger import get_logger

log = get_logger("execution.scoring")


@dataclass
class EMAScore:
    score:       float   # 0–10
    ema_value:   float
    distance:    float   # absolute price distance
    distance_pct: float  # % of price


@dataclass
class ConfirmationScore:
    score:   float   # 0–10
    pattern: str     # pattern name


# ── EMA proximity score ───────────────────────────────────────────────────────

def compute_ema_score(
    price:         float,
    candles_m15:   List[Candle],
    ema_period:    int   = 21,
    max_score:     float = 10.0,
    threshold_pct: float = 0.50,   # price within 0.50% of EMA -> full decay range
) -> EMAScore:
    """
    Score how close `price` is to EMA(`ema_period`) on M15.

    Formula (linear decay):
      distance_pct = |price - EMA| / price × 100
      score = max_score × (1 - distance_pct / threshold_pct)
      score clamped to [0, max_score]

    Args:
        price:         Current price (last M15 close).
        candles_m15:   M15 history for EMA computation.
        ema_period:    Default 21 — entry EMA.
        threshold_pct: Distance at which score reaches 0 (default 0.50%).

    Returns:
        EMAScore with numeric score and diagnostic fields.
    """
    closes   = [c.close for c in candles_m15]
    ema_vals = _ema_fn(closes, ema_period)

    if not ema_vals:
        log.debug("ema_score: insufficient candles for EMA%d", ema_period)
        return EMAScore(score=0.0, ema_value=0.0, distance=0.0, distance_pct=0.0)

    ema_val      = ema_vals[-1]
    distance     = abs(price - ema_val)
    distance_pct = (distance / price * 100.0) if price > 0 else 0.0

    if distance_pct >= threshold_pct:
        score = 0.0
    else:
        score = max_score * (1.0 - distance_pct / threshold_pct)

    score = round(max(0.0, min(max_score, score)), 2)

    log.debug(
        "ema_score: EMA%d=%.5f price=%.5f dist_pct=%.4f%% score=%.2f",
        ema_period, ema_val, price, distance_pct, score,
    )

    return EMAScore(
        score        = score,
        ema_value    = round(ema_val,      5),
        distance     = round(distance,     6),
        distance_pct = round(distance_pct, 4),
    )


# ── Candle confirmation score ─────────────────────────────────────────────────

def compute_confirmation_score(
    candles:   List[Candle],
    direction: str,            # "BUY" | "SELL"
    max_score: float = 10.0,
) -> ConfirmationScore:
    """
    Score the quality of the most recent candle pattern in `direction`.

    Patterns checked (in priority order):
      1. Strong engulfing  — current body ≥ 1.5× previous body, correct direction
      2. Engulfing         — current candle engulfs previous body
      3. Rejection wick    — wick opposing direction is ≥ 60% of candle range
      4. Momentum candle   — body ≥ 70% of range, in correct direction

    Args:
        candles:   At least 2 candles.  Uses the last two.
        direction: Trade direction to match patterns against.

    Returns:
        ConfirmationScore with score and pattern label.
    """
    _NONE = ConfirmationScore(score=0.0, pattern="none")

    if len(candles) < 2:
        return _NONE

    cur  = candles[-1]
    prev = candles[-2]

    cur_body  = abs(cur.close  - cur.open)
    prev_body = abs(prev.close - prev.open)

    # ── 1. Engulfing patterns ─────────────────────────────────────────────────
    if direction == "BUY":
        is_directional = cur.close > cur.open   # bullish candle
        engulfs_body   = (cur.open <= prev.close) and (cur.close >= prev.open)
    else:
        is_directional = cur.close < cur.open   # bearish candle
        engulfs_body   = (cur.open >= prev.close) and (cur.close <= prev.open)

    if is_directional and engulfs_body:
        body_ratio = (cur_body / prev_body) if prev_body > 0 else 1.0
        if body_ratio >= 1.5:
            log.debug("confirmation: strong_engulfing dir=%s ratio=%.2f", direction, body_ratio)
            return ConfirmationScore(score=max_score, pattern="strong_engulfing")
        log.debug("confirmation: engulfing dir=%s", direction)
        return ConfirmationScore(score=round(max_score * 0.80, 1), pattern="engulfing")

    # ── 2. Rejection wick ─────────────────────────────────────────────────────
    if cur.candle_range > 0:
        if direction == "BUY":
            wick_ratio = cur.lower_wick / cur.candle_range
        else:
            wick_ratio = cur.upper_wick / cur.candle_range

        if wick_ratio >= 0.60:
            score = round(max_score * 0.70, 1)
            log.debug("confirmation: rejection_wick dir=%s ratio=%.2f", direction, wick_ratio)
            return ConfirmationScore(score=score, pattern="rejection_wick")

    # ── 3. Momentum candle ────────────────────────────────────────────────────
    if cur.candle_range > 0:
        body_pct = cur_body / cur.candle_range
        if body_pct >= 0.70 and is_directional:
            score = round(max_score * 0.60, 1)
            log.debug("confirmation: momentum_candle dir=%s body_pct=%.2f", direction, body_pct)
            return ConfirmationScore(score=score, pattern="momentum_candle")

    return _NONE
