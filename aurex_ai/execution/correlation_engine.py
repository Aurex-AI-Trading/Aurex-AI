"""
Aurex AI — Institutional Correlation Exposure Engine  (Phase 11)

Replaces the simple binary _check_currency_exposure() function in main.py
with a full lot-weighted directional exposure system.

PROBLEM
-------
  Binary position-count checks miss the actual dollar exposure:
    • EURUSD BUY 0.01 lot vs EURUSD BUY 1.00 lot both count as "1 USD-short"
    • Two micro-lot positions do NOT create the same correlation risk as two
      standard lots
    • Simple counts also cannot produce graduated size reductions

  This creates two failure modes:
    1. Over-restriction: micro accounts blocked from second trade unnecessarily
    2. Under-restriction: large positions escape limits designed for standard lots

SOLUTION
--------
  Lot-weighted directional net exposure per currency:

    net_USD_long  = Σ (lot_size × usd_direction) for all BUY positions where USD > 0
    net_USD_short = Σ (lot_size × |usd_direction|) for all positions with net USD < 0

  Limits are expressed in LOTS (equivalent to 1 standard lot = 1 unit):

    MAX_USD_LONG_LOTS  = 2.0  → two 1-lot USD-long positions is the ceiling
    MAX_JPY_LOTS       = 1.5  → tighter because JPY risk stacks across 3 majors

  GRADUATED RESPONSE:
    exposure < 50% of limit  → size_multiplier = 1.00 (no restriction)
    exposure 50%–80% of limit→ linear taper to 0.50
    exposure 80%–100% of limit→ linear taper to 0.10
    exposure ≥ limit         → size_multiplier = 0.00 (hard block)

CURRENCY EXPOSURE MAP
---------------------
  Each pair is decomposed into its component currencies with directional signs.
  BUY = long base / short quote.
  Signs from a BUY trade perspective:

    EURUSD BUY:  EUR+1, USD-1
    GBPUSD BUY:  GBP+1, USD-1
    USDJPY BUY:  USD+1, JPY-1
    GBPJPY BUY:  GBP+1, JPY-1
    EURJPY BUY:  EUR+1, JPY-1
    XAUUSD BUY:  XAU+1, USD-1   (gold weakening USD is a USD-short exposure)
    AUDUSD BUY:  AUD+1, USD-1
    NZDUSD BUY:  NZD+1, USD-1
    USDCAD BUY:  USD+1, CAD-1
    USDCHF BUY:  USD+1, CHF-1
    EURGBP BUY:  EUR+1, GBP-1

  For SELL trades: flip all signs.

Log tags
--------
    [CORRELATION ENGINE]    [CORRELATION BLOCK]    [EXPOSURE SUMMARY]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from aurex_ai.core.logger import get_logger

log = get_logger("execution.correlation_engine")


# ── Currency exposure map ─────────────────────────────────────────────────────
# Keys are BASE symbol names (no broker suffix).
# Values: {currency: direction_for_BUY} where +1=long, -1=short.
# For SELL: all signs are reversed.

_CURRENCY_MAP: Dict[str, Dict[str, int]] = {
    "EURUSD": {"EUR": +1, "USD": -1},
    "GBPUSD": {"GBP": +1, "USD": -1},
    "USDJPY": {"USD": +1, "JPY": -1},
    "GBPJPY": {"GBP": +1, "JPY": -1},
    "EURJPY": {"EUR": +1, "JPY": -1},
    "XAUUSD": {"XAU": +1, "USD": -1},
    "AUDUSD": {"AUD": +1, "USD": -1},
    "NZDUSD": {"NZD": +1, "USD": -1},
    "USDCAD": {"USD": +1, "CAD": -1},
    "USDCHF": {"USD": +1, "CHF": -1},
    "EURGBP": {"EUR": +1, "GBP": -1},
    "AUDCAD": {"AUD": +1, "CAD": -1},
    "CADJPY": {"CAD": +1, "JPY": -1},
    "CHFJPY": {"CHF": +1, "JPY": -1},
    "EURAUD": {"EUR": +1, "AUD": -1},
    "EURCAD": {"EUR": +1, "CAD": -1},
    "EURCHF": {"EUR": +1, "CHF": -1},
    "GBPAUD": {"GBP": +1, "AUD": -1},
    "GBPCAD": {"GBP": +1, "CAD": -1},
    "GBPCHF": {"GBP": +1, "CHF": -1},
    "AUDNZD": {"AUD": +1, "NZD": -1},
    "AUDCHF": {"AUD": +1, "CHF": -1},
    "AUDCAD": {"AUD": +1, "CAD": -1},
    "NZDCAD": {"NZD": +1, "CAD": -1},
    "NZDJPY": {"NZD": +1, "JPY": -1},
    "NZDCHF": {"NZD": +1, "CHF": -1},
}

# ── Default exposure limits ───────────────────────────────────────────────────
# Expressed in standard-lot equivalents of net directional exposure.
# Separate long/short limits for asymmetric pairs (USD, JPY, GBP).

_DEFAULT_LIMITS: Dict[str, float] = {
    "USD_long":  2.0,    # max net long-USD exposure in lots
    "USD_short": 2.0,    # max net short-USD exposure in lots
    "JPY_long":  1.5,    # JPY stacks across USDJPY + GBPJPY + EURJPY
    "JPY_short": 1.5,
    "GBP_long":  1.5,    # GBP stacks across GBPUSD + GBPJPY + GBPAUD...
    "GBP_short": 1.5,
    "EUR_long":  1.5,
    "EUR_short": 1.5,
    "XAU_long":  1.0,    # gold is standalone; 1 lot = large notional
    "XAU_short": 1.0,
}

# ── Graduated size-reduction thresholds ──────────────────────────────────────
# When exposure is between SOFT and HARD thresholds, size is linearly tapered.
_SOFT_THRESHOLD_RATIO = 0.50   # below: no restriction
_HARD_THRESHOLD_RATIO = 1.00   # at/above: hard block (size_mult=0.0)
_MIN_MULT_AT_SOFT     = 1.00   # full size at soft threshold
_MIN_MULT_AT_HARD     = 0.00   # blocked at hard limit


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class CorrelationResult:
    """Result from CorrelationEngine.evaluate()."""
    allowed:          bool             # False = hard block regardless of mult
    size_multiplier:  float            # 0.0–1.0; 1.0 = no restriction
    block_reason:     str              # "" when allowed; human-readable when blocked
    net_exposure:     Dict[str, float] = field(default_factory=dict)  # currency→net lots
    limiting_key:     str              = ""   # which limit was binding


# ── Engine ────────────────────────────────────────────────────────────────────

class CorrelationEngine:
    """
    Lot-weighted directional exposure guard.

    Instantiate once per session with the config object.
    Call evaluate() on each candidate trade.

    Usage:
        engine = CorrelationEngine(cfg)
        result = engine.evaluate("GBPJPY.Z", "BUY", open_positions)
        if not result.allowed:
            return None
        size_mult *= result.size_multiplier
    """

    def __init__(self, cfg=None) -> None:
        self._limits = dict(_DEFAULT_LIMITS)
        if cfg is not None:
            self._load_limits_from_cfg(cfg)

    def _load_limits_from_cfg(self, cfg) -> None:
        """Override default limits from settings.yaml correlation: section."""
        corr_cfg = getattr(cfg, "correlation", None)
        if corr_cfg is None:
            return
        _map = {
            "max_usd_longs":    ("USD_long",  float),
            "max_usd_shorts":   ("USD_short", float),
            "max_gbp_exposure": ("GBP_long",  float),
            "max_jpy_exposure": ("JPY_long",  float),
        }
        for attr, (key, cast) in _map.items():
            val = getattr(corr_cfg, attr, None)
            if val is not None:
                self._limits[key]          = cast(val)
                # Apply same limit to the matching short side
                short_key = key.replace("_long", "_short")
                if short_key in self._limits:
                    self._limits[short_key] = cast(val)

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(
        self,
        symbol:         str,
        direction:      str,          # "BUY" | "SELL"
        open_positions: List[Dict],   # each has "symbol", "type", "volume"
        cfg=None,
    ) -> CorrelationResult:
        """
        Evaluate whether a new trade would breach correlation limits.

        Returns CorrelationResult with:
          .allowed           — False = hard block
          .size_multiplier   — 0.0–1.0 (1.0 = no restriction)
          .block_reason      — human-readable if blocked
          .net_exposure      — snapshot of current lot-weighted exposure

        Also emits [CORRELATION ENGINE] telemetry log.
        """
        corr_cfg = getattr(cfg, "correlation", None) if cfg else None
        enabled  = bool(getattr(corr_cfg, "enabled", True))
        if not enabled:
            return CorrelationResult(
                allowed=True, size_multiplier=1.0,
                block_reason="", net_exposure={},
            )

        # Current exposure from all open positions
        current = self._weighted_exposure(open_positions)

        # Contribution from the new candidate trade
        candidate = self._symbol_exposure_vector(symbol, direction)

        # Compute size multiplier: smallest across all currencies
        global_mult   = 1.0
        limiting_key  = ""
        block_reason  = ""

        for currency, add_lots in candidate.items():
            if abs(add_lots) < 1e-6:
                continue

            # Determine which limit key applies
            if add_lots > 0:
                limit_key = f"{currency}_long"
                existing  = max(0.0, current.get(f"{currency}_long", 0.0))
            else:
                limit_key = f"{currency}_short"
                existing  = max(0.0, current.get(f"{currency}_short", 0.0))

            limit = self._limits.get(limit_key, 99.0)
            mult  = self._size_mult(existing, abs(add_lots), limit)

            if mult < global_mult:
                global_mult  = mult
                limiting_key = limit_key

            if mult == 0.0 and not block_reason:
                block_reason = (
                    f"net_{limit_key.lower()}={existing + abs(add_lots):.2f}lots "
                    f"exceeds limit={limit:.2f}lots"
                )

        allowed = global_mult > 0.0

        # ── Telemetry ─────────────────────────────────────────────────────────
        self._log_telemetry(
            symbol, direction,
            current, candidate,
            global_mult, limiting_key, allowed,
        )

        return CorrelationResult(
            allowed         = allowed,
            size_multiplier = round(global_mult, 2),
            block_reason    = block_reason,
            net_exposure    = {
                k: round(v, 3) for k, v in current.items() if abs(v) > 0.001
            },
            limiting_key    = limiting_key,
        )

    def get_net_exposure(self, open_positions: List[Dict]) -> Dict[str, float]:
        """
        Return the current lot-weighted net exposure per currency direction.

        Keyed as "USD_long", "USD_short", "JPY_long", etc.
        Only non-zero values are included.
        """
        raw = self._weighted_exposure(open_positions)
        return {k: round(v, 3) for k, v in raw.items() if abs(v) > 0.001}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _weighted_exposure(self, positions: List[Dict]) -> Dict[str, float]:
        """
        Sum lot-weighted directional exposure across all open positions.

        Each position contributes lot_size * sign to the relevant currency bucket.
        Long and short are kept separate (e.g. USD_long and USD_short).
        """
        exposure: Dict[str, float] = {}
        for pos in positions:
            sym  = pos.get("symbol", "")
            dirn = pos.get("type", pos.get("direction", "BUY"))
            lots = float(pos.get("volume", pos.get("lot_size", 0.0)) or 0.0)
            if lots <= 0:
                continue
            vec = self._symbol_exposure_vector(sym, dirn)
            for currency, signed_lots in vec.items():
                if signed_lots > 0:
                    k = f"{currency}_long"
                else:
                    k = f"{currency}_short"
                exposure[k] = exposure.get(k, 0.0) + abs(signed_lots * lots)
        return exposure

    def _symbol_exposure_vector(self, symbol: str, direction: str) -> Dict[str, float]:
        """
        Return the per-currency lot-exposure of a 1-lot position.

        For BUY: uses the raw _CURRENCY_MAP signs.
        For SELL: flips all signs.

        Returns lot-fractions (not lot-weights) — caller multiplies by actual lots.
        """
        from aurex_ai.core.symbol_mapper import strip_suffix
        base = strip_suffix(symbol).upper()
        raw  = _CURRENCY_MAP.get(base, {})
        sign = 1 if direction.upper() == "BUY" else -1
        return {currency: sign * direction_sign for currency, direction_sign in raw.items()}

    @staticmethod
    def _size_mult(existing_lots: float, add_lots: float, limit: float) -> float:
        """
        Compute the size multiplier for a given exposure delta.

        Graduated reduction:
          exposure < 50% of limit   → 1.00 (no restriction)
          exposure 50%-100% of limit → linear taper: 1.00 → 0.00
          exposure ≥ limit           → 0.00 (hard block)

        'existing_lots' is the current same-direction exposure in lots.
        'add_lots'      is the positive lot contribution of the new trade.
        'limit'         is the maximum allowed exposure in lots.
        """
        after = existing_lots + add_lots
        if after >= limit:
            return 0.0

        soft = limit * _SOFT_THRESHOLD_RATIO
        if after <= soft:
            return 1.0

        # Linear taper from 1.0 (at soft) to 0.0 (at hard limit)
        ratio = (after - soft) / (limit - soft)
        return round(max(0.0, 1.0 - ratio), 3)

    def _log_telemetry(
        self,
        symbol:        str,
        direction:     str,
        current:       Dict[str, float],
        candidate:     Dict[str, float],
        size_mult:     float,
        limiting_key:  str,
        allowed:       bool,
    ) -> None:
        """Emit the [CORRELATION ENGINE] diagnostic log."""
        # Build compact exposure summary
        def _fmt(d: Dict[str, float]) -> str:
            parts = []
            for currency in ("USD_long", "USD_short", "JPY_long", "JPY_short",
                             "GBP_long", "GBP_short", "EUR_long", "EUR_short",
                             "XAU_long", "XAU_short"):
                val = d.get(currency, 0.0)
                if abs(val) > 0.001:
                    parts.append(f"{currency}={val:.2f}")
            return " | ".join(parts) if parts else "none"

        action = "ALLOW" if allowed else "BLOCK"
        if allowed and size_mult < 1.0:
            action = f"REDUCE(×{size_mult:.2f})"

        if not allowed:
            log.warning(
                "[CORRELATION ENGINE] [CORRELATION BLOCK] %s %s "
                "action=%s limiting=%s | current: %s",
                symbol, direction, action,
                limiting_key,
                _fmt(current),
            )
        elif size_mult < 1.0:
            log.warning(
                "[CORRELATION ENGINE] [EXPOSURE SUMMARY] %s %s "
                "action=%s limiting=%s mult=%.2f | current: %s",
                symbol, direction, action,
                limiting_key, size_mult,
                _fmt(current),
            )
        else:
            log.info(
                "[CORRELATION ENGINE] %s %s action=ALLOW | exposure: %s",
                symbol, direction, _fmt(current),
            )
