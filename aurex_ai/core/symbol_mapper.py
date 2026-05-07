"""
Aurex AI — Broker Symbol Mapper
================================
Centralised handling of broker-specific symbol suffixes (e.g. HFM Premium .Z).

Many MT5 brokers append a suffix to standard FX symbol names:
    HFM Premium  → EURUSD.Z, GBPUSD.Z, USDJPY.Z
    IC Markets   → EURUSD, GBPUSD, USDJPY   (no suffix)
    Pepperstone  → EURUSD, GBPUSD, USDJPY   (no suffix)
    FXCM         → EUR/USD  (slash format — not supported)

Hard-coding suffixed symbols throughout the codebase creates brittle code that
breaks the moment a user switches broker.  This module is the single source of
truth: every other module calls into here rather than manipulating symbol
strings directly.

Public API
----------
    strip_suffix(symbol)                  → "EURUSD.Z"   → "EURUSD"
    add_suffix(base, suffix)              → "EURUSD", ".Z" → "EURUSD.Z"
    normalize(symbol)                     → canonical base for analytics keys
    to_broker_symbol(base, suffix)        → adds suffix only if not already present
    detect_broker_suffix(mt5_mod, bases)  → auto-detect from live MT5 symbol list
    ensure_visible(symbol, mt5_mod)       → add to Market Watch with retry
    validate_symbol(symbol, mt5_mod)      → full pre-trade availability check
    build_symbol_map(bases, suffix)       → {"EURUSD": "EURUSD.Z", ...}
    expand_allowed_set(bases, suffix)     → frozenset of all allowed broker symbols
    get_base_symbols(broker_syms)         → ["EURUSD.Z", ...] → ["EURUSD", ...]

Known suffixes
--------------
    KNOWN_SUFFIXES = {".Z", ".m", ".pro", ".ecn", ".r", ".raw", ".std"}

All public functions are pure (no side effects) except:
    ensure_visible()   — calls mt5.symbol_select()
    validate_symbol()  — calls mt5.symbol_info() + mt5.symbol_info_tick()
    detect_broker_suffix() — calls mt5.symbols_get()
"""
from __future__ import annotations

import time
from typing import Dict, FrozenSet, List, Optional, Set

from aurex_ai.core.logger import get_logger

log = get_logger("core.symbol_mapper")

# ── Known broker suffixes (ordered longest-first to avoid prefix collisions) ──
KNOWN_SUFFIXES: List[str] = [
    ".raw", ".pro", ".ecn", ".std", ".stp", ".cnt", ".Z", ".m", ".r", ".b",
]

# Base symbols Aurex AI trades — broker suffix is applied on top.
BASE_SYMBOLS: List[str] = ["EURUSD", "GBPUSD", "USDJPY"]

# Retry config for symbol_select failures
_SELECT_RETRIES    = 3
_SELECT_RETRY_WAIT = 0.5   # seconds between retries


# ─────────────────────────────────────────────────────────────────────────────
# Pure string helpers
# ─────────────────────────────────────────────────────────────────────────────

def strip_suffix(symbol: str) -> str:
    """
    Remove any known broker suffix from a symbol name.

    Examples:
        strip_suffix("EURUSD.Z")   → "EURUSD"
        strip_suffix("EURUSD.pro") → "EURUSD"
        strip_suffix("EURUSD")     → "EURUSD"
        strip_suffix("USDJPY.m")   → "USDJPY"
    """
    sym = symbol.strip()
    for sfx in KNOWN_SUFFIXES:
        if sym.upper().endswith(sfx.upper()):
            return sym[: -len(sfx)]
    return sym


def add_suffix(base: str, suffix: str) -> str:
    """
    Append a suffix to a base symbol, returning it unchanged if suffix is empty.

    Examples:
        add_suffix("EURUSD", ".Z")  → "EURUSD.Z"
        add_suffix("EURUSD", "")    → "EURUSD"
    """
    suffix = (suffix or "").strip()
    if not suffix:
        return base.strip()
    return base.strip() + suffix


def to_broker_symbol(base_or_broker: str, suffix: str) -> str:
    """
    Convert a base or already-suffixed symbol to the canonical broker form.

    Idempotent — safe to call even if the symbol already has the suffix.

    Examples:
        to_broker_symbol("EURUSD",   ".Z") → "EURUSD.Z"
        to_broker_symbol("EURUSD.Z", ".Z") → "EURUSD.Z"  (no double suffix)
        to_broker_symbol("EURUSD.m", ".Z") → "EURUSD.Z"  (replaces wrong suffix)
    """
    base = strip_suffix(base_or_broker)
    return add_suffix(base, suffix)


def normalize(symbol: str) -> str:
    """
    Return the canonical base symbol used as analytics / storage keys.

    Always strips any suffix so that internal performance data remains
    broker-agnostic.  Uppercase for consistency.

    Examples:
        normalize("EURUSD.Z")   → "EURUSD"
        normalize("eurusd")     → "EURUSD"
        normalize("EURUSD.pro") → "EURUSD"
    """
    return strip_suffix(symbol).upper()


def build_symbol_map(base_symbols: List[str], suffix: str) -> Dict[str, str]:
    """
    Build a mapping: base_symbol → broker_symbol.

    Example (suffix=".Z"):
        {
            "EURUSD": "EURUSD.Z",
            "GBPUSD": "GBPUSD.Z",
            "USDJPY": "USDJPY.Z",
        }
    """
    return {base: to_broker_symbol(base, suffix) for base in base_symbols}


def expand_allowed_set(base_symbols: List[str], suffix: str) -> FrozenSet[str]:
    """
    Build the frozenset of ALL allowed symbol strings for the executor whitelist.

    Includes both bare and suffixed forms so validation never fails on either.

    Example (suffix=".Z", bases=["EURUSD", "GBPUSD", "USDJPY"]):
        frozenset({"EURUSD", "EURUSD.Z", "GBPUSD", "GBPUSD.Z", "USDJPY", "USDJPY.Z"})
    """
    allowed: Set[str] = set()
    for base in base_symbols:
        base_upper = base.strip().upper()
        allowed.add(base_upper)
        broker = to_broker_symbol(base_upper, suffix)
        if broker != base_upper:
            allowed.add(broker.upper())
    return frozenset(allowed)


def get_base_symbols(broker_symbols: List[str]) -> List[str]:
    """Strip suffixes from a list of broker symbol names."""
    return [strip_suffix(s) for s in broker_symbols]


# ─────────────────────────────────────────────────────────────────────────────
# MT5-dependent helpers (pass mt5 module as argument — avoids circular imports)
# ─────────────────────────────────────────────────────────────────────────────

def detect_broker_suffix(
    mt5_mod,
    base_symbols: Optional[List[str]] = None,
) -> str:
    """
    Auto-detect the broker suffix by querying the live MT5 symbol list.

    Queries mt5.symbols_get() once, then checks which suffix converts any of
    the known base symbols into an available symbol.  Returns the first match.

    Falls back to "" (no suffix) if MT5 is unavailable or no suffix found.

    Args:
        mt5_mod:       The MetaTrader5 module (mt5).  Pass None to get "" immediately.
        base_symbols:  List of base symbols to probe.  Defaults to BASE_SYMBOLS.

    Returns:
        Suffix string, e.g. ".Z" or ".m" or "" for no suffix.

    Example:
        import MetaTrader5 as mt5
        suffix = detect_broker_suffix(mt5)   # returns ".Z" for HFM Premium
    """
    if mt5_mod is None:
        return ""

    bases = [b.upper() for b in (base_symbols or BASE_SYMBOLS)]

    try:
        all_symbols = mt5_mod.symbols_get()
    except Exception as exc:
        log.warning("[SYMBOL MAPPER] detect_broker_suffix: symbols_get() failed: %s", exc)
        return ""

    if not all_symbols:
        log.warning("[SYMBOL MAPPER] detect_broker_suffix: symbols_get() returned empty list")
        return ""

    available: Set[str] = {s.name.upper() for s in all_symbols}

    # Check each known suffix against each base symbol
    for sfx in KNOWN_SUFFIXES:
        for base in bases:
            candidate = (base + sfx).upper()
            if candidate in available:
                log.warning(
                    "[SYMBOL MAPPER] Detected broker suffix: '%s' "
                    "(found %s in Market Watch — %d total symbols available)",
                    sfx, candidate, len(available),
                )
                return sfx

    # Check bare symbols (no suffix broker)
    for base in bases:
        if base in available:
            log.warning(
                "[SYMBOL MAPPER] No broker suffix detected — bare symbol %s found "
                "(%d total symbols available)",
                base, len(available),
            )
            return ""

    log.warning(
        "[SYMBOL MAPPER] Could not detect broker suffix — none of %s (bare or suffixed) "
        "found in %d available symbols",
        bases, len(available),
    )
    return ""


def ensure_visible(symbol: str, mt5_mod, retries: int = _SELECT_RETRIES) -> bool:
    """
    Ensure a symbol is visible in the MT5 Market Watch.

    Calls mt5.symbol_select(symbol, True) with up to `retries` attempts.
    Returns True when the symbol is confirmed visible, False on failure.

    This must be called before any mt5.symbol_info() or tick/candle fetch,
    because MT5 will not return data for symbols not in Market Watch.

    Args:
        symbol:   Full broker symbol name (e.g. "EURUSD.Z").
        mt5_mod:  MetaTrader5 module.
        retries:  Maximum number of symbol_select attempts.

    Returns:
        True if visible after selection, False if all attempts failed.
    """
    if mt5_mod is None:
        return False

    for attempt in range(1, retries + 1):
        try:
            ok = mt5_mod.symbol_select(symbol, True)
            if ok:
                if attempt > 1:
                    log.warning(
                        "[SYMBOL MAPPER] %s became visible after %d attempt(s)",
                        symbol, attempt,
                    )
                return True

            log.warning(
                "[SYMBOL MAPPER] symbol_select(%s, True) returned False "
                "(attempt %d/%d)",
                symbol, attempt, retries,
            )
        except Exception as exc:
            log.warning(
                "[SYMBOL MAPPER] symbol_select(%s) raised on attempt %d/%d: %s",
                symbol, attempt, retries, exc,
            )

        if attempt < retries:
            time.sleep(_SELECT_RETRY_WAIT)

    log.error(
        "[SYMBOL MAPPER] %s could not be added to Market Watch after %d attempts. "
        "Check that the symbol exists on this broker and account type.",
        symbol, retries,
    )
    return False


def validate_symbol(
    symbol: str,
    mt5_mod,
    require_tick: bool = True,
) -> tuple[bool, str]:
    """
    Full pre-trade symbol availability check.

    Steps:
        1. ensure_visible() — add to Market Watch if needed
        2. symbol_info()    — confirm info is available
        3. trade_mode check — must not be DISABLED (0)
        4. tick freshness   — bid/ask must be non-zero (if require_tick=True)

    Args:
        symbol:       Full broker symbol name (e.g. "EURUSD.Z").
        mt5_mod:      MetaTrader5 module.  Pass None to get (True, "sim_mode").
        require_tick: When True, fail if bid==0 (feed not yet streaming).

    Returns:
        (valid: bool, reason: str)
        reason is "" on success, human-readable error description on failure.
    """
    if mt5_mod is None:
        return True, "sim_mode"

    # Step 1 — Market Watch visibility
    if not ensure_visible(symbol, mt5_mod):
        return False, f"symbol_select_failed: {symbol} not visible in Market Watch"

    # Step 2 — symbol_info
    try:
        info = mt5_mod.symbol_info(symbol)
    except Exception as exc:
        return False, f"symbol_info_exception: {exc}"

    if info is None:
        return False, f"symbol_info_none: {symbol} returned None — symbol may not exist on this broker"

    # Step 3 — trade mode
    trade_mode = getattr(info, "trade_mode", -1)
    if trade_mode == 0:
        return False, f"trade_mode=DISABLED(0): {symbol} is not tradeable on this account"

    # Step 4 — tick freshness
    if require_tick:
        try:
            tick = mt5_mod.symbol_info_tick(symbol)
        except Exception as exc:
            return False, f"symbol_info_tick_exception: {exc}"

        if tick is None:
            return False, f"no_tick: {symbol} — feed not streaming (market closed?)"
        if tick.bid == 0.0 or tick.ask == 0.0:
            return False, f"zero_prices: {symbol} bid={tick.bid} ask={tick.ask}"

    log.info(
        "[SYMBOL MAPPER] %s validated OK | trade_mode=%d",
        symbol, trade_mode,
    )
    return True, ""


def log_symbol_config(
    broker_symbols: List[str],
    suffix: str,
    broker_name: str = "",
) -> None:
    """
    Log a startup banner summarising active symbol configuration.

    Call once after suffix detection and symbol validation at startup.
    """
    bases = get_base_symbols(broker_symbols)
    log.warning(
        "\n"
        "  ┌──────────────────────────────────────────────────┐\n"
        "  │           SYMBOL CONFIGURATION                    │\n"
        "  ├──────────────────────────────────────────────────┤\n"
        "  │  Broker         : %-31s│\n"
        "  │  Suffix         : %-31s│\n"
        "  │  Broker symbols : %-31s│\n"
        "  │  Base symbols   : %-31s│\n"
        "  └──────────────────────────────────────────────────┘",
        broker_name or "unknown",
        repr(suffix) if suffix else "(none — bare symbols)",
        ", ".join(broker_symbols),
        ", ".join(bases),
    )
