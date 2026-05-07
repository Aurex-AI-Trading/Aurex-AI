"""
Aurex AI — Fallback Strategy

Activated when the primary AI strategy produces no signals for N consecutive
scan cycles.  Implements two detection methods with runtime context filters.

  Method 1 — Trend + Momentum (higher-quality):
    Requires swing-structure AND EMA slope to agree, then validates with
    Rate-of-Change momentum and last-candle body alignment.

  Method 2 — Breakout (secondary):
    Price breaks above the recent swing high or below the swing low with a
    minimum pip clearance.

Pre-execution filters (applied before any signal is generated):
  - ATR floor:   market must be moving (min_atr_pips)
  - Spread cap:  spread must not be excessive (max_spread_pips)
  - Session:     prime sessions (London/NY) lower the idle threshold

Design constraints:
  - Signal strength is hard-capped at 0.50 -> always Tier 3 by decision engine
  - Does NOT touch primary scoring, confluence, or optimizer logic
  - All SL/TP and position sizing delegated to the existing risk_manager

Logging examples:
  [FALLBACK DEBUG] EURUSD.Z idle=5 threshold=5 session=london atr=14.2 -> TRIGGER
  [FALLBACK] EURUSD.Z BUY (trend+momentum) | strength=0.42
  [FALLBACK DEBUG] GBPUSD.Z idle=3 threshold=5 session=asian atr=6.1 -> SKIP (idle<threshold)
  [FALLBACK DEBUG] USDJPY.Z spread=4.2 pips > max=3.0 -> SKIP (spread too wide)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from aurex_ai.core.data_feed import Candle
from aurex_ai.core.logger import get_logger

log = get_logger("strategy.fallback")

# ── Static defaults (overridden by FallbackConfig from settings) ──────────────
_TREND_LOOKBACK      = 20      # bars for swing-structure analysis
_EMA_PERIOD          = 20      # EMA period for trend confirmation
_MOMENTUM_PERIOD     = 5       # bars for Rate-of-Change
_MOMENTUM_MIN        = 0.0008  # minimum ROC to count as momentum (0.08%)
_BO_LOOKBACK         = 20      # bars for breakout reference range
_BO_EXCLUDE_LAST     = 2       # exclude last N bars to avoid self-anchoring
_BO_MIN_PIPS         = 5.0     # minimum pip clearance above/below range
_MAX_STRENGTH        = 0.50    # hard cap -> always Tier 3
_DEFAULT_MIN_ATR     = 8.0     # pips — below this = flat market, skip
_DEFAULT_MAX_SPREAD  = 3.0     # pips — above this = too expensive, skip
_IDLE_NORMAL         = 5       # cycles before fallback activates (base)
_IDLE_ACTIVE         = 3       # cycles during prime session with high ATR


@dataclass
class FallbackSignal:
    detected:  bool
    direction: str    # "BUY" | "SELL" | "NONE"
    method:    str    # "trend_momentum" | "breakout" | "none"
    strength:  float  # 0.0–0.50 (capped)
    reason:    str


@dataclass
class FallbackConfig:
    """Runtime parameters pulled from settings.yaml [fallback] section."""
    enabled:            bool  = True
    force_trigger:      bool  = False   # skip idle threshold (debug/force use only)
    idle_cycles_normal: int   = _IDLE_NORMAL
    idle_cycles_active: int   = _IDLE_ACTIVE
    max_daily_trades:   int   = 2
    min_atr_pips:       float = _DEFAULT_MIN_ATR
    max_spread_pips:    float = _DEFAULT_MAX_SPREAD
    min_strength:       float = 0.35


_NONE = FallbackSignal(
    detected=False, direction="NONE", method="none", strength=0.0, reason="",
)


# ── ATR ───────────────────────────────────────────────────────────────────────

def _calc_atr(candles: List[Candle], period: int = 14) -> float:
    """Return Average True Range (price units)."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(-period, 0):
        c    = candles[i]
        prev = candles[i - 1].close
        trs.append(max(c.high - c.low, abs(c.high - prev), abs(c.low - prev)))
    return sum(trs) / len(trs)


def calc_atr_pips(candles: List[Candle], pip_size: float, period: int = 14) -> float:
    """Return ATR in pips.  Exported for use by the main loop."""
    if pip_size <= 0:
        return 0.0
    return round(_calc_atr(candles, period) / pip_size, 1)


# ── Session helpers ───────────────────────────────────────────────────────────

def _session_name(utc_hour: int) -> str:
    if 7 <= utc_hour < 13:
        return "london"
    if 13 <= utc_hour < 16:
        return "overlap"
    if 16 <= utc_hour < 22:
        return "newyork"
    return "asian"


def is_prime_session(utc_hour: int) -> bool:
    """True if current hour is London, Overlap, or New York session."""
    return 7 <= utc_hour < 22


def get_idle_threshold(
    utc_hour:  int,
    atr_pips:  float,
    base:      int   = _IDLE_NORMAL,
    active:    int   = _IDLE_ACTIVE,
    min_atr:   float = _DEFAULT_MIN_ATR,
) -> int:
    """
    Return the idle-cycle threshold for this market context.

    During London/NY sessions with sufficient ATR, reduce the threshold to
    `active` (default 3) so the fallback responds faster to opportunity.
    Otherwise, require `base` cycles (default 5).
    """
    if is_prime_session(utc_hour) and atr_pips >= min_atr:
        return active
    return base


# ── Internal utilities ────────────────────────────────────────────────────────

def _ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    k   = 2.0 / (period + 1)
    val = values[0]
    for v in values[1:]:
        val = v * k + val * (1.0 - k)
    return val


# ── Trend detection (swing structure) ────────────────────────────────────────

def detect_trend(candles: List[Candle], lookback: int = _TREND_LOOKBACK) -> str:
    """
    Detect trend by comparing HH/HL structure across two halves of the window.

    Returns "bullish" | "bearish" | "neutral".
    """
    if len(candles) < lookback:
        return "neutral"

    window = candles[-lookback:]
    mid    = len(window) // 2

    first_high  = max(c.high for c in window[:mid])
    second_high = max(c.high for c in window[mid:])
    first_low   = min(c.low  for c in window[:mid])
    second_low  = min(c.low  for c in window[mid:])

    if second_high > first_high and second_low > first_low:
        return "bullish"
    if second_high < first_high and second_low < first_low:
        return "bearish"
    return "neutral"


# ── Trend confirmation (EMA slope) ────────────────────────────────────────────

def detect_trend_ema(candles: List[Candle], period: int = _EMA_PERIOD) -> str:
    """
    Confirm trend via EMA position.  0.1% buffer filters ranging price.

    Returns "bullish" | "bearish" | "neutral".
    """
    needed = period + 5
    if len(candles) < needed:
        return "neutral"

    closes  = [c.close for c in candles[-needed:]]
    ema_val = _ema(closes, period)

    if ema_val <= 0:
        return "neutral"

    ratio = closes[-1] / ema_val
    if ratio > 1.001:
        return "bullish"
    if ratio < 0.999:
        return "bearish"
    return "neutral"


# ── Momentum (Rate-of-Change) ─────────────────────────────────────────────────

def detect_momentum(
    candles:   List[Candle],
    direction: str,
    period:    int   = _MOMENTUM_PERIOD,
    threshold: float = _MOMENTUM_MIN,
) -> bool:
    """
    Return True if the ROC over `period` bars confirms `direction`.

    BUY  requires ROC > +threshold.
    SELL requires ROC < -threshold.
    """
    if len(candles) < period + 2:
        return False

    ref  = candles[-period - 1].close
    curr = candles[-1].close
    if ref <= 0:
        return False

    roc = (curr - ref) / ref
    if direction == "BUY":
        return roc > threshold
    if direction == "SELL":
        return roc < -threshold
    return False


def _last_candle_confirms(candles: List[Candle], direction: str) -> bool:
    """True if the most recent candle body aligns with direction."""
    if not candles:
        return False
    c = candles[-1]
    if direction == "BUY":
        return c.close > c.open
    if direction == "SELL":
        return c.close < c.open
    return False


# ── Breakout detection ────────────────────────────────────────────────────────

def detect_breakout(
    candles:  List[Candle],
    price:    float,
    pip_size: float = 0.0001,
    lookback: int   = _BO_LOOKBACK,
    exclude:  int   = _BO_EXCLUDE_LAST,
    min_pips: float = _BO_MIN_PIPS,
) -> Tuple[str, float]:
    """
    Detect price clearing the recent swing range by at least `min_pips`.

    Reference range: candles[-(lookback+exclude):-exclude] — excludes the
    last `exclude` bars so the breakout candle doesn't anchor its own range.

    Returns ("BUY", pips) | ("SELL", pips) | ("NONE", 0.0).
    """
    if len(candles) < lookback + exclude + 2:
        return "NONE", 0.0

    ref_end   = -exclude if exclude > 0 else len(candles)
    ref_start = ref_end - lookback
    window    = candles[ref_start:ref_end]

    if not window:
        return "NONE", 0.0

    swing_high = max(c.high for c in window)
    swing_low  = min(c.low  for c in window)
    min_move   = min_pips * pip_size

    if price > swing_high + min_move:
        return "BUY",  (price - swing_high) / pip_size
    if price < swing_low - min_move:
        return "SELL", (swing_low - price) / pip_size
    return "NONE", 0.0


# ── Public API ────────────────────────────────────────────────────────────────

def analyze(
    candles:     List[Candle],
    price:       float,
    symbol:      str         = "",
    pip_size:    float       = 0.0001,
    spread_pips: float       = 0.0,
    cfg:         FallbackConfig = None,   # type: ignore[assignment]
) -> FallbackSignal:
    """
    Run pre-execution filters then all detection methods.

    Pre-filters (applied first — fail-fast, log reason):
      1. Sufficient candles
      2. ATR above minimum (market is moving)
      3. Spread below maximum (market is tradeable)

    Detection priority:
      1. Trend + Momentum  (swing structure + EMA + ROC + candle body)
      2. Breakout          (pip clearance above/below swing range)

    Strength is capped at 0.50 -> always Tier 3.

    Args:
        candles:      M15 candle list (most recent at index -1).
        price:        Current price (last close).
        symbol:       Instrument name (logging only).
        pip_size:     Symbol pip size.
        spread_pips:  Current spread in pips (0 = skip spread check).
        cfg:          FallbackConfig; uses defaults if None.

    Returns:
        FallbackSignal — detected=False if blocked or no setup found.
    """
    _cfg = cfg if cfg is not None else FallbackConfig()

    # ── Candle floor ──────────────────────────────────────────────────────────
    min_bars = max(_TREND_LOOKBACK, _EMA_PERIOD + 5, _BO_LOOKBACK + _BO_EXCLUDE_LAST + 2)
    if len(candles) < min_bars:
        log.debug("[FALLBACK] %s — insufficient candles (%d < %d)", symbol, len(candles), min_bars)
        return _NONE

    # ── ATR floor (flat market guard) ─────────────────────────────────────────
    atr_v = calc_atr_pips(candles, pip_size)
    if atr_v < _cfg.min_atr_pips:
        log.info(
            "[FALLBACK DEBUG] %s ATR=%.1f pips < min=%.1f -> SKIP (flat market)",
            symbol, atr_v, _cfg.min_atr_pips,
        )
        return _NONE

    # ── Spread cap ────────────────────────────────────────────────────────────
    if spread_pips > 0 and spread_pips > _cfg.max_spread_pips:
        log.info(
            "[FALLBACK DEBUG] %s spread=%.1f pips > max=%.1f -> SKIP (wide spread)",
            symbol, spread_pips, _cfg.max_spread_pips,
        )
        return _NONE

    # ── Method 1: Trend + Momentum ────────────────────────────────────────────
    structure = detect_trend(candles)
    ema_dir   = detect_trend_ema(candles)

    if structure != "neutral" and structure == ema_dir:
        direction    = "BUY" if structure == "bullish" else "SELL"
        has_momentum = detect_momentum(candles, direction)
        candle_ok    = _last_candle_confirms(candles, direction)

        if has_momentum and candle_ok:
            strength = round(min(_MAX_STRENGTH, 0.42), 2)
            reason   = (
                f"structure={structure} ema={ema_dir} "
                f"roc=confirmed candle=confirmed atr={atr_v:.1f}pips"
            )
            log.warning(
                "[FALLBACK] %s %s (trend+momentum) | strength=%.2f | %s",
                symbol, direction, strength, reason,
            )
            return FallbackSignal(
                detected  = True,
                direction = direction,
                method    = "trend_momentum",
                strength  = strength,
                reason    = reason,
            )

    # ── Method 2: Breakout ────────────────────────────────────────────────────
    bo_dir, bo_pips = detect_breakout(candles, price, pip_size=pip_size)

    if bo_dir != "NONE" and bo_pips > 0:
        strength = round(min(_MAX_STRENGTH, 0.30 + min(0.20, bo_pips / 80.0)), 2)
        reason   = (
            f"price broke {bo_dir} swing range by {bo_pips:.1f} pips "
            f"atr={atr_v:.1f}pips"
        )
        log.warning(
            "[FALLBACK] %s %s (breakout) | pips=%.1f strength=%.2f",
            symbol, bo_dir, bo_pips, strength,
        )
        return FallbackSignal(
            detected  = True,
            direction = bo_dir,
            method    = "breakout",
            strength  = strength,
            reason    = reason,
        )

    log.debug(
        "[FALLBACK] %s — no setup (structure=%s ema=%s atr=%.1f)",
        symbol, structure, ema_dir, atr_v,
    )
    return _NONE
