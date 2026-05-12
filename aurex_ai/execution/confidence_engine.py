"""
Aurex AI — Confidence Engine

Lightweight 0-100 pre-filter score computed before full strategy analysis.
Eliminates structurally dead markets (no ATR, flat EMAs, off-hours) before
the expensive confluence pipeline runs.

Design principle:
  This is a QUICK GATE, not a strategy filter.  The confluence engine does
  the real filtering.  A score of 30 means "market is alive and tradeable",
  not "this is a high-quality setup".  Keep the threshold low.

Components:
  trend      0-25  EMA alignment on M15 — partial credit for macro trend
  momentum   0-20  5-period Rate of Change (absolute)
  structure  0-20  HH/HL vs LH/LL net consistency over a 10-bar window
  volatility 0-15  ATR-in-pips bracket
  entry      0-20  EMA21 distance from current price

Typical real-market scores:
  Dead / Asian flat       :  5 – 18   → filtered
  Active but choppy       : 20 – 38   → passes (let confluence decide)
  Trending + good entry   : 40 – 70   → passes
  Strong A+ setup         : 70 – 95   → passes
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from aurex_ai.core.data_feed import Candle
from aurex_ai.strategy.trend import ema as _ema
from aurex_ai.core.logger import get_logger

log = get_logger("execution.confidence")

MIN_TRADE_CONFIDENCE = 40   # [Phase 7] base pre-filter raised from 30; use get_confidence_threshold() for adaptive

# Per-symbol minimum confidence thresholds [Phase 8]
# Applied independently of mode.min_confidence — the stricter gate wins.
# Matches settings.yaml per_symbol section exactly.
# Key: strip all broker suffixes before lookup (XAUUSD, not XAUUSD.Z).
_SYMBOL_CONF_FLOOR: dict = {
    "XAUUSD": 72,   # Gold — extreme ATR + frequent false breaks; strictest institutional gate
    "GBPJPY": 68,   # Highest ATR cross; hardest directional predictability
    "GBPUSD": 68,   # High-ATR GBP cable; wide spreads + London-dominated flow
    "USDJPY": 66,   # Yen carry pair; risk-on/off driven; needs macro alignment
    "EURUSD": 66,   # Deepest FX liquidity; still needs strong institutional signal
}


def get_confidence_threshold(utc_hour: int = -1, atr_pips: float = 0.0) -> int:
    """
    Return an adaptive minimum confidence threshold for market state.

    Used as a secondary gate alongside mode.min_confidence and per-symbol
    thresholds.  Raises bar for dead or off-hours markets.

    Args:
        utc_hour:  Current UTC hour (0-23).  Pass -1 when unknown.
        atr_pips:  Recent ATR in pips for the symbol being evaluated.

    Returns:
        Threshold integer in the range [40, 58].
    """
    base = MIN_TRADE_CONFIDENCE   # 40

    # Off-hours penalty: Asian / dead session demands a higher bar.
    # London/NY sessions keep base unchanged.
    if utc_hour >= 0:
        if not (7 <= utc_hour < 21):
            base += 8    # off-hours dead session

    # Volatility penalty: flat market deserves a hard gate.
    if atr_pips < 5.0:
        base += 10   # near-dead volatility; was 7

    return max(40, min(58, base))


def get_symbol_confidence_threshold(
    symbol:    str,
    utc_hour:  int   = -1,
    atr_pips:  float = 0.0,
    cfg=None,
) -> int:
    """
    Return the per-symbol minimum confidence threshold.

    Checks cfg.per_symbol first (runtime override from settings.yaml).
    Falls back to the hardcoded _SYMBOL_CONF_FLOOR dict.
    If the symbol is unknown, returns MIN_TRADE_CONFIDENCE.

    The caller should use max(this, mode.min_confidence, get_confidence_threshold())
    as the effective gate.

    Args:
        symbol:    Broker symbol string (e.g. "XAUUSD", "EURUSD.Z").
        utc_hour:  Current UTC hour for session context (not currently used here;
                   session penalties are applied separately in main.py).
        atr_pips:  ATR for the symbol (not currently used here).
        cfg:       Settings object — checks cfg.per_symbol for overrides.

    Returns:
        Integer minimum confidence threshold for this symbol.
    """
    # Strip broker suffix for lookup: "EURUSD.Z" → "EURUSD", "XAUUSD" → "XAUUSD"
    base = symbol.upper().strip()
    for suffix in (".Z", ".M", ".ECN", ".PRO", ".STP"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break

    # Check runtime config override first
    if cfg is not None:
        try:
            ps = getattr(cfg, "per_symbol", None)
            if ps is not None:
                val = getattr(ps, base, None)
                if val is not None:
                    return int(val)
        except Exception:
            pass

    # Hardcoded institutional floor — apply to known symbols
    for key, floor in _SYMBOL_CONF_FLOOR.items():
        if key in base:
            log.debug("[CONFIDENCE] %s per-symbol floor=%d", symbol, floor)
            return floor

    return MIN_TRADE_CONFIDENCE


@dataclass
class ConfidenceScore:
    total:      int   # 0-100
    trend:      int   # 0-25
    momentum:   int   # 0-20
    structure:  int   # 0-20
    volatility: int   # 0-15
    entry:      int   # 0-20


# ── Component scorers ─────────────────────────────────────────────────────────

def _trend_score(
    candles_m15: List[Candle],
    candles_h1:  Optional[List[Candle]],
) -> int:
    """
    Score trend alignment using H1 candles (primary) with M15 as fallback.

    H1 is preferred because the config fetches 250 H1 bars — enough to warm
    EMA200 (needs 200).  M15 only fetches 150 bars, which is too short for
    a reliable EMA200.

    Partial credit tiers:
      20 pts — full stack  (price > EMA50 > EMA200 or inverse)
      12 pts — macro trend (EMA50 / EMA200 aligned; price in pullback)
       6 pts — EMA50 direction only (EMA200 not available)
       0 pts — no alignment

    +5 pts when M15 short-term direction agrees with H1 trend.
    """
    # ── Primary: H1 EMAs ─────────────────────────────────────────────────────
    if candles_h1 and len(candles_h1) >= 55:
        closes = [c.close for c in candles_h1]
        price  = closes[-1]

        ema50  = _ema(closes, 50)
        ema200 = _ema(closes, 200) if len(closes) >= 200 else None

        if not ema50:
            return 0

        e50 = ema50[-1]

        if ema200:
            e200 = ema200[-1]
            if (price > e50 > e200) or (price < e50 < e200):
                score = 20
            elif e50 > e200 or e50 < e200:
                score = 12   # macro trend; price in pullback — ideal entry zone
            else:
                score = 0
        else:
            # EMA200 not available — give partial credit for EMA50 direction
            score = 6 if price != e50 else 0

        # M15 confirmation bonus
        if score > 0 and len(candles_m15) >= 22:
            closes_m15  = [c.close for c in candles_m15]
            m15_ema50   = _ema(closes_m15, 50)
            if m15_ema50:
                h1_bull  = price > e50
                m15_bull = closes_m15[-1] > m15_ema50[-1]
                if h1_bull == m15_bull:
                    score += 5

        return min(25, score)

    # ── Fallback: M15 EMAs (only when H1 not available) ──────────────────────
    closes_m15 = [c.close for c in candles_m15]
    ema50      = _ema(closes_m15, 50)
    if not ema50:
        return 0

    price = closes_m15[-1]
    e50   = ema50[-1]
    ema200 = _ema(closes_m15, 200)

    if ema200:
        e200 = ema200[-1]
        if (price > e50 > e200) or (price < e50 < e200):
            return 20
        elif e50 > e200 or e50 < e200:
            return 12
        return 0
    return 6 if price != e50 else 0


def _momentum_score(candles_m15: List[Candle]) -> int:
    """5-bar Rate-of-Change (absolute) on M15 — detects directional energy."""
    if len(candles_m15) < 6:
        return 0
    current = candles_m15[-1].close
    past    = candles_m15[-6].close
    if past <= 0:
        return 0
    roc = abs(current - past) / past * 100.0
    if roc >= 0.30:  return 20
    if roc >= 0.15:  return 15
    if roc >= 0.05:  return 8
    if roc >= 0.02:  return 4    # low but non-zero movement
    return 0


def _structure_score(candles_m15: List[Candle]) -> int:
    """Net HH/HL vs LH/LL count over 10 bars — detects orderly directional structure."""
    window = candles_m15[-10:] if len(candles_m15) >= 10 else candles_m15
    if len(window) < 2:
        return 0
    hh = sum(1 for i in range(1, len(window)) if window[i].high > window[i-1].high)
    hl = sum(1 for i in range(1, len(window)) if window[i].low  > window[i-1].low)
    lh = sum(1 for i in range(1, len(window)) if window[i].high < window[i-1].high)
    ll = sum(1 for i in range(1, len(window)) if window[i].low  < window[i-1].low)
    return min(20, abs(hh + hl - ll - lh) * 2)


def _volatility_score(atr_pips: float) -> int:
    """ATR-in-pips bracket — filters dead / sleeping markets."""
    if atr_pips >= 20:  return 15
    if atr_pips >= 12:  return 12
    if atr_pips >= 8:   return 8
    if atr_pips >= 5:   return 4
    return 0


def _entry_score(price: float, candles_m15: List[Candle]) -> int:
    """EMA21 proximity — higher score when price is near the entry EMA."""
    closes = [c.close for c in candles_m15]
    ema21  = _ema(closes, 21)
    if not ema21 or price <= 0:
        return 0
    dist_pct = abs(price - ema21[-1]) / price * 100.0
    if dist_pct <= 0.10:  return 20
    if dist_pct <= 0.25:  return 15
    if dist_pct <= 0.50:  return 10
    if dist_pct <= 1.00:  return 5
    if dist_pct <= 2.50:  return 3   # still somewhat relevant, not completely extended
    return 0


# ── Public API ────────────────────────────────────────────────────────────────

def compute_confidence(
    symbol:      str,
    candles_m15: List[Candle],
    candles_h1:  Optional[List[Candle]],
    price:       float,
    atr_pips:    float,
    pip:         float,
) -> ConfidenceScore:
    """
    Compute a 0-100 confidence score for a symbol.

    Returns 0 only when candle data is insufficient for every component.
    A missing H1 feed reduces the max achievable score by 5 (no H1 bonus).
    """
    t = _trend_score(candles_m15, candles_h1)
    m = _momentum_score(candles_m15)
    s = _structure_score(candles_m15)
    v = _volatility_score(atr_pips)
    e = _entry_score(price, candles_m15)

    total = t + m + s + v + e

    # Dead-market cap: when ATR is below the dead-market floor, the market is
    # sleeping.  Even good trend/entry components can score 45+ on a flat chart.
    # Cap the score so a sleeping market cannot clear the adaptive threshold,
    # which in Asian+flat-ATR conditions rises to 40 (30+3+7).
    # DEAD_CAP=35 < 40 → guaranteed rejection in Asian flat markets.
    # DEAD_CAP=35 > 32 → still allows borderline London/NY flat markets through.
    _ATR_DEAD = 5.0
    _DEAD_CAP = 35
    _dead_capped = False
    if atr_pips < _ATR_DEAD and total > _DEAD_CAP:
        _dead_capped = True
        total = _DEAD_CAP

    log.warning(
        "[CONFIDENCE] %s = %d%s "
        "(trend=%d mom=%d struct=%d vol=%d entry=%d atr=%.1f)",
        symbol, total, " [dead-cap]" if _dead_capped else "",
        t, m, s, v, e, atr_pips,
    )
    return ConfidenceScore(total=total, trend=t, momentum=m, structure=s, volatility=v, entry=e)
