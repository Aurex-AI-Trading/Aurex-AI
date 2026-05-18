"""
Aurex AI — Pair Intelligence Engine  (Phase 10)

Dynamic per-scan quality evaluation for symbols with bespoke institutional
profiles (currently GBPJPY; extensible to any symbol with a full registry entry).

Design contract
---------------
  This module is called ONCE per scan, AFTER the ATR / basic candle data are
  available but BEFORE the expensive confluence pipeline runs.  It replaces
  hard-coded symbol-if-blocks in main.py with a clean, data-driven gate.

  evaluate_symbol_quality() returns a PairConditions dataclass.  The caller
  (main.py) reads the fields directly:

    result.allowed          → False  means hard-block, return None from scan
    result.lot_mult_cap     → float  cap applied to combined_size_mult
    result.scoring_mods     → dict   passed to confluence combine() weights
    result.exec_mult_override→ float | None  overrides market_state.exec_mult
    result.atr_size_mult    → float  additional size factor for non-ideal ATR
    result.block_reason     → str    human-readable block reason (if not allowed)

ATR quality tiers
-----------------
  below min_atr_pips        → BLOCK (dead/Asian)
  [min, ideal_min)          → ALLOW, atr_size_mult = 0.75 (low momentum)
  [ideal_min, ideal_max]    → ALLOW, atr_size_mult = 1.00 (sweet spot)
  (ideal_max, max_atr_pips] → ALLOW, atr_size_mult = 0.80 (elevated spike risk)
  above max_atr_pips        → BLOCK (flash crash / extreme news)

Log tags
--------
  [PAIR INTELLIGENCE]  [GBPJPY QUALITY]  [PAIR BLOCK]  [PAIR ALLOW]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from aurex_ai.core.symbol_registry import (
    get_profile,
    get_atr_gates,
    get_lot_mult_cap,
    get_regime_mult,
    get_scoring_modifiers,
)
from aurex_ai.core.logger import get_logger

log = get_logger("core.pair_intelligence")

# Session hour ranges (UTC)
_ASIAN_START   = 0
_ASIAN_END     = 7    # 00:00–06:59 UTC
_LONDON_START  = 7
_LONDON_END    = 12   # 07:00–11:59 UTC
_OVERLAP_START = 12
_OVERLAP_END   = 16   # 12:00–15:59 UTC
_NEWYORK_START = 16
_NEWYORK_END   = 21   # 16:00–20:59 UTC


def _utc_session(utc_hour: int) -> str:
    if _ASIAN_START <= utc_hour < _ASIAN_END:
        return "asian"
    if _LONDON_START <= utc_hour < _LONDON_END:
        return "london"
    if _OVERLAP_START <= utc_hour < _OVERLAP_END:
        return "overlap"
    if _NEWYORK_START <= utc_hour < _NEWYORK_END:
        return "newyork"
    return "off_hours"


@dataclass
class PairConditions:
    """Output of evaluate_symbol_quality()."""
    allowed:                  bool
    symbol:                   str
    session:                  str           # "london" | "overlap" | "asian" | ...
    atr_pips:                 float
    atr_quality:              str           # "dead" | "low" | "ideal" | "elevated" | "extreme"
    atr_size_mult:            float         # size factor for ATR zone (1.0 = ideal)
    spread_pips:              float
    spread_quality:           str           # "acceptable" | "elevated" | "blocked"
    market_state:             str           # regime string from MarketStateResult
    exec_mult_override:       Optional[float]  # None = use market_state.exec_mult
    lot_mult_cap:             float         # applied on top of all other size factors
    scoring_mods:             Dict[str, float]  # factor → multiplier for confluence
    block_reason:             str           # "" when allowed; human-readable when blocked
    confidence_override:      Optional[int]    # None = use normal threshold
    regime_confidence_penalty: int = 0     # additive threshold penalty for degraded regime


def evaluate_symbol_quality(
    symbol:        str,
    atr_pips:      float,
    spread_pips:   float,
    utc_hour:      int,
    market_state:  str,
    confidence:    int   = 0,
) -> PairConditions:
    """
    Evaluate per-scan trading conditions for a symbol using its registry profile.

    Returns a PairConditions result.  Main.py must check result.allowed before
    proceeding; if False, return None from the scan immediately.

    Args:
        symbol:       Broker symbol string (e.g. "GBPJPY.Z").
        atr_pips:     Current M15 ATR in pips.
        spread_pips:  Current spread in pips (0.0 if unavailable).
        utc_hour:     Current UTC hour (0-23).
        market_state: Market regime from MarketStateResult.state.
        confidence:   Confidence score (used for diagnostic log only).

    Returns:
        PairConditions dataclass.
    """
    from aurex_ai.core.symbol_mapper import strip_suffix
    base = strip_suffix(symbol).upper()
    profile = get_profile(symbol)

    session     = _utc_session(utc_hour)
    scoring_mods = get_scoring_modifiers(symbol)

    # ── Symbols without a full profile get a pass-through result ─────────────
    # All gates (ATR, spread, session, regime) fall back to global settings.
    if not profile or not profile.get("regime_mult"):
        return PairConditions(
            allowed             = True,
            symbol              = symbol,
            session             = session,
            atr_pips            = atr_pips,
            atr_quality         = "unknown",
            atr_size_mult       = 1.0,
            spread_pips         = spread_pips,
            spread_quality      = "acceptable",
            market_state        = market_state,
            exec_mult_override  = None,
            lot_mult_cap        = get_lot_mult_cap(symbol),
            scoring_mods        = scoring_mods,
            block_reason        = "",
            confidence_override = None,
        )

    # ── Session gate ──────────────────────────────────────────────────────────
    block_sessions    = profile.get("block_sessions",    [])
    priority_sessions = profile.get("priority_sessions", [])

    if session in block_sessions:
        reason = (
            f"session_block | session={session} is in block_sessions={block_sessions}"
        )
        log.warning(
            "[PAIR BLOCK] [PAIR INTELLIGENCE] %s — %s session hard-blocked | "
            "atr=%.1f spread=%.1f regime=%s",
            symbol, session.upper(), atr_pips, spread_pips, market_state,
        )
        return _blocked(symbol, session, atr_pips, spread_pips, market_state,
                        scoring_mods, reason)

    # ── ATR gates ─────────────────────────────────────────────────────────────
    min_atr, ideal_min, ideal_max, max_atr = get_atr_gates(symbol)

    if atr_pips < min_atr:
        reason = (
            f"atr_too_low | atr={atr_pips:.1f} < min={min_atr:.0f} pips "
            f"(dead/Asian market)"
        )
        log.warning(
            "[PAIR BLOCK] [PAIR INTELLIGENCE] %s — ATR dead: %.1f < min=%.0f pips | "
            "session=%s regime=%s",
            symbol, atr_pips, min_atr, session, market_state,
        )
        return _blocked(symbol, session, atr_pips, spread_pips, market_state,
                        scoring_mods, reason)

    if atr_pips > max_atr:
        reason = (
            f"atr_extreme | atr={atr_pips:.1f} > max={max_atr:.0f} pips "
            f"(flash crash / news spike — too dangerous)"
        )
        log.warning(
            "[PAIR BLOCK] [PAIR INTELLIGENCE] %s — ATR extreme: %.1f > max=%.0f pips | "
            "session=%s regime=%s",
            symbol, atr_pips, max_atr, session, market_state,
        )
        return _blocked(symbol, session, atr_pips, spread_pips, market_state,
                        scoring_mods, reason)

    # ATR quality tier → size multiplier
    if atr_pips < ideal_min:
        atr_quality    = "low"
        atr_size_mult  = 0.75    # below ideal floor — momentum not fully engaged
    elif atr_pips <= ideal_max:
        atr_quality    = "ideal"
        atr_size_mult  = 1.00    # sweet spot
    else:
        atr_quality    = "elevated"
        atr_size_mult  = 0.80    # above ideal ceiling — spike / wider SL risk

    # ── Spread gate ───────────────────────────────────────────────────────────
    spread_warn  = float(profile.get("spread_warn_pips",  2.0))
    spread_block = float(profile.get("spread_block_pips", 0.0))
    spread_quality = "acceptable"

    if spread_pips > 0:
        if spread_block > 0 and spread_pips >= spread_block:
            reason = (
                f"spread_blocked | spread={spread_pips:.1f} >= block={spread_block:.0f} pips "
                f"(rollover/news spike)"
            )
            log.warning(
                "[PAIR BLOCK] [PAIR INTELLIGENCE] %s — spread too wide: %.1f >= %.0f pips | "
                "session=%s atr=%.1f",
                symbol, spread_pips, spread_block, session, atr_pips,
            )
            return _blocked(symbol, session, atr_pips, spread_pips, market_state,
                            scoring_mods, reason)

        if spread_pips >= spread_warn:
            spread_quality = "elevated"
            log.warning(
                "[PAIR INTELLIGENCE] %s — spread elevated: %.1f pips (warn=%.0f block=%.0f) | "
                "session=%s",
                symbol, spread_pips, spread_warn, spread_block, session,
            )

    # ── Regime gate ───────────────────────────────────────────────────────────
    # Hard blocks: DEAD (0.0), VOLATILITY_COMPRESSION (0.0).
    # RANGING (0.80 for GBPJPY) is a soft penalty — Asian session is already
    # blocked by the session gate above, so 0.80 only applies during active sessions.
    regime_override = get_regime_mult(symbol, market_state)
    regime_conf_penalty = 0

    if regime_override == 0.0:
        reason = (
            f"regime_block | state={market_state} exec_mult=0.0 "
            f"(hard-blocked for {base} — DEAD or VOLATILITY_COMPRESSION)"
        )
        log.warning(
            "[PAIR BLOCK] [PAIR INTELLIGENCE] %s — regime hard-block: state=%s | "
            "session=%s atr=%.1f spread=%.1f",
            symbol, market_state, session, atr_pips, spread_pips,
        )
        return _blocked(symbol, session, atr_pips, spread_pips, market_state,
                        scoring_mods, reason)

    if market_state == "RANGING" and regime_override is not None and regime_override < 1.0:
        regime_conf_penalty = 2
        log.warning(
            "[PAIR REGIME SOFT] %s — RANGING during %s | exec_mult=%.2f "
            "conf_penalty=+%d (no hard block; size reduced)",
            symbol, session.upper(), regime_override, regime_conf_penalty,
        )

    # ── All gates passed — build ALLOW result ─────────────────────────────────
    lot_cap = get_lot_mult_cap(symbol)

    _trend_label = "MODERATE" if confidence < 70 else "STRONG" if confidence >= 80 else "GOOD"
    _atr_label   = atr_quality.upper()
    _spread_label = spread_quality.upper()
    _sess_label  = session.upper()
    _decision    = "ALLOW"

    # Emit GBPJPY-specific quality diagnostic
    if base == "GBPJPY":
        log.warning(
            "[GBPJPY QUALITY] trend=%s atr=%.0f(%s) spread=%s session=%s "
            "regime=%s exec_mult=%.2f lot_cap=%.2f decision=%s",
            _trend_label,
            atr_pips, _atr_label,
            _spread_label,
            _sess_label,
            market_state,
            regime_override if regime_override is not None else 1.0,
            lot_cap,
            _decision,
        )
    else:
        log.info(
            "[PAIR ALLOW] [PAIR INTELLIGENCE] %s | session=%s atr=%.1f(%s) "
            "spread=%.1f(%s) regime=%s exec_mult=%s lot_cap=%.2f",
            symbol, session, atr_pips, _atr_label,
            spread_pips, _spread_label,
            market_state,
            f"{regime_override:.2f}" if regime_override is not None else "default",
            lot_cap,
        )

    return PairConditions(
        allowed             = True,
        symbol              = symbol,
        session             = session,
        atr_pips            = atr_pips,
        atr_quality         = atr_quality,
        atr_size_mult       = atr_size_mult,
        spread_pips         = spread_pips,
        spread_quality      = spread_quality,
        market_state        = market_state,
        exec_mult_override  = regime_override,
        lot_mult_cap        = lot_cap,
        scoring_mods        = scoring_mods,
        block_reason               = "",
        confidence_override        = None,
        regime_confidence_penalty  = regime_conf_penalty,
    )


# ── Internal helper ───────────────────────────────────────────────────────────

def _blocked(
    symbol:       str,
    session:      str,
    atr_pips:     float,
    spread_pips:  float,
    market_state: str,
    scoring_mods: dict,
    reason:       str,
) -> PairConditions:
    """Return a PairConditions with allowed=False."""
    return PairConditions(
        allowed             = False,
        symbol              = symbol,
        session             = session,
        atr_pips            = atr_pips,
        atr_quality         = "blocked",
        atr_size_mult       = 0.0,
        spread_pips         = spread_pips,
        spread_quality      = "blocked",
        market_state        = market_state,
        exec_mult_override  = 0.0,
        lot_mult_cap        = 0.0,
        scoring_mods        = scoring_mods,
        block_reason        = reason,
        confidence_override = None,
    )
