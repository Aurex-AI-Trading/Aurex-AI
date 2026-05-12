"""
Aurex AI — Centralized Symbol Registry  (Phase 6)

Single source of truth for all supported trading instruments.

Rules enforced here:
  • XAUUSD  — gold, never receives a broker suffix (e.g. never XAUUSD.Z)
  • Forex   — always receive the configured broker suffix (e.g. EURUSD.Z)

Per-symbol profiles carry volatility class, ATR behaviour, preferred sessions,
RR guidance, and spread tolerance so the rest of the codebase can query one
place instead of scattering if-XAU logic everywhere.

Public API
----------
    get_broker_symbol(base, broker_suffix)  → str   e.g. "XAUUSD" or "EURUSD.Z"
    get_profile(symbol)                     → dict  full profile or {}
    get_all_broker_symbols(broker_suffix)   → list  of all canonical broker symbols
    is_no_suffix(base)                      → bool  True for gold / bare symbols
    validate_symbol_name(symbol)            → bool  True if in registry
    log_registry_summary(broker_suffix)     → None  startup banner

Tags emitted
------------
    [SYMBOL INIT]  [SYMBOL VALIDATION]  [MARKET WATCH SYNC]  [SYMBOL EXECUTION READY]
    [SYMBOL PROFILE]  [PAIR INTELLIGENCE]  [SYMBOL VERIFIED]
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
    "XAUUSD": {
        "no_suffix":          True,          # gold NEVER uses broker suffix
        "type":               "gold",
        "volatility":         "high",
        "atr_profile":        "expanded",    # ATR 5-100 pips on M15; normal forex is 5-30
        "rr_standard":        1.5,
        "rr_high_conf":       2.0,
        "spread_warn_pips":   3.0,           # gold spread > 3 pips is elevated
        "spread_block_pips":  8.0,           # gold spread > 8 pips = rollover/news spike
        "sessions":           ["london", "newyork"],
        "pip_size":           0.1,
        "notes":              "Expanded ATR. No suffix. Wide spread tolerance vs forex.",
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
    """Log the profile for a single symbol at scan startup."""
    profile = get_profile(symbol)
    if not profile:
        log.warning("[SYMBOL PROFILE] %s — NOT IN REGISTRY [PAIR INTELLIGENCE UNAVAILABLE]", symbol)
        return
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
