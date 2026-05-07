"""
Aurex AI Signature Strategy — Liquidity Sweep Detection

Institutional traders accumulate orders behind clusters of stop-loss orders
("liquidity pools").  This module identifies three sweep patterns in order
of significance:

  1. Equal-highs / equal-lows sweep  — multiple swing points at the same
     level create a dense stop cluster; highest-probability reversal.
  2. Previous swing high/low taken out — single-touch sweep of a structural
     level with a rejection close.
  3. Stop-hunt wick — isolated candle with a wick extending beyond structure
     and a body closing in the opposite direction.

Sweep terminology (ICT convention):
  "buy_side"  sweep -> price dipped below equal lows / swing lows (hunted
                       longs) then reversed UP  -> signals a BUY opportunity.
  "sell_side" sweep -> price spiked above equal highs / swing highs (hunted
                       shorts) then reversed DOWN -> signals a SELL opportunity.

Score breakdown (max 20):
  Equal-level sweep (2+ touches): 12 + 2×touches + 4×wick_ratio  (cap 20)
  Swing high/low swept:            8 + 6×wick_ratio              (cap 14)
  Pure stop-hunt wick (≥0.65):     4 + 6×wick_ratio              (cap 10)
  No sweep detected:               0
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from aurex_ai.core.data_feed import Candle
from aurex_ai.core.logger import get_logger

log = get_logger("strategy.liquidity")


@dataclass
class SweepResult:
    detected:     bool
    sweep_type:   str    # "buy_side" | "sell_side" | "none"
    strength:     float  # 0–20  (liquidity_score in confluence)
    swept_level:  float  # price level that was swept
    pattern:      str    # "equal_highs"|"equal_lows"|"swing_high"|"swing_low"|"stop_hunt"
    wick_ratio:   float  # directional wick / full candle range
    touches:      int    # how many swing points formed the level
    reason:       str
    displacement: bool   = False  # True when a displacement candle confirms the sweep
    inducement:   bool   = False  # True when a fake sweep preceded the real one


_NONE = SweepResult(
    detected=False, sweep_type="none", strength=0.0,
    swept_level=0.0, pattern="none", wick_ratio=0.0,
    touches=0, reason="no_sweep", displacement=False, inducement=False,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pip_size(symbol: str) -> float:
    s = symbol.upper()
    if "JPY" in s:               return 0.01
    if "BTC" in s or "ETH" in s: return 1.0
    return 0.0001


def _swing_highs(candles: List[Candle], window: int = 3) -> List[Tuple[int, float]]:
    out: List[Tuple[int, float]] = []
    for i in range(window, len(candles) - window):
        h = candles[i].high
        if all(h >= candles[j].high for j in range(i - window, i + window + 1) if j != i):
            out.append((i, h))
    return out


def _swing_lows(candles: List[Candle], window: int = 3) -> List[Tuple[int, float]]:
    out: List[Tuple[int, float]] = []
    for i in range(window, len(candles) - window):
        l = candles[i].low
        if all(l <= candles[j].low for j in range(i - window, i + window + 1) if j != i):
            out.append((i, l))
    return out


def _cluster_levels(
    points: List[Tuple[int, float]],
    tol:    float,
    min_touches: int = 2,
) -> List[Tuple[float, int]]:  # (avg_price, touch_count)
    """Group nearby swing points into equal-level clusters."""
    levels: List[Tuple[float, int]] = []
    used:   set = set()
    sorted_pts = sorted(points, key=lambda x: x[1])

    for i, (_, p1) in enumerate(sorted_pts):
        if i in used:
            continue
        group = [p1]
        for j, (_, p2) in enumerate(sorted_pts[i + 1:], i + 1):
            if abs(p1 - p2) <= tol:
                group.append(p2)
                used.add(j)
        if len(group) >= min_touches:
            levels.append((sum(group) / len(group), len(group)))

    return levels


def _upper_wick_ratio(c: Candle) -> float:
    r = c.candle_range
    return (c.high - max(c.open, c.close)) / r if r > 0 else 0.0


def _lower_wick_ratio(c: Candle) -> float:
    r = c.candle_range
    return (min(c.open, c.close) - c.low) / r if r > 0 else 0.0


def _detect_displacement(
    candles: List[Candle],
    direction: str,           # "buy_side" → look for bullish displacement, "sell_side" → bearish
    lookback:  int = 5,
    body_mult: float = 2.0,   # body must be ≥ body_mult × average body size
) -> bool:
    """
    Detect a displacement candle — an impulsive move confirming institutional participation.

    A displacement candle has:
      • Body ≥ body_mult × average body size of the preceding candles.
      • Closing direction matches the sweep: bullish close for buy_side, bearish for sell_side.

    This is the smart-money "displacement" concept: after sweeping liquidity, institutions
    drive price strongly in the reversal direction, leaving an imbalance (FVG) behind.
    """
    if len(candles) < lookback + 1:
        return False

    recent = candles[-(lookback + 1):-1]    # candles before the signal bar
    signal = candles[-1]

    avg_body = sum(abs(c.close - c.open) for c in recent) / len(recent)
    if avg_body <= 0:
        return False

    sig_body     = abs(signal.close - signal.open)
    sig_bullish  = signal.close > signal.open

    if sig_body < avg_body * body_mult:
        return False

    if direction == "buy_side"  and sig_bullish:
        return True
    if direction == "sell_side" and not sig_bullish:
        return True
    return False


def _detect_inducement(
    highs:  List[Tuple[int, float]],
    lows:   List[Tuple[int, float]],
    level:  float,
    direction: str,    # "sell_side" = equal highs context, "buy_side" = equal lows
    tol:    float,
    event_zone_start: int,
) -> bool:
    """
    Detect inducement: a shallow fake sweep that preceded the real event.

    Inducement pattern:
      sell_side context — price briefly poked above the level (creating a new swing high
      near the level) in the candles before the event zone, but closed back below the level.
      This signals that retail traders were faked out before the real smart money sweep.

    Returns True if a fake-sweep swing point exists between the equal-level cluster and
    the event zone, confirming the multi-layer liquidity pattern.
    """
    if direction == "sell_side":
        # Look for a swing high near 'level' but strictly before the event zone
        for idx, h in highs:
            if idx < event_zone_start and abs(h - level) <= tol * 2:
                return True
    elif direction == "buy_side":
        for idx, l in lows:
            if idx < event_zone_start and abs(l - level) <= tol * 2:
                return True
    return False


# ── Public API ────────────────────────────────────────────────────────────────

def analyze(
    candles:         List[Candle],
    symbol:          str   = "EURUSD.Z",
    lookback:        int   = 50,
    swing_window:    int   = 3,
    equal_tol_pips:  int   = 3,
    min_wick_ratio:  float = 0.55,
) -> SweepResult:
    """
    Scan the last `lookback` candles for a liquidity sweep event.

    The three most recent candles are the "event zone" — this is where we
    check for the sweep wick + rejection close.  Older candles define the
    structural levels being swept.

    Args:
        candles:        M15 candles (or M5 for finer entry signals).
        symbol:         Instrument name — used for pip size calculation.
        lookback:       Bars to scan for swing levels.
        swing_window:   Bars each side to define a swing point.
        equal_tol_pips: Pip tolerance for "equal" highs/lows.
        min_wick_ratio: Minimum wick fraction to qualify as a sweep wick.
    """
    if len(candles) < lookback + swing_window:
        return _NONE

    pip  = _pip_size(symbol)
    tol  = equal_tol_pips * pip
    window     = candles[-lookback:]
    event_zone = window[-5:]          # latest 5 bars = potential sweep candles
    # Index of the first event-zone bar within `window`, used for inducement detection.
    _ez_start  = max(0, len(window) - 5)

    sh = _swing_highs(window, swing_window)
    sl = _swing_lows(window,  swing_window)

    # ── Priority 1: equal-level sweeps ───────────────────────────────────────

    eq_highs = _cluster_levels(sh, tol, min_touches=2)
    eq_lows  = _cluster_levels(sl, tol, min_touches=2)

    # Equal highs swept (price spiked above → closed below) — sell-side sweep → SELL opportunity
    for level, touches in sorted(eq_highs, key=lambda x: -x[1]):
        for c in event_zone:
            if c.high > level and c.close < level:
                wr = _upper_wick_ratio(c)
                if wr >= min_wick_ratio:
                    score = min(20.0, 12.0 + touches * 2.0 + wr * 4.0)
                    # Displacement confirmation: +1 pt bonus (capped at 20)
                    _displace = _detect_displacement(candles, "sell_side")
                    if _displace:
                        score = min(20.0, score + 1.0)
                    _induce = _detect_inducement(sh, sl, level, "sell_side", tol, _ez_start)
                    log.debug(
                        "sweep: equal_highs_swept level=%.5f touches=%d wr=%.2f "
                        "score=%.1f displacement=%s inducement=%s",
                        level, touches, wr, score, _displace, _induce,
                    )
                    return SweepResult(
                        detected=True, sweep_type="sell_side",
                        strength=round(score, 1), swept_level=round(level, 5),
                        pattern="equal_highs", wick_ratio=round(wr, 3),
                        touches=touches,
                        reason=(
                            f"equal_highs level={level:.5f} touches={touches} wr={wr:.2f}"
                            + (" +displacement" if _displace else "")
                            + (" +inducement" if _induce else "")
                        ),
                        displacement=_displace,
                        inducement=_induce,
                    )

    # Equal lows swept (price dipped below → closed above) — buy-side sweep → BUY opportunity
    for level, touches in sorted(eq_lows, key=lambda x: -x[1]):
        for c in event_zone:
            if c.low < level and c.close > level:
                wr = _lower_wick_ratio(c)
                if wr >= min_wick_ratio:
                    score = min(20.0, 12.0 + touches * 2.0 + wr * 4.0)
                    _displace = _detect_displacement(candles, "buy_side")
                    if _displace:
                        score = min(20.0, score + 1.0)
                    _induce = _detect_inducement(sh, sl, level, "buy_side", tol, _ez_start)
                    log.debug(
                        "sweep: equal_lows_swept level=%.5f touches=%d wr=%.2f "
                        "score=%.1f displacement=%s inducement=%s",
                        level, touches, wr, score, _displace, _induce,
                    )
                    return SweepResult(
                        detected=True, sweep_type="buy_side",
                        strength=round(score, 1), swept_level=round(level, 5),
                        pattern="equal_lows", wick_ratio=round(wr, 3),
                        touches=touches,
                        reason=(
                            f"equal_lows level={level:.5f} touches={touches} wr={wr:.2f}"
                            + (" +displacement" if _displace else "")
                            + (" +inducement" if _induce else "")
                        ),
                        displacement=_displace,
                        inducement=_induce,
                    )

    # ── Priority 2: swing high/low single sweep ───────────────────────────────

    if sh:
        _, last_sh = sh[-1]
        for c in event_zone:
            if c.high > last_sh and c.close < last_sh:
                wr    = _upper_wick_ratio(c)
                score = min(14.0, 8.0 + wr * 6.0)
                _displace = _detect_displacement(candles, "sell_side")
                if _displace:
                    score = min(14.0, score + 1.0)
                log.debug("sweep: swing_high_swept level=%.5f wr=%.2f displacement=%s",
                          last_sh, wr, _displace)
                return SweepResult(
                    detected=True, sweep_type="sell_side",
                    strength=round(score, 1), swept_level=round(last_sh, 5),
                    pattern="swing_high", wick_ratio=round(wr, 3),
                    touches=1,
                    reason=f"swing_high={last_sh:.5f} swept wr={wr:.2f}"
                           + (" +displacement" if _displace else ""),
                    displacement=_displace,
                )

    if sl:
        _, last_sl = sl[-1]
        for c in event_zone:
            if c.low < last_sl and c.close > last_sl:
                wr    = _lower_wick_ratio(c)
                score = min(14.0, 8.0 + wr * 6.0)
                _displace = _detect_displacement(candles, "buy_side")
                if _displace:
                    score = min(14.0, score + 1.0)
                log.debug("sweep: swing_low_swept level=%.5f wr=%.2f displacement=%s",
                          last_sl, wr, _displace)
                return SweepResult(
                    detected=True, sweep_type="buy_side",
                    strength=round(score, 1), swept_level=round(last_sl, 5),
                    pattern="swing_low", wick_ratio=round(wr, 3),
                    touches=1,
                    reason=f"swing_low={last_sl:.5f} swept wr={wr:.2f}"
                           + (" +displacement" if _displace else ""),
                    displacement=_displace,
                )

    # ── Priority 3: isolated stop-hunt wick on the last candle ────────────────
    # Minimum wick threshold raised from 0.65 to 0.70 for isolated wicks —
    # this reduces false positives from normal candle variation.

    last = window[-1]
    uwr  = _upper_wick_ratio(last)
    lwr  = _lower_wick_ratio(last)

    if uwr >= 0.70 and last.close < last.open:
        score = min(10.0, 4.0 + uwr * 6.0)
        return SweepResult(
            detected=True, sweep_type="sell_side",
            strength=round(score, 1), swept_level=round(last.high, 5),
            pattern="stop_hunt", wick_ratio=round(uwr, 3),
            touches=1, reason=f"stop_hunt_upper wr={uwr:.2f}",
            displacement=False, inducement=False,
        )

    if lwr >= 0.70 and last.close > last.open:
        score = min(10.0, 4.0 + lwr * 6.0)
        return SweepResult(
            detected=True, sweep_type="buy_side",
            strength=round(score, 1), swept_level=round(last.low, 5),
            pattern="stop_hunt", wick_ratio=round(lwr, 3),
            touches=1, reason=f"stop_hunt_lower wr={lwr:.2f}",
            displacement=False, inducement=False,
        )

    return _NONE
