"""
Aurex AI — Centralized Symbol Registry  (Phase 6 + Phase 10)

Single source of truth for all supported trading instruments.

Rules enforced here:
  • XAUUSD  — gold, never receives a broker suffix (e.g. never XAUUSD.Z)
  • Forex   — always receive the configured broker suffix (e.g. EURUSD.Z)

Per-symbol profiles carry volatility class, ATR behaviour, preferred sessions,
RR guidance, and spread tolerance so the rest of the codebase can query one
place instead of scattering if-XAU logic everywhere.

Extended fields (Phase 10 — institutional pair intelligence):
  • min_atr_pips      — hard minimum ATR floor (overrides global setting)
  • ideal_atr_min     — lower bound of ideal trading ATR range
  • ideal_atr_max     — upper bound of ideal trading ATR range (above = spike/news risk)
  • max_atr_pips      — hard ATR ceiling (block trade; too volatile to trade safely)
  • lot_mult_cap      — max lot multiplier for this symbol (1.0 = no extra cap)
  • execution_style   — "momentum_trend" | "trend_continuation" | "range_revert"
  • scoring_modifiers — dict: factor → multiplier adjustment (1.0 = no change)
  • regime_mult       — dict: market_state → exec_mult override (None = use default)
  • priority_sessions — sessions where this pair has historically highest WR
  • block_sessions    — sessions to hard-block (Asian for crosses, etc.)

Public API
----------
    get_broker_symbol(base, broker_suffix)  → str   e.g. "XAUUSD" or "EURUSD.Z"
    get_profile(symbol)                     → dict  full profile or {}
    get_all_broker_symbols(broker_suffix)   → list  of all canonical broker symbols
    is_no_suffix(base)                      → bool  True for gold / bare symbols
    validate_symbol_name(symbol)            → bool  True if in registry
    log_registry_summary(broker_suffix)     → None  startup banner
    get_atr_gates(symbol)                   → tuple (min, ideal_min, ideal_max, max)
    get_lot_mult_cap(symbol)                → float
    get_regime_mult(symbol, state)          → float | None

Tags emitted
------------
    [SYMBOL INIT]  [SYMBOL VALIDATION]  [MARKET WATCH SYNC]  [SYMBOL EXECUTION READY]
    [SYMBOL PROFILE]  [PAIR INTELLIGENCE]  [SYMBOL VERIFIED]
    [GBPJPY PROFILE]  [GBPJPY QUALITY]
"""
from __future__ import annotations

from typing import Dict, List, Optional

from aurex_ai.core.logger import get_logger

log = get_logger("core.symbol_registry")

# ── Master symbol configuration ───────────────────────────────────────────────
# Keys are BASE symbol names (no broker suffix).
# broker_symbol is resolved at runtime via get_broker_symbol().
#
# volatility:   "high" | "medium" | "low"
# atr_profile:  "expanded" (gold/GBP) | "normal" | "tight" (CHF/NZD/CAD)
# no_suffix:    True  → never add broker suffix (gold on all brokers)
#               False → add broker suffix (forex pairs)
# rr_standard:  default R:R for standard setups on this symbol
# rr_high_conf: max R:R for high-confidence setups
# spread_warn_pips: spread above which to log a warning
# spread_block_pips: spread above which to block (0 = use global spread/ATR ratio)
# sessions:     sessions with highest historical win rate for this symbol
# pip_size:     monetary pip size (not contract size)
# notes:        human-readable rationale for profile settings

SYMBOL_CONFIG: Dict[str, dict] = {
    # ── Phase 11: XAUUSD Institutional Intelligence Profile ──────────────────
    # Gold is an entirely different animal from FX crosses.  It is driven by:
    #   • USD macro flow (DXY inverse correlation)
    #   • Real-yield differentials (rates/inflation)
    #   • Safe-haven positioning (risk-on/off)
    #   • Central bank demand (structural support)
    #   • CPI / NFP / FOMC event risk (hard blocks required)
    #
    # Key edges that work on XAUUSD:
    #   • Trend continuation in macro-trending regimes (Fed pivot, USD weakness)
    #   • Order block sweeps — gold respects OB zones because institutions
    #     protect their structural fill levels very cleanly
    #   • FVG fills in London/NY — gold carries deep imbalances that fill fast
    #   • Liquidity sweeps of swing highs/lows before reversal continuation
    #   • Displacement candles + FVG = highest-confidence gold entries
    #
    # Key risks that destroy alpha:
    #   • Pure breakouts above swing highs — stop raids are extremely common
    #   • Asian session — thin gold market; wide spread; no institutional flow
    #   • FOMC/CPI/NFP spike sessions — ATR spikes to 50-80+ pips; untradeable
    #   • RANGING regime — gold can fake-range before violent continuation
    #
    # pip_size = 0.1 means: $2.00 price move = 20 pips.
    # M15 ATR typical ranges:
    #   Dead/overnight  : $0.50–1.20 = 5-12 pips
    #   Normal London   : $1.20–2.50 = 12-25 pips (ideal floor)
    #   Active NY       : $2.50–5.00 = 25-50 pips (sweet spot)
    #   News spike      : $5.00–8.00 = 50-80 pips (elevated; reduce size)
    #   FOMC/CPI spike  : $8.00+    = 80+ pips (hard block)
    "XAUUSD": {
        # ── Core registry fields ──────────────────────────────────────────────
        "no_suffix":          True,          # gold NEVER uses broker suffix
        "type":               "commodity",   # not forex — macro/safe-haven instrument
        "volatility":         "very_high",   # highest dollar-pip value of any instrument
        "atr_profile":        "expanded",    # M15 ATR 5-80 pips (pip_size=0.1)
        "rr_standard":        1.5,           # 1:1.5 minimum for gold setups
        "rr_high_conf":       2.5,           # displacement+OB setups: 1:2.5 target
        "spread_warn_pips":   3.0,           # gold spread > 3 pips is elevated
        "spread_block_pips":  8.0,           # gold spread > 8 pips = rollover/news spike
        "sessions":           ["london", "newyork"],
        "pip_size":           0.1,           # 1 pip = $0.10; ATR ~5-50 pips on M15
        "notes": (
            "Macro/safe-haven commodity driven by USD, real yields, risk sentiment. "
            "OB + Trend is the primary edge. FVG fills reliable in London/NY. "
            "Hard block on Asian session and FOMC/CPI spike ATR levels. "
            "Pure breakouts are stop-raid traps — OB backing required. "
            "pip_size=0.1: $2 move = 20 pips. ATR sweet spot: 12-50 pips."
        ),

        # ── ATR gates (Phase 11 extended fields) ─────────────────────────────
        # pip_size=0.1, so:
        #   5 pips  = $0.50  (dead/rollover)
        #   12 pips = $1.20  (London open minimum momentum)
        #   25 pips = $2.50  (active session sweet spot)
        #   50 pips = $5.00  (elevated — spike risk; reduce size)
        #   80 pips = $8.00  (FOMC/CPI territory; hard block)
        "min_atr_pips":       5.0,    # below = dead overnight / Asian rollover
        "ideal_atr_min":      12.0,   # healthy London momentum starts here
        "ideal_atr_max":      50.0,   # above = news/spike territory; reduce size
        "max_atr_pips":       80.0,   # above = FOMC/CPI spike; hard block

        # ── Lot multiplier cap ────────────────────────────────────────────────
        # Gold: $1 move per pip × pip_size=0.1 × lot_size.
        # A 0.01-lot gold position moves $1 per $0.10 price change.
        # Dollar risk per pip is proportionally larger than most FX pairs.
        # Cap at 0.80 to limit notional exposure on wide ATR moves.
        "lot_mult_cap":       0.80,

        # ── Execution style ───────────────────────────────────────────────────
        "execution_style":    "momentum_trend",   # OB+Trend primary; no counter-trend

        # ── Scoring modifiers ─────────────────────────────────────────────────
        # Applied multiplicatively to factor weights in the confluence engine.
        # Derived from gold market microstructure:
        #   trend:         MACRO THESIS — #1 predictor; gold trends align with macro regime
        #   order_block:   INSTITUTIONAL FILL PROTECTION — OB zones are gold's entry trigger
        #   fvg:           IMBALANCE FILLS — gold fills gaps cleanly in London/NY sessions
        #   liquidity:     SWEEP ACCELERATION — liquidity sweeps precede gold's strongest moves
        #   fibonacci:     TIMING — useful for pullback timing but secondary to OB
        #   ema:           REDUCED — gold price action is more impulsive; EMA can lag
        #   confirmation:  REDUCED — gold can run before candle confirmation
        #   breakout:      STRONG DEMOTION — pure breakouts = stop raids; avoid without OB
        "scoring_modifiers": {
            "trend":        1.25,   # +25%: macro alignment is the strongest gold predictor
            "order_block":  1.20,   # +20%: institutional OB zones are the primary trigger
            "fvg":          1.15,   # +15%: FVG fills reliable in London/NY
            "liquidity":    1.10,   # +10%: sweep → acceleration; high-confidence signal
            "fibonacci":    1.05,   # +5%:  useful for timing on impulse pullbacks
            "ema":          0.90,   # -10%: impulsive gold moves exceed EMA zones
            "confirmation": 0.95,   # -5%:  candle confirmation can lag on fast gold moves
            "breakout":     0.55,   # -45%: stop-raid traps above swing highs; needs OB
        },

        # ── Market regime execution multiplier overrides ──────────────────────
        # Gold regime characteristics:
        #   TRENDING:              1.00  (prime state; macro thesis driving direction)
        #   VOLATILE_EXPANSION:    0.40  (CPI/NFP/FOMC spike — most dangerous gold state)
        #   RANGING:               0.50  (unlike GBPJPY, gold ranging is tradeable with OB)
        #   VOLATILITY_COMPRESSION:0.30  (coiling before unknown breakout; min exposure)
        #   REVERSAL_ENVIRONMENT:  0.60  (reversals valid with strong OB; controlled risk)
        #   DEAD:                  0.00  (standard)
        "regime_mult": {
            "TRENDING":               1.00,
            "VOLATILE_EXPANSION":     0.40,   # hard reduction — spike regimes kill gold P&L
            "RANGING":                0.50,   # not a hard block; gold range trading viable
            "VOLATILITY_COMPRESSION": 0.30,   # minimum exposure — direction unknown
            "REVERSAL_ENVIRONMENT":   0.60,
            "DEAD":                   0.00,
        },

        # ── Session restrictions ──────────────────────────────────────────────
        "priority_sessions":  ["london", "newyork"],  # institutional gold sessions
        "block_sessions":     ["asian"],              # thin liquidity; wide spread; no flow
    },
    "EURUSD": {
        "no_suffix":          False,
        "type":               "forex",
        "volatility":         "medium",
        "atr_profile":        "normal",
        "rr_standard":        1.5,
        "rr_high_conf":       2.0,
        "spread_warn_pips":   1.5,
        "spread_block_pips":  0.0,           # rely on global spread/ATR ratio
        "sessions":           ["london", "newyork"],
        "pip_size":           0.0001,
        "notes":              "Deepest liquidity. Tighter TP precision preferred.",
    },
    "GBPUSD": {
        "no_suffix":          False,
        "type":               "forex",
        "volatility":         "high",
        "atr_profile":        "expanded",
        "rr_standard":        1.5,
        "rr_high_conf":       2.0,
        "spread_warn_pips":   2.0,
        "spread_block_pips":  0.0,
        "sessions":           ["london"],
        "pip_size":           0.0001,
        "notes":              "Strong institutional flow. Continuation bias in London.",
    },
    "USDJPY": {
        "no_suffix":          False,
        "type":               "forex",
        "volatility":         "medium",
        "atr_profile":        "normal",
        "rr_standard":        1.5,
        "rr_high_conf":       2.0,
        "spread_warn_pips":   1.5,
        "spread_block_pips":  0.0,
        "sessions":           ["london", "newyork", "asian"],
        "pip_size":           0.01,
        "notes":              "Risk-on/risk-off gauge. Volatility-aware pullback.",
    },
    "AUDUSD": {
        "no_suffix":          False,
        "type":               "forex",
        "volatility":         "medium",
        "atr_profile":        "normal",
        "rr_standard":        1.5,
        "rr_high_conf":       1.75,
        "spread_warn_pips":   1.5,
        "spread_block_pips":  0.0,
        "sessions":           ["london", "asian"],
        "pip_size":           0.0001,
        "notes":              "Commodity-correlated. Active in Asian crossover.",
    },
    "USDCAD": {
        "no_suffix":          False,
        "type":               "forex",
        "volatility":         "medium",
        "atr_profile":        "tight",
        "rr_standard":        1.5,
        "rr_high_conf":       1.75,
        "spread_warn_pips":   1.5,
        "spread_block_pips":  0.0,
        "sessions":           ["newyork"],
        "pip_size":           0.0001,
        "notes":              "Oil-correlated. Active during NY open.",
    },
    "NZDUSD": {
        "no_suffix":          False,
        "type":               "forex",
        "volatility":         "low",
        "atr_profile":        "tight",
        "rr_standard":        1.5,
        "rr_high_conf":       1.75,
        "spread_warn_pips":   2.0,
        "spread_block_pips":  0.0,
        "sessions":           ["london", "asian"],
        "pip_size":           0.0001,
        "notes":              "Lower ATR. Tighter TP. More conservative execution.",
    },
    "USDCHF": {
        "no_suffix":          False,
        "type":               "forex",
        "volatility":         "medium",
        "atr_profile":        "tight",
        "rr_standard":        1.5,
        "rr_high_conf":       1.75,
        "spread_warn_pips":   1.5,
        "spread_block_pips":  0.0,
        "sessions":           ["london"],
        "pip_size":           0.0001,
        "notes":              "Safe-haven flow. Active in London. CHF spikes on risk events.",
    },

    # ── Phase 10: GBPJPY Institutional Intelligence Profile ───────────────────
    # GBPJPY is the highest-ATR actively-traded FX cross.  It combines GBP
    # institutional momentum (London-dominated) with JPY risk-on/off flows.
    # Result: explosive trends when both currencies agree; dangerous whipsaw
    # when they diverge.  The edge is EXCLUSIVELY momentum continuation in
    # clearly-trending markets during London/Overlap sessions.
    #
    # DO NOT treat this pair like EURUSD.  ATR thresholds, spread tolerances,
    # session restrictions, and lot caps all require bespoke calibration.
    #
    # Live analytics: GBPJPY.Z = +40.0 score (strong positive expected value).
    # This profile unlocks that edge while protecting capital in adverse conditions.
    "GBPJPY": {
        # ── Core registry fields ──────────────────────────────────────────────
        "no_suffix":          False,
        "type":               "forex_cross",   # cross pair — no direct USD leg
        "volatility":         "very_high",     # highest ATR of any major cross
        "atr_profile":        "expanded",      # M15 ATR typically 15-60 pips
        "rr_standard":        2.0,             # wider TP needed to accommodate ATR
        "rr_high_conf":       3.0,             # exceptional trending setups: 3R target
        "spread_warn_pips":   4.0,             # GBP+JPY spread stacks; warn at 4 pips
        "spread_block_pips":  9.0,             # rollover/news spike ceiling: hard block
        "sessions":           ["london", "overlap"],  # priority sessions
        "pip_size":           0.01,            # JPY pairs: 0.01 pip (not 0.0001)
        "notes": (
            "Highest-ATR major FX cross. MOMENTUM_TREND execution only. "
            "London/Overlap sessions exclusively. Block Asian session. "
            "Block RANGING/COMPRESSION regimes — ATR collapse = whipsaw trap. "
            "Wide SL required; use full ATR-adaptive sizing (no hard pips cap). "
            "OB+Trend is the primary edge; pure breakout is a trap on this pair."
        ),

        # ── ATR gates (Phase 10 extended fields) ─────────────────────────────
        # min_atr_pips: below this = Asian dead session / institutional liquidity absent
        # ideal_atr_min/max: sweet spot — enough momentum, not spike/news territory
        # max_atr_pips: above this = news/spike; execution blocked
        "min_atr_pips":       12.0,    # must have at least 12 pips ATR to trade
        "ideal_atr_min":      18.0,    # healthy momentum starts here
        "ideal_atr_max":      55.0,    # above 55 = risky spike territory; reduce size
        "max_atr_pips":       100.0,   # above 100 = flash crash / extreme news; hard block

        # ── Lot multiplier cap ────────────────────────────────────────────────
        # GBPJPY extreme ATR means dollar risk is very high per pip.
        # Cap the lot multiplier at 0.85 to avoid runaway exposure on volatile entries.
        "lot_mult_cap":       0.85,

        # ── Execution style ───────────────────────────────────────────────────
        "execution_style":    "momentum_trend",   # OB+Trend, no counter-trend

        # ── Scoring modifiers ─────────────────────────────────────────────────
        # Applied multiplicatively to factor weights in the confluence engine.
        # Values > 1.0 increase the effective weight; < 1.0 reduce it.
        # These reflect what actually works on GBPJPY based on market microstructure:
        #   - Trend alignment is MANDATORY (directional conviction is the edge)
        #   - Order block confirms institutional order flow (strongest on crosses)
        #   - FVG valid in London/Overlap where filling is fast
        #   - Breakout: weak without OB or Fib — demoted
        #   - Fibonacci: important for entry timing on continuation pullbacks
        "scoring_modifiers": {
            "trend":         1.20,   # +20%: directional commitment is the primary edge
            "order_block":   1.15,   # +15%: institutional flow confirmation essential
            "fvg":           1.10,   # +10%: valid in London/Overlap sessions
            "fibonacci":     1.10,   # +10%: pullback timing on momentum legs
            "liquidity":     1.05,   # +5%:  sweep of swing highs/lows = acceleration signal
            "ema":           1.00,   # neutral: EMA proximity scoring unchanged
            "confirmation":  1.00,   # neutral: candle confirmation unchanged
            "breakout":      0.60,   # -40%: pure breakouts without OB/Fib often fail
        },

        # ── Market regime execution multiplier overrides ──────────────────────
        # For most pairs the global market_state.exec_mult applies.
        # GBPJPY needs stricter overrides because its volatility makes regime
        # mismatches far more costly than on EURUSD-class pairs.
        #   TRENDING:              1.00  (home territory — full size)
        #   VOLATILE_EXPANSION:    0.65  (extra caution vs global 0.70; gap risk)
        #   RANGING:               0.00  (hard block; GBPJPY chop is savage)
        #   VOLATILITY_COMPRESSION: 0.00 (coiling → direction unknown; avoid)
        #   REVERSAL_ENVIRONMENT:  0.50  (risky but sometimes valid with full OB)
        #   DEAD:                  0.00  (standard)
        "regime_mult": {
            "TRENDING":               1.00,
            "VOLATILE_EXPANSION":     0.65,
            "RANGING":                0.00,   # hard block — identical to EURUSD treatment
            "VOLATILITY_COMPRESSION": 0.00,   # block: breakout direction unknown
            "REVERSAL_ENVIRONMENT":   0.50,
            "DEAD":                   0.00,
        },

        # ── Session restrictions ──────────────────────────────────────────────
        "priority_sessions":  ["overlap", "london"],   # highest-WR execution windows
        "block_sessions":     ["asian"],               # Asian session = thin JPY + wide spread
    },
}

# Fast lookup sets derived once at module load
_NO_SUFFIX_BASES: frozenset = frozenset(
    k for k, v in SYMBOL_CONFIG.items() if v.get("no_suffix")
)
_ALL_BASES: frozenset = frozenset(SYMBOL_CONFIG.keys())


# ── Public API ────────────────────────────────────────────────────────────────

def is_no_suffix(base: str) -> bool:
    """Return True if this symbol must never have a broker suffix added."""
    return base.strip().upper() in _NO_SUFFIX_BASES


def get_broker_symbol(base: str, broker_suffix: str) -> str:
    """
    Return the canonical broker symbol for a base symbol.

    Respects no_suffix rules: gold symbols always return as-is regardless
    of the configured broker_suffix.

    Examples (broker_suffix=".Z"):
        get_broker_symbol("XAUUSD", ".Z") → "XAUUSD"   (gold — no suffix)
        get_broker_symbol("EURUSD", ".Z") → "EURUSD.Z"  (forex — suffix added)
        get_broker_symbol("GBPUSD", "")   → "GBPUSD"    (no suffix configured)
    """
    base_upper = base.strip().upper()
    if is_no_suffix(base_upper):
        return base_upper
    suffix = (broker_suffix or "").strip()
    if not suffix or base_upper.endswith(suffix.upper()):
        return base_upper
    return base_upper + suffix


def get_profile(symbol: str) -> dict:
    """
    Return the full profile dict for a symbol.

    Accepts both base (EURUSD) and broker-suffixed (EURUSD.Z) forms.
    Returns {} for unknown symbols.
    """
    from aurex_ai.core.symbol_mapper import strip_suffix
    base = strip_suffix(symbol).upper()
    return SYMBOL_CONFIG.get(base, {})


def get_all_broker_symbols(broker_suffix: str) -> List[str]:
    """
    Return the list of all canonical broker symbols in registry order.

    Example (broker_suffix=".Z"):
        ["XAUUSD", "EURUSD.Z", "GBPUSD.Z", "USDJPY.Z", ...]
    """
    return [get_broker_symbol(base, broker_suffix) for base in SYMBOL_CONFIG]


def validate_symbol_name(symbol: str) -> bool:
    """Return True if symbol (base or suffixed) is in the registry."""
    from aurex_ai.core.symbol_mapper import strip_suffix
    base = strip_suffix(symbol).upper()
    return base in _ALL_BASES


def get_symbol_rr_profile(symbol: str) -> tuple[float, float]:
    """
    Return (rr_standard, rr_high_conf) for a symbol.
    Falls back to (1.5, 2.0) for unknown symbols.
    """
    profile = get_profile(symbol)
    return (
        float(profile.get("rr_standard",  1.5)),
        float(profile.get("rr_high_conf", 2.0)),
    )


def get_spread_thresholds(symbol: str) -> tuple[float, float]:
    """
    Return (spread_warn_pips, spread_block_pips) for a symbol.
    spread_block_pips == 0.0 means use the global ATR-ratio gate instead.
    """
    profile = get_profile(symbol)
    return (
        float(profile.get("spread_warn_pips",  2.0)),
        float(profile.get("spread_block_pips", 0.0)),
    )


def get_atr_gates(symbol: str) -> tuple[float, float, float, float]:
    """
    Return (min_atr_pips, ideal_atr_min, ideal_atr_max, max_atr_pips) for a symbol.

    Phase 10 extended field — only populated for symbols with bespoke ATR profiles
    (currently GBPJPY).  Falls back to sensible global defaults for all others.

    Returns:
        min_atr_pips   — hard floor: below this the market is dead / sleeping
        ideal_atr_min  — lower bound of the ideal trading ATR range
        ideal_atr_max  — upper bound: above this apply size reduction
        max_atr_pips   — hard ceiling: above this block execution entirely
    """
    profile = get_profile(symbol)
    return (
        float(profile.get("min_atr_pips",   6.0)),
        float(profile.get("ideal_atr_min",  8.0)),
        float(profile.get("ideal_atr_max", 40.0)),
        float(profile.get("max_atr_pips", 200.0)),
    )


def get_lot_mult_cap(symbol: str) -> float:
    """
    Return the per-symbol lot-multiplier cap.

    Phase 10: GBPJPY uses 0.85 to limit exposure on its extreme ATR.
    All other symbols default to 1.0 (no extra cap beyond the global 1.5× guard).
    """
    profile = get_profile(symbol)
    return float(profile.get("lot_mult_cap", 1.0))


def get_regime_mult(symbol: str, market_state: str) -> Optional[float]:
    """
    Return a symbol-specific execution multiplier for the given market regime.

    Phase 10: GBPJPY has bespoke regime gates (e.g. RANGING=0.0 hard block,
    VOLATILITY_COMPRESSION=0.0).  Returns None for symbols without overrides
    (caller should fall back to the global market_state.exec_mult).

    Args:
        symbol:       Instrument name (base or broker-suffixed).
        market_state: State string from MarketStateResult.state.

    Returns:
        float override if the symbol has a bespoke gate for this state.
        None if no override — caller uses market_state.exec_mult directly.
    """
    profile = get_profile(symbol)
    regime_map: dict = profile.get("regime_mult", {})
    val = regime_map.get(market_state)
    if val is None:
        return None
    return float(val)


def get_scoring_modifiers(symbol: str) -> dict:
    """
    Return the per-symbol factor scoring modifiers dict.

    Phase 10: GBPJPY has bespoke modifiers (trend +20%, breakout -40%, etc.).
    Returns {} for symbols without overrides (no modification applied).
    """
    profile = get_profile(symbol)
    return dict(profile.get("scoring_modifiers", {}))


def log_registry_summary(broker_suffix: str) -> None:
    """
    Emit a startup banner listing all registered symbols and their broker names.
    Call once after suffix detection, before trading begins.
    """
    lines = [
        "\n"
        "  ┌──────────────────────────────────────────────────────────────┐\n"
        "  │              SYMBOL REGISTRY (Phase 6)                       │\n"
        "  ├──────────────────────────────────────────────────────────────┤"
    ]
    for base, profile in SYMBOL_CONFIG.items():
        broker = get_broker_symbol(base, broker_suffix)
        tag    = "[GOLD/NO-SUFFIX]" if profile.get("no_suffix") else f"[{profile.get('volatility','?').upper()} VOL]"
        lines.append(
            f"  │  {base:<10} → {broker:<14} {profile.get('atr_profile',''):>8}  {tag:<18}│"
        )
    lines.append(
        "  └──────────────────────────────────────────────────────────────┘"
    )
    log.warning(
        "[SYMBOL INIT] Registry loaded | %d instruments | suffix='%s'\n%s",
        len(SYMBOL_CONFIG),
        broker_suffix,
        "\n".join(lines),
    )


def log_symbol_profile(symbol: str) -> None:
    """
    Log the profile for a single symbol at scan startup.

    Phase 10: GBPJPY emits an extended [GBPJPY PROFILE] institutional banner
    instead of the generic one-liner, reflecting its bespoke intelligence profile.
    """
    profile = get_profile(symbol)
    if not profile:
        log.warning(
            "[SYMBOL PROFILE] %s — NOT IN REGISTRY [PAIR INTELLIGENCE UNAVAILABLE]",
            symbol,
        )
        return

    from aurex_ai.core.symbol_mapper import strip_suffix
    base = strip_suffix(symbol).upper()

    if base == "XAUUSD":
        # ── XAUUSD extended institutional banner ──────────────────────────────
        _mod    = profile.get("scoring_modifiers", {})
        _regime = profile.get("regime_mult", {})
        log.warning(
            "\n"
            "  ┌──────────────────────────────────────────────────────────────┐\n"
            "  │                  XAUUSD PROFILE                              │\n"
            "  │             INSTITUTIONAL COMMODITY INTELLIGENCE              │\n"
            "  ├──────────────────────────────────────────────────────────────┤\n"
            "  │  Instrument     : %-42s│\n"
            "  │  ATR Profile    : %-42s│\n"
            "  │  ATR Gates      : %-42s│\n"
            "  │  Spread         : %-42s│\n"
            "  │  Sessions       : %-42s│\n"
            "  │  Block Sessions : %-42s│\n"
            "  │  RR Profile     : %-42s│\n"
            "  │  Lot Mult Cap   : %-42s│\n"
            "  │  Execution      : %-42s│\n"
            "  │  Pip Size       : %-42s│\n"
            "  ├──────────────────────────────────────────────────────────────┤\n"
            "  │  Scoring Modifiers (vs baseline):                            │\n"
            "  │    trend=%.2f× ob=%.2f× fvg=%.2f× liquidity=%.2f× breakout=%.2f×  │\n"
            "  ├──────────────────────────────────────────────────────────────┤\n"
            "  │  Regime Gates:                                               │\n"
            "  │    TRENDING=%.2f  RANGING=%.2f  VOLATILE_EXP=%.2f            │\n"
            "  │    COMPRESSION=%.2f  REVERSAL=%.2f                           │\n"
            "  └──────────────────────────────────────────────────────────────┘",
            "Gold / XAUUSD (safe-haven commodity)",
            "EXPANDED (5-80 pips M15; pip_size=0.1)",
            f"min={profile.get('min_atr_pips',5):.0f} ideal={profile.get('ideal_atr_min',12):.0f}-{profile.get('ideal_atr_max',50):.0f} max={profile.get('max_atr_pips',80):.0f} pips",
            f"warn≥{profile.get('spread_warn_pips',3):.0f} block≥{profile.get('spread_block_pips',8):.0f} pips",
            " / ".join(profile.get("priority_sessions", [])).upper(),
            " / ".join(profile.get("block_sessions", [])).upper(),
            f"standard={profile.get('rr_standard',1.5):.1f}R  high-conf={profile.get('rr_high_conf',2.5):.1f}R",
            f"{profile.get('lot_mult_cap',0.80):.2f}× (dollar-pip exposure cap)",
            str(profile.get("execution_style","momentum_trend")).upper(),
            "0.1 (gold — 1 pip = $0.10; $2 move = 20 pips)",
            _mod.get("trend",    1.0), _mod.get("order_block", 1.0),
            _mod.get("fvg",      1.0), _mod.get("liquidity",   1.0),
            _mod.get("breakout", 1.0),
            _regime.get("TRENDING",               1.00),
            _regime.get("RANGING",                0.50),
            _regime.get("VOLATILE_EXPANSION",     0.40),
            _regime.get("VOLATILITY_COMPRESSION", 0.30),
            _regime.get("REVERSAL_ENVIRONMENT",   0.60),
        )
        log.warning(
            "[XAUUSD PROFILE] type=COMMODITY volatility=VERY_HIGH "
            "atr=EXPANDED(5-80pips) spread=WIDE(warn3/block8) "
            "session=LONDON/NEWYORK mode=MOMENTUM_TREND "
            "rr=1.5/2.5 lot_cap=0.80x",
        )
        return

    if base == "GBPJPY":
        # ── GBPJPY extended institutional banner ──────────────────────────────
        _mod = profile.get("scoring_modifiers", {})
        _regime = profile.get("regime_mult", {})
        log.warning(
            "\n"
            "  ┌──────────────────────────────────────────────────────────────┐\n"
            "  │                  GBPJPY PROFILE                              │\n"
            "  │              INSTITUTIONAL PAIR INTELLIGENCE                 │\n"
            "  ├──────────────────────────────────────────────────────────────┤\n"
            "  │  Volatility     : %-42s│\n"
            "  │  ATR Profile    : %-42s│\n"
            "  │  ATR Gates      : %-42s│\n"
            "  │  Spread         : %-42s│\n"
            "  │  Sessions       : %-42s│\n"
            "  │  Block Sessions : %-42s│\n"
            "  │  RR Profile     : %-42s│\n"
            "  │  Lot Mult Cap   : %-42s│\n"
            "  │  Execution      : %-42s│\n"
            "  │  Pip Size       : %-42s│\n"
            "  ├──────────────────────────────────────────────────────────────┤\n"
            "  │  Scoring Modifiers (vs baseline):                            │\n"
            "  │    trend=%.2f× ob=%.2f× fib=%.2f× liquidity=%.2f× breakout=%.2f×  │\n"
            "  ├──────────────────────────────────────────────────────────────┤\n"
            "  │  Regime Gates:                                               │\n"
            "  │    TRENDING=%.2f  RANGING=%.2f  VOLATILE_EXP=%.2f            │\n"
            "  │    COMPRESSION=%.2f  REVERSAL=%.2f                           │\n"
            "  └──────────────────────────────────────────────────────────────┘",
            "VERY HIGH (highest ATR major cross)",
            "EXPANDED (15-60 pips M15 ATR typical)",
            f"min={profile.get('min_atr_pips',12):.0f} ideal={profile.get('ideal_atr_min',18):.0f}-{profile.get('ideal_atr_max',55):.0f} max={profile.get('max_atr_pips',100):.0f} pips",
            f"warn≥{profile.get('spread_warn_pips',4):.0f} block≥{profile.get('spread_block_pips',9):.0f} pips",
            " / ".join(profile.get("priority_sessions", [])).upper(),
            " / ".join(profile.get("block_sessions", [])).upper(),
            f"standard={profile.get('rr_standard',2.0):.1f}R  high-conf={profile.get('rr_high_conf',3.0):.1f}R",
            f"{profile.get('lot_mult_cap',0.85):.2f}× (ATR-risk cap)",
            str(profile.get("execution_style","momentum_trend")).upper(),
            f"0.01 (JPY pair — 100 pips = 1.00 price unit)",
            _mod.get("trend",    1.0), _mod.get("order_block", 1.0),
            _mod.get("fibonacci",1.0), _mod.get("liquidity",   1.0),
            _mod.get("breakout", 1.0),
            _regime.get("TRENDING",               1.00),
            _regime.get("RANGING",                0.00),
            _regime.get("VOLATILE_EXPANSION",     0.65),
            _regime.get("VOLATILITY_COMPRESSION", 0.00),
            _regime.get("REVERSAL_ENVIRONMENT",   0.50),
        )
        log.warning(
            "[GBPJPY PROFILE] volatility=VERY_HIGH atr=EXPANDED(12-55pips) "
            "spread=WIDE(warn4/block9) session=LONDON/OVERLAP mode=MOMENTUM_TREND "
            "rr=2.0/3.0 lot_cap=0.85x",
        )
        return

    # ── Generic one-liner for all other symbols ───────────────────────────────
    log.info(
        "[SYMBOL PROFILE] [PAIR INTELLIGENCE] %s | type=%s vol=%s atr=%s "
        "rr=%.1f/%.1f sessions=%s",
        symbol,
        profile.get("type", "?"),
        profile.get("volatility", "?"),
        profile.get("atr_profile", "?"),
        profile.get("rr_standard", 1.5),
        profile.get("rr_high_conf", 2.0),
        ",".join(profile.get("sessions", [])),
    )
