"""
Aurex AI — Order Block Detection

An Order Block (OB) is the last opposing candle before a significant impulse move.
It represents institutional accumulation/distribution and acts as a high-probability
support/resistance zone.

Bullish OB: Last bearish candle before a bullish impulse (price may retrace to it)
Bearish OB: Last bullish candle before a bearish impulse (price may retrace to it)

Scoring (0–15):
  - Price inside OB zone:              15
  - Price approaching OB (within 1×):  8
  - OB exists but price far:           4
  - No OB:                             0
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from aurex_ai.core.data_feed import Candle
from aurex_ai.core.logger import get_logger

log = get_logger("strategy.order_blocks")


@dataclass
class OrderBlock:
    high: float
    low:  float
    bullish: bool        # True = bullish OB (demand zone), False = bearish OB (supply)
    index: int           # bar index in the lookback window
    strength: float      # impulse move size in pips


@dataclass
class OBResult:
    present:    bool
    ob_type:    str                    # "bullish" | "bearish" | "none"
    active_ob:  Optional[OrderBlock]
    all_obs:    List[OrderBlock]
    score:      float                  # 0–15
    price_in_ob: bool


def analyze(
    candles:        List[Candle],
    price:          float,
    symbol:         str    = "",
    lookback:       int    = 30,
    min_impulse_pips: float = 8.0,
    pip_size:       float  = 0.0001,
    max_score:      float  = 15.0,
) -> OBResult:
    """
    Scan recent candles for valid order blocks and score proximity to price.

    Args:
        candles:          M15 candle list (newest at index -1)
        price:            Current price (last close)
        lookback:         Number of candles to scan
        min_impulse_pips: Minimum impulse size to qualify as OB
        pip_size:         Pip size for the symbol
        max_score:        Maximum score (default 15)

    Returns:
        OBResult with detected OBs and proximity score
    """
    _EMPTY = OBResult(
        present=False, ob_type="none", active_ob=None,
        all_obs=[], score=0.0, price_in_ob=False,
    )

    window = candles[-lookback:] if len(candles) >= lookback else candles[:]
    if len(window) < 4:
        return _EMPTY

    obs: List[OrderBlock] = []
    min_impulse = min_impulse_pips * pip_size

    # ── Scan for order blocks ─────────────────────────────────────────────────
    # Look for: opposing candle -> gap/impulse -> continuation candles
    for i in range(1, len(window) - 2):
        ob_candle = window[i]
        next_candle = window[i + 1]

        ob_body = abs(ob_candle.close - ob_candle.open)
        if ob_body < pip_size * 2:
            continue  # doji / very small candle

        # ── Bullish OB: bearish candle -> bullish impulse ──────────────────────
        if ob_candle.close < ob_candle.open:  # bearish ob candle
            impulse = sum(
                abs(window[j].close - window[j].open)
                for j in range(i + 1, min(i + 4, len(window)))
                if window[j].close > window[j].open  # only bullish impulse candles
            )
            if impulse >= min_impulse:
                obs.append(OrderBlock(
                    high=ob_candle.high,
                    low=ob_candle.low,
                    bullish=True,
                    index=i,
                    strength=impulse / pip_size,
                ))

        # ── Bearish OB: bullish candle -> bearish impulse ──────────────────────
        elif ob_candle.close > ob_candle.open:  # bullish ob candle
            impulse = sum(
                abs(window[j].close - window[j].open)
                for j in range(i + 1, min(i + 4, len(window)))
                if window[j].close < window[j].open  # only bearish impulse candles
            )
            if impulse >= min_impulse:
                obs.append(OrderBlock(
                    high=ob_candle.high,
                    low=ob_candle.low,
                    bullish=False,
                    index=i,
                    strength=impulse / pip_size,
                ))

    if not obs:
        log.debug("OB: no order blocks detected for %s", symbol)
        return _EMPTY

    # ── Find most relevant OB (most recent, closest to price) ─────────────────
    # Prefer OBs that price is inside or closest to
    def ob_distance(ob: OrderBlock) -> float:
        if ob.low <= price <= ob.high:
            return 0.0  # inside
        return min(abs(price - ob.high), abs(price - ob.low))

    best_ob = min(obs, key=lambda ob: ob_distance(ob))
    dist = ob_distance(best_ob)
    ob_size = best_ob.high - best_ob.low

    price_in_ob = dist == 0.0
    ob_type = "bullish" if best_ob.bullish else "bearish"

    # ── Score ─────────────────────────────────────────────────────────────────
    if price_in_ob:
        score = max_score                           # 15: price inside OB
    elif ob_size > 0 and dist <= ob_size:
        score = max_score * 0.53                    # 8: price within 1× OB size
    else:
        score = max_score * 0.27                    # 4: OB exists, price far

    score = round(score, 1)

    log.debug(
        "OB [%s]: type=%s price_in_ob=%s best=%.5f-%.5f dist_pips=%.1f score=%.1f",
        symbol, ob_type, price_in_ob,
        best_ob.low, best_ob.high,
        dist / pip_size, score,
    )

    return OBResult(
        present    = True,
        ob_type    = ob_type,
        active_ob  = best_ob,
        all_obs    = obs,
        score      = score,
        price_in_ob = price_in_ob,
    )
