"""
Aurex AI — Market Structure Detection

Break of Structure (BOS) and trend bias from M15/H1 candle data.

BOS detection (last 10 structure candles before the signal bar):
  BUY  BOS: signal bar close > max(high) of last 10 candles
  SELL BOS: signal bar close < min(low)  of last 10 candles

Trend bias fallback (last 10 structure candles, first half vs second half):
  Second-half max(high) > first-half max(high) → buy  bias
  Second-half min(low)  < first-half min(low)  → sell bias
  Otherwise                                    → neutral

Composite score (used as structure bonus in confluence):
  BOS confirmed AND sweep detected → 25  (strong)
  BOS confirmed only               → 15  (moderate)
  Trend bias only (no BOS)         →  5  (weak)
  None of the above                →  0

Minimum required: 30 candles on M15 or H1.  Returns neutral on insufficient
data and emits a [STRUCTURE] insufficient_data warning.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from aurex_ai.core.data_feed import Candle
from aurex_ai.core.logger import get_logger

log = get_logger("strategy.structure")

_MIN_CANDLES = 30    # guard — M15 fetch provides 150 so this is always met in live
_LOOKBACK    = 10    # structure window size (candles before signal bar)


@dataclass
class StructureResult:
    bos_detected:  bool
    bos_direction: str    # "buy" | "sell" | "none"
    trend_bias:    str    # "buy" | "sell" | "neutral"
    score:         float  # 0 | 5 | 15 | 25
    reason:        str


_NEUTRAL = StructureResult(
    bos_detected=False, bos_direction="none",
    trend_bias="neutral", score=0.0, reason="insufficient_data",
)


# ── Trend bias (first half vs second half of structure window) ─────────────────

def _trend_bias(candles: List[Candle]) -> str:
    """
    Compare highs/lows of the first vs second half of the structure window.
    Returns "buy" if second half has higher highs, "sell" if lower lows.
    """
    n = len(candles)
    if n < 4:
        return "neutral"

    mid         = n // 2
    first_half  = candles[:mid]
    second_half = candles[mid:]

    if max(c.high for c in second_half) > max(c.high for c in first_half):
        return "buy"
    if min(c.low for c in second_half) < min(c.low for c in first_half):
        return "sell"
    return "neutral"


# ── Public API ────────────────────────────────────────────────────────────────

def analyze(
    candles:        List[Candle],
    sweep_detected: bool = False,
    swing_window:   int  = 3,    # kept for call-site compatibility; not used
) -> StructureResult:
    """
    Detect Break of Structure and trend bias from M15 or H1 candle data.

    Args:
        candles:        M15 or H1 candles (newest at index -1).
                        Must be at least 30 bars; returns neutral otherwise.
        sweep_detected: True when liquidity.analyze() confirmed a sweep event.
        swing_window:   Kept for API compatibility — not used in detection.

    Returns:
        StructureResult with bos_detected, bos_direction, trend_bias, score,
        and a human-readable reason string.
    """
    if len(candles) < _MIN_CANDLES:
        log.warning(
            "[STRUCTURE] insufficient_data — got %d candles (need ≥%d)",
            len(candles), _MIN_CANDLES,
        )
        return _NEUTRAL

    # Structure window: last _LOOKBACK candles before the signal bar
    structure    = candles[-(_LOOKBACK + 1):-1]   # excludes signal bar
    signal_close = candles[-1].close

    # ── BOS detection ─────────────────────────────────────────────────────────
    recent_high   = max(c.high for c in structure)
    recent_low    = min(c.low  for c in structure)
    bos_direction = "none"

    if signal_close > recent_high:
        bos_direction = "buy"
    elif signal_close < recent_low:
        bos_direction = "sell"

    bos_detected = bos_direction != "none"

    # ── Trend bias fallback ───────────────────────────────────────────────────
    bias = _trend_bias(structure)

    # ── Composite score (sweep is bonus, not a requirement) ───────────────────
    if bos_detected and sweep_detected:
        score, label = 25.0, "strong"
    elif bos_detected:
        score, label = 15.0, "moderate"
    elif bias != "neutral":
        score, label = 5.0, "weak"
    else:
        score, label = 0.0, "none"

    reason = (
        f"bos={bos_detected}({bos_direction}) "
        f"bias={bias} sweep={sweep_detected} "
        f"strength={label} score={score:.0f}"
    )

    log.warning(
        "[STRUCTURE] bos=%s sweep=%s trend_bias=%s score=%.0f",
        bos_detected, sweep_detected, bias, score,
    )

    return StructureResult(
        bos_detected  = bos_detected,
        bos_direction = bos_direction,
        trend_bias    = bias,
        score         = score,
        reason        = reason,
    )
