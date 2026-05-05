"""
Aurex AI — Adaptive Execution Engine

Replaces binary entry quality gates with continuous position-size multipliers.
Every condition that was formerly a hard block now becomes a proportional
size reduction.  The only true hard block is ATR=0 (corrupted price data).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionFactors:
    session_factor:    float = 1.0
    volatility_factor: float = 1.0
    momentum_factor:   float = 1.0
    pullback_factor:   float = 1.0
    mtf_factor:        float = 1.0
    reasons:           dict  = field(default_factory=dict)

    @property
    def composite(self) -> float:
        raw = (
            self.session_factor
            * self.volatility_factor
            * self.momentum_factor
            * self.pullback_factor
            * self.mtf_factor
        )
        return max(0.10, min(1.5, round(raw, 3)))

    @property
    def reduction_pct(self) -> int:
        return max(0, round((1.0 - self.composite) * 100))

    def log_summary(self, symbol: str, direction: str, logger: Any) -> None:
        if self.reasons:
            logger.warning(
                "[EXECUTION ADJUSTED] %s %s composite=%.2f reduction=%d%% | %s",
                symbol, direction, self.composite, self.reduction_pct,
                " | ".join(f"{k}={v}" for k, v in self.reasons.items()),
            )
        else:
            logger.info(
                "[EXECUTION OPTIMAL] %s %s composite=%.2f — all factors pass",
                symbol, direction, self.composite,
            )


def compute_session_factor(utc_hour: int) -> tuple[float, str]:
    """
    Session quality factor by UTC hour.
    London + NY (07-21): 1.0 — peak liquidity.
    Pre-London (06): 0.80 — setup window, trending about to start.
    Asian active (02-06): 0.65 — lower volatility, noisier price action.
    Dead hours (21-02): 0.50 — thin liquidity, erratic moves.
    """
    if 7 <= utc_hour < 21:
        return 1.0, ""
    if utc_hour == 6:
        return 0.80, f"pre_london(UTC={utc_hour:02d})"
    if 2 <= utc_hour < 6:
        return 0.65, f"asian_session(UTC={utc_hour:02d})"
    return 0.50, f"off_session(UTC={utc_hour:02d})"


def compute_volatility_factor(
    atr_pips: float,
    min_atr:  float = 80.0,
    max_atr:  float = 200.0,
) -> tuple[float, str]:
    """
    Proportional lot reduction when ATR is outside the healthy range.
    Returns (0.0, reason) only when ATR=0 (corrupted data — the one true hard block).
    """
    if atr_pips <= 0:
        return 0.0, "atr_zero"

    if min_atr > 0 and atr_pips < min_atr:
        factor = max(0.10, atr_pips / min_atr)
        return round(factor, 3), f"atr_low({atr_pips:.0f}<{min_atr:.0f})"

    if max_atr > 0 and atr_pips > max_atr:
        factor = max(0.50, max_atr / atr_pips)
        return round(factor, 3), f"atr_high({atr_pips:.0f}>{max_atr:.0f})"

    return 1.0, ""


def compute_momentum_factor(candles: list, direction: str) -> tuple[float, str]:
    """
    Body-to-range ratio → size factor.
    body >= 40%: 1.0   (healthy momentum)
    body >= 30%: 0.7   (-30%)
    body <  30%: 0.5   (-50%)
    Wrong-direction close: capped at body_ratio (aligned with candle quality).
    """
    if not candles:
        return 1.0, ""

    last = candles[-1]
    c_range = last.high - last.low
    if c_range <= 0:
        return 1.0, ""

    body_ratio = abs(last.close - last.open) / c_range
    bullish    = last.close >= last.open

    # Direction mismatch — additional penalty on top of body ratio
    if direction == "BUY" and not bullish:
        factor = max(0.50, round(body_ratio, 3))
        return factor, f"bearish_candle(body={body_ratio:.0%})"
    if direction == "SELL" and bullish:
        factor = max(0.50, round(body_ratio, 3))
        return factor, f"bullish_candle(body={body_ratio:.0%})"

    if body_ratio >= 0.40:
        return 1.0, ""
    if body_ratio >= 0.30:
        return 0.7, f"weak_body({body_ratio:.0%})"
    return 0.5, f"doji({body_ratio:.0%})"


def compute_pullback_factor(
    candles:          list,
    direction:        str,
    atr_pips:         float,
    pip:              float,
    pullback_min_atr: float = 0.2,
    pullback_max_atr: float = 2.5,
) -> tuple[float, str]:
    """
    ATR-adaptive pullback zone: [ATR×min_atr, ATR×max_atr].
    Inside zone  → 1.0
    Shallow (<min): 0.7  (price still near swing extreme, minor penalty)
    Deep (>max):    0.5  (price has retraced too far from setup)
    """
    if not candles or atr_pips <= 0 or pip <= 0:
        return 1.0, ""

    swing_window = min(20, len(candles) - 1)
    if swing_window < 3:
        return 1.0, ""

    recent = candles[-swing_window - 1:-1]
    price  = candles[-1].close

    if direction == "BUY":
        swing_level   = max(c.high for c in recent)
        pullback_pips = (swing_level - price) / pip
    else:
        swing_level   = min(c.low for c in recent)
        pullback_pips = (price - swing_level) / pip

    pb_min = pullback_min_atr * atr_pips
    pb_max = pullback_max_atr * atr_pips

    if pullback_pips < pb_min:
        return 0.7, f"pullback_shallow({pullback_pips:.0f}<{pb_min:.0f}pips)"
    if pullback_pips > pb_max:
        return 0.5, f"pullback_deep({pullback_pips:.0f}>{pb_max:.0f}pips)"
    return 1.0, ""


def compute_mtf_factor(h1_dir: str, m5_dir: str) -> tuple[float, str]:
    """H1 vs M5 timeframe alignment — mismatch reduces size by 40%."""
    if m5_dir == "neutral" or h1_dir == m5_dir:
        return 1.0, ""
    return 0.6, f"mtf_conflict(H1={h1_dir},M5={m5_dir})"


def build_execution_factors(
    direction:        str,
    candles_m5:       list,
    atr_pips:         float,
    pip:              float,
    utc_hour:         int,
    h1_dir:           str  = "",
    m5_dir:           str  = "",
    min_atr_pips:     float = 80.0,
    max_atr_pips:     float = 200.0,
    pullback_min_atr: float = 0.20,
    pullback_max_atr: float = 2.50,
) -> ExecutionFactors:
    """
    Compute all execution factors in one call.
    Returns ExecutionFactors with individual factors and merged reasons dict.
    """
    sf, s_reason  = compute_session_factor(utc_hour)
    vf, v_reason  = compute_volatility_factor(atr_pips, min_atr_pips, max_atr_pips)
    mf, m_reason  = compute_momentum_factor(candles_m5, direction)
    pf, p_reason  = compute_pullback_factor(candles_m5, direction, atr_pips, pip,
                                            pullback_min_atr, pullback_max_atr)
    tf, t_reason  = compute_mtf_factor(h1_dir, m5_dir)

    reasons: dict = {}
    if s_reason: reasons["session"]    = s_reason
    if v_reason: reasons["volatility"] = v_reason
    if m_reason: reasons["momentum"]   = m_reason
    if p_reason: reasons["pullback"]   = p_reason
    if t_reason: reasons["mtf"]        = t_reason

    return ExecutionFactors(
        session_factor    = sf,
        volatility_factor = vf,
        momentum_factor   = mf,
        pullback_factor   = pf,
        mtf_factor        = tf,
        reasons           = reasons,
    )
