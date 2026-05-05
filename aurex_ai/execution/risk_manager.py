"""
Aurex AI Signature Strategy — Risk Manager

Computes stop-loss, take-profit, and position size for a validated signal.

SL/TP placement (ATR-based — deterministic, never produces RR=0):
  BUY  -> SL = entry − ATR × sl_atr_mult   TP = entry + ATR × tp_atr_mult
  SELL -> SL = entry + ATR × sl_atr_mult   TP = entry − ATR × tp_atr_mult

  Defaults: sl_atr_mult=1.5, tp_atr_mult=3.0 → RR always = 2.0

Position sizing (volatility-adjusted fixed-risk):
  lot_size = (balance × risk_pct / 100) / (sl_pips × pip_value_per_lot)

  pip_value_per_lot is instrument-specific and sourced from MT5 symbol info.

Trade rejected when:
  - ATR == 0  (flat market / insufficient candle data)
  - SL or TP equals entry  (degenerate float edge case)
  - RR < min_rr  (default 1.5)
  - Lot size cannot be computed
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from aurex_ai.core.data_feed import Candle, AccountInfo
from aurex_ai.strategy.liquidity import SweepResult
from aurex_ai.core.logger import get_logger

log = get_logger("execution.risk")


@dataclass
class RiskResult:
    allowed:        bool
    entry:          float
    stop_loss:      float
    take_profit:    float
    lot_size:       float
    sl_pips:        float
    rr_ratio:       float
    risk_amount:    float   # in account currency
    sl_method:      str     # "sweep" | "swing" | "fallback"
    tp_method:      str     # "liquidity_zone" | "fixed_rr"
    reason:         str


# ── Pip helpers ───────────────────────────────────────────────────────────────

def _pip_size(symbol: str) -> float:
    s = symbol.upper()
    if "JPY" in s:               return 0.01
    if "BTC" in s or "ETH" in s: return 1.0
    if any(x in s for x in ("NAS", "US30", "US500", "SPX", "GER", "UK")): return 0.5
    return 0.0001


def _pip_value_per_lot(
    symbol:        str,
    symbol_info:   Dict[str, Any],
) -> float:
    """
    Return the monetary value of 1 pip per 1 standard lot in account currency.

    For standard Forex (100,000 contract):
      pip_value = pip_size × contract_size × tick_value / tick_size
    """
    pip     = _pip_size(symbol)
    cs      = symbol_info.get("trade_contract_size", 100_000)
    tv      = symbol_info.get("trade_tick_value",    10.0)
    ts      = symbol_info.get("trade_tick_size",     0.00001)
    if ts <= 0:
        ts = pip
    return pip / ts * tv


# ── Stop-loss placement ───────────────────────────────────────────────────────

def _sl_from_sweep(
    direction:   str,
    swept_level: float,
    buffer_pips: int,
    pip:         float,
) -> tuple[float, str]:
    buf = buffer_pips * pip
    if direction == "BUY":
        return round(swept_level - buf, 5), "sweep"
    return round(swept_level + buf, 5), "sweep"


def _sl_from_swing(
    direction:   str,
    candles:     List[Candle],
    buffer_pips: int,
    pip:         float,
    lookback:    int = 20,
) -> tuple[float, str]:
    bars = candles[-lookback:] if len(candles) >= lookback else candles
    buf  = buffer_pips * pip
    if direction == "BUY":
        swing_low = min(c.low for c in bars)
        return round(swing_low - buf, 5), "swing"
    swing_high = max(c.high for c in bars)
    return round(swing_high + buf, 5), "swing"


# ── Take-profit placement ─────────────────────────────────────────────────────

def _tp_from_liquidity(
    direction: str,
    entry:     float,
    candles:   List[Candle],
    lookback:  int = 100,
) -> Optional[float]:
    """
    Find the next significant swing high (SELL TP) or swing low (BUY TP)
    beyond the entry price.  Returns None if no suitable level found.
    """
    window = candles[-lookback:] if len(candles) >= lookback else candles

    if direction == "BUY":
        candidates = [c.high for c in window if c.high > entry]
        return round(min(candidates), 5) if candidates else None
    else:
        candidates = [c.low for c in window if c.low < entry]
        return round(max(candidates), 5) if candidates else None


def _tp_fixed_rr(
    direction: str,
    entry:     float,
    sl:        float,
    rr:        float,
) -> float:
    sl_dist = abs(entry - sl)
    if direction == "BUY":
        return round(entry + sl_dist * rr, 5)
    return round(entry - sl_dist * rr, 5)


# ── Position sizing ───────────────────────────────────────────────────────────

def _compute_lot_size(
    balance:       float,
    risk_pct:      float,
    sl_pips:       float,
    pip_val:       float,
    volume_min:    float = 0.01,
    volume_max:    float = 100.0,
    volume_step:   float = 0.01,
) -> float:
    """
    lot_size = risk_amount / (sl_pips × pip_value_per_lot)
    Clamped to [volume_min, volume_max] and rounded to volume_step.
    """
    if sl_pips <= 0 or pip_val <= 0:
        return volume_min

    risk_amount = balance * (risk_pct / 100.0)
    raw_lots    = risk_amount / (sl_pips * pip_val)

    # Round to volume_step
    steps    = math.floor(raw_lots / volume_step)
    lot_size = round(steps * volume_step, 2)
    return max(volume_min, min(volume_max, lot_size))


# ── ATR helper ───────────────────────────────────────────────────────────────

def _calc_atr(candles: List[Candle], period: int = 14) -> float:
    """Average True Range over `period` bars in raw price units."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(-period, 0):
        c    = candles[i]
        prev = candles[i - 1].close
        trs.append(max(c.high - c.low, abs(c.high - prev), abs(c.low - prev)))
    return sum(trs) / len(trs)


# ── Public API ────────────────────────────────────────────────────────────────

def calculate(
    direction:      str,
    entry:          float,
    account:        AccountInfo,
    candles_m15:    List[Candle],
    sweep:          SweepResult,        # kept for call-site compatibility; not used for SL
    symbol_info:    Dict[str, Any],
    symbol:         str   = "EURUSD",
    risk_pct:       float = 1.0,
    min_rr:         float = 1.5,        # reject trade if RR < this
    tp_fixed_rr:    float = 2.5,        # kept for call-site compatibility
    sl_buffer_pips: int   = 5,          # kept for call-site compatibility
    size_mult:          float = 1.0,    # 0.5 for CONDITIONAL, 0.25 for TIER3
    max_lot_size:       float = 0.0,    # hard cap; 0.0 = no cap
    max_trade_risk_pct: float = 5.0,    # survivability gate: block if dollar risk > this % of balance
    fixed_lot_size:     float = 0.0,    # when > 0: bypass ALL dynamic sizing; use exact lot
    max_sl_pips:        float = 0.0,    # balance-independent SL cap; 0.0 = disabled
    sl_atr_mult:        float = 1.5,    # SL = entry ± ATR × sl_atr_mult
    tp_atr_mult:        float = 3.0,    # TP = entry ± ATR × tp_atr_mult
    atr_period:         int   = 14,
) -> RiskResult:
    """
    Compute SL, TP, and lot size using ATR-based placement.

    SL distance = ATR(14) × sl_atr_mult  (default 1.5)
    TP distance = ATR(14) × tp_atr_mult  (default 3.0)
    RR          = tp_dist / sl_dist       (= 2.0 with defaults — never 0)

    Rejects when ATR == 0, SL/TP degenerate, or RR < min_rr.
    """
    _FAIL = lambda r: RiskResult(
        allowed=False, entry=entry, stop_loss=0.0, take_profit=0.0,
        lot_size=0.0, sl_pips=0.0, rr_ratio=0.0, risk_amount=0.0,
        sl_method="none", tp_method="none", reason=r,
    )

    pip = _pip_size(symbol)

    # ── ATR-based SL / TP placement ───────────────────────────────────────────
    atr_raw = _calc_atr(candles_m15, atr_period)
    if atr_raw <= 0.0:
        log.warning(
            "[RISK ERROR] %s %s ATR=0 — insufficient candle data or flat market",
            symbol, direction,
        )
        return _FAIL("atr_zero")

    sl_dist = atr_raw * sl_atr_mult
    tp_dist = atr_raw * tp_atr_mult

    if direction == "BUY":
        sl = round(entry - sl_dist, 5)
        tp = round(entry + tp_dist, 5)
    else:
        sl = round(entry + sl_dist, 5)
        tp = round(entry - tp_dist, 5)

    # Guard: SL or TP must not equal entry (floating-point edge case)
    if sl == entry or tp == entry:
        log.warning(
            "[RISK ERROR] %s %s SL or TP equals entry after rounding — degenerate ATR",
            symbol, direction,
        )
        return _FAIL("sl_or_tp_equals_entry")

    # Validate both levels are on the correct side of entry
    if direction == "BUY" and sl >= entry:
        return _FAIL(f"sl_above_entry sl={sl:.5f} entry={entry:.5f}")
    if direction == "SELL" and sl <= entry:
        return _FAIL(f"sl_below_entry sl={sl:.5f} entry={entry:.5f}")
    if direction == "BUY" and tp <= entry:
        return _FAIL(f"tp_below_entry tp={tp:.5f} entry={entry:.5f}")
    if direction == "SELL" and tp >= entry:
        return _FAIL(f"tp_above_entry tp={tp:.5f} entry={entry:.5f}")

    # sl_dist > 0 is guaranteed (atr_raw > 0 and sl_atr_mult > 0) — no division by zero
    sl_pips  = round(sl_dist / pip, 1) if pip > 0 else 0.0
    rr_ratio = round(tp_dist / sl_dist, 2)

    log.warning(
        "[RR] %s %s entry=%.5f sl=%.5f tp=%.5f rr=%.2f sl_pips=%.1f",
        symbol, direction, entry, sl, tp, rr_ratio, sl_pips,
    )

    if rr_ratio < min_rr:
        return RiskResult(
            allowed=False, entry=round(entry, 5),
            stop_loss=sl, take_profit=0.0,
            lot_size=0.0, sl_pips=sl_pips, rr_ratio=rr_ratio,
            risk_amount=0.0, sl_method="atr", tp_method="none",
            reason=f"rr_too_low rr={rr_ratio:.2f} min={min_rr}",
        )

    # ── Pre-trade SL distance filter (balance-independent) ────────────────────
    pip_val = _pip_value_per_lot(symbol, symbol_info)   # needed for projected risk log

    if max_sl_pips > 0 and sl_pips > max_sl_pips:
        _eff_lot   = fixed_lot_size if fixed_lot_size > 0 else symbol_info.get("volume_min", 0.01)
        _proj_risk = round(sl_pips * pip_val * _eff_lot, 2)
        _proj_pct  = round(_proj_risk / account.balance * 100, 1) if account.balance > 0 else 0.0
        _ccy_sl    = account.currency or "?"
        log.warning(
            "[RISK FILTER] symbol=%s | sl_pips=%.1f | risk=%.2f %s | risk_pct=%.1f%% "
            "→ REJECTED (sl_too_large: %.1f > %.1f pips)",
            symbol, sl_pips, _proj_risk, _ccy_sl, _proj_pct, sl_pips, max_sl_pips,
        )
        return _FAIL(f"sl_too_large: {sl_pips:.1f} > {max_sl_pips:.1f}")

    # ── Position size ─────────────────────────────────────────────────────────
    # pip_val already computed above

    if fixed_lot_size > 0:
        # Fixed lot mode — all dynamic sizing bypassed.
        # Optimizer lot_mult, session_mult, dynamic_risk_mult, execution factors,
        # size_mult, and balance-based calculations are intentionally ignored.
        raw_lots = fixed_lot_size
        log.warning(
            "[LOT MODE] FIXED | %s %s | lot=%.2f | balance=%.2f %s"
            " | dynamic sizing disabled",
            symbol, direction, raw_lots, account.balance, account.currency or "?",
        )
    else:
        # Dynamic lot mode — balance-based Kelly-style sizing.
        raw_lots = _compute_lot_size(
            balance      = account.balance,
            risk_pct     = risk_pct * size_mult,
            sl_pips      = sl_pips,
            pip_val      = pip_val,
            volume_min   = symbol_info.get("volume_min",  0.01),
            volume_max   = symbol_info.get("volume_max",  100.0),
            volume_step  = symbol_info.get("volume_step", 0.01),
        )

        if raw_lots <= 0:
            return _FAIL("lot_size_zero")

        if max_lot_size > 0 and raw_lots > max_lot_size:
            log.warning("[RISK ADJUSTED] %s lot capped %.2f -> %.2f (dynamic mode)",
                        symbol, raw_lots, max_lot_size)
            raw_lots = max_lot_size

    # Safety guard: final lot must never exceed fixed_lot_size when fixed mode is active.
    # Catches any future code path that might inflate lot after this point.
    if fixed_lot_size > 0 and raw_lots > fixed_lot_size:
        log.error(
            "[LOT GUARD] %s — lot %.2f exceeded fixed cap %.2f, forcing to %.2f",
            symbol, raw_lots, fixed_lot_size, fixed_lot_size,
        )
        raw_lots = fixed_lot_size

    risk_amount = round(raw_lots * sl_pips * pip_val, 2)

    # Survivability gate — fires when broker volume_min forces a lot size that is
    # too large relative to account balance.  The gate blocks the trade and logs
    # the minimum balance required to trade this setup safely.
    _ccy = account.currency or "?"
    if max_trade_risk_pct > 0 and account.balance > 0:
        _actual_risk_pct = risk_amount / account.balance * 100.0
        if _actual_risk_pct > max_trade_risk_pct:
            _min_balance_needed = round(risk_amount / (max_trade_risk_pct / 100.0), 2)
            log.warning(
                "[RISK GATE] %s %s BLOCKED — risk %.2f %s = %.1f%% of %.2f %s balance "
                "(cap=%.1f%%). Fund account to at least %.2f %s to trade this setup.",
                symbol, direction,
                risk_amount, _ccy, _actual_risk_pct, account.balance, _ccy,
                max_trade_risk_pct, _min_balance_needed, _ccy,
            )
            return _FAIL(
                f"risk_too_large: {risk_amount:.2f} {_ccy} "
                f"({_actual_risk_pct:.1f}% > {max_trade_risk_pct:.1f}% cap) "
                f"min_balance_required={_min_balance_needed:.2f} {_ccy}"
            )

    log.info(
        "risk: %s %s | entry=%.5f sl=%.5f tp=%.5f | "
        "sl_pips=%.1f rr=%.2f lots=%.2f risk=%.2f %s",
        symbol, direction, entry, sl, tp,
        sl_pips, rr_ratio, raw_lots, risk_amount, _ccy,
    )

    return RiskResult(
        allowed     = True,
        entry       = round(entry,  5),
        stop_loss   = sl,
        take_profit = tp,
        lot_size    = raw_lots,
        sl_pips     = sl_pips,
        rr_ratio    = rr_ratio,
        risk_amount = risk_amount,
        sl_method   = "atr",
        tp_method   = "atr",
        reason      = (
            f"sl={sl:.5f}(atr×{sl_atr_mult}) tp={tp:.5f}(atr×{tp_atr_mult}) "
            f"rr={rr_ratio:.2f} lots={raw_lots:.2f} risk={risk_amount:.2f} {_ccy}"
        ),
    )
