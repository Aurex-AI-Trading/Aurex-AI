"""
Aurex AI — Pre-Trade Optimizer

Takes a raw signal (entry, SL, TP, lot) and applies context-aware adjustments
before the trade is sent to MT5.

Adjustments applied (in order):
  1. Volatility (ATR-based)  — widen SL/TP in high-vol, tighten in low-vol
  2. Session quality         — reduce size during Asian session
  3. Symbol win rate         — scale down lots if symbol is underperforming
  4. Drawdown protection     — halve size if daily loss > 50% of max-daily-loss

Log output:
  [OPTIMIZER]
  Symbol:      EURUSD.Z
  Direction:   BUY
  ATR:         18.3 pips
  Original SL: 20.0 pips -> Adjusted: 28.0 pips
  Original TP: 45.0 pips -> Adjusted: 63.0 pips
  Lot size:    1.00 -> 0.75 (mult=0.75)
  Reason:      High volatility + Asian session
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from aurex_ai.core.data_feed import Candle
from aurex_ai.core.logger import get_logger
from aurex_ai.execution.account_guard import cap_lot_multiplier

log = get_logger("ai.optimizer")

# Default ATR baselines per pip-size class (in pips)
_ATR_BASELINE: dict[float, float] = {
    0.0001: 15.0,   # majors / minors
    0.01:   150.0,  # JPY pairs
    0.10:   300.0,  # reserved for commodity instruments (not currently active)
    0.5:    50.0,   # indices
    1.0:    200.0,  # crypto
}


def _atr(candles: List[Candle], period: int = 14) -> float:
    """Average True Range over `period` bars."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(-period, 0):
        c    = candles[i]
        prev = candles[i - 1].close
        trs.append(max(c.high - c.low, abs(c.high - prev), abs(c.low - prev)))
    return sum(trs) / len(trs)


@dataclass
class OptimizeResult:
    sl_mult:    float   # SL distance multiplier  (1.0 = unchanged, 1.4 = widen 40%)
    tp_mult:    float   # TP distance multiplier  (matches sl_mult so R:R is preserved)
    lot_mult:   float   # lot size multiplier (0.25–1.0)
    atr_pips:   float   # current ATR in pips
    reason:     str     # human-readable summary of adjustments made


def optimize(
    symbol:         str,
    direction:      str,
    sl_pips:        float,
    tp_pips:        float,
    lot_size:       float,
    candles_m15:    List[Candle],
    utc_hour:       int,
    pip_size:       float = 0.0001,
    daily_loss_pct: float = 0.0,
    max_daily_loss: float = 5.0,
    tracker=None,                  # Optional[PerformanceTracker]
    atr_period:     int   = 14,
    atr_high_mult:  float = 1.40,  # SL/TP expansion in high-vol regime
    atr_low_mult:   float = 0.85,  # SL/TP compression in low-vol regime
) -> OptimizeResult:
    """
    Apply pre-trade optimizations and return adjusted SL, TP, and lot multiplier.

    Args:
        symbol:         Instrument name.
        direction:      "BUY" or "SELL".
        sl_pips:        Raw SL distance from risk manager (in pips).
        tp_pips:        Raw TP distance from risk manager (in pips).
        lot_size:       Base lot size from risk manager.
        candles_m15:    Recent M15 candles for ATR calculation.
        utc_hour:       Current UTC hour (0–23).
        pip_size:       Symbol pip size (0.0001, 0.01, etc.).
        daily_loss_pct: Current day's unrealised + realised loss as % of balance.
        max_daily_loss: Max daily loss % from settings.
        tracker:        PerformanceTracker instance (optional).
        atr_period:     Period for ATR calculation.
        atr_high_mult:  SL/TP multiplier when ATR is >1.3× baseline.
        atr_low_mult:   SL/TP multiplier when ATR is <0.6× baseline.

    Returns:
        OptimizeResult with adjusted SL, TP, lot multiplier, and reason string.
    """
    reasons: list[str] = []
    lot_mult = 1.0
    sl_mult  = 1.0
    tp_mult  = 1.0

    # ── 1. ATR volatility adjustment ──────────────────────────────────────────
    atr_raw  = _atr(candles_m15, period=atr_period)
    atr_pips = atr_raw / pip_size if pip_size > 0 else 0.0

    if atr_pips > 0:
        baseline  = _ATR_BASELINE.get(pip_size, atr_pips)
        atr_ratio = atr_pips / baseline if baseline > 0 else 1.0

        if atr_ratio > 1.30:
            sl_mult = atr_high_mult
            tp_mult = atr_high_mult
            reasons.append("High volatility")
        elif atr_ratio < 0.60:
            sl_mult = atr_low_mult
            tp_mult = atr_low_mult
            reasons.append("Low volatility")

    # ── 2. Session quality ────────────────────────────────────────────────────
    if 0 <= utc_hour < 7:
        lot_mult *= 0.75
        reasons.append("Asian session")
    # London/NY overlap (13–16 UTC) is prime — no reduction

    # ── 3. Symbol win rate ────────────────────────────────────────────────────
    if tracker is not None:
        sym_wr = tracker.get_symbol_win_rate(symbol, min_trades=15)
        if sym_wr is not None:
            if sym_wr < 0.40:
                lot_mult *= 0.50
                reasons.append(f"Symbol WR {sym_wr:.0%} (poor)")
            elif sym_wr < 0.50:
                lot_mult *= 0.75
                reasons.append(f"Symbol WR {sym_wr:.0%} (low)")

    # ── 4. Drawdown protection ────────────────────────────────────────────────
    if max_daily_loss > 0 and daily_loss_pct >= max_daily_loss * 0.50:
        lot_mult *= 0.50
        reasons.append(f"Drawdown {daily_loss_pct:.1f}%")

    lot_mult = cap_lot_multiplier(round(max(0.25, min(1.0, lot_mult)), 2), context="optimizer")
    sl_mult  = round(sl_mult, 2)
    tp_mult  = round(tp_mult, 2)
    reason   = " + ".join(reasons) if reasons else "No adjustments"

    _emit = log.warning if (lot_mult < 1.0 or sl_mult != 1.0) else log.info
    _emit(
        "\n[OPTIMIZER]\n"
        "  Symbol:      %s\n"
        "  Direction:   %s\n"
        "  ATR:         %.1f pips\n"
        "  SL/TP mult:  %.2f× (%s)\n"
        "  Lot mult:    %.2f (%s)\n"
        "  Reason:      %s",
        symbol, direction, atr_pips,
        sl_mult, "widen" if sl_mult > 1.0 else ("tighten" if sl_mult < 1.0 else "unchanged"),
        lot_mult, reason,
        reason,
    )

    return OptimizeResult(
        sl_mult  = sl_mult,
        tp_mult  = tp_mult,
        lot_mult = lot_mult,
        atr_pips = round(atr_pips, 1),
        reason   = reason,
    )


# ── Parameter self-optimizer ─────────────────────────────────────────────────

def _session_from_hour(hour: int) -> str:
    if 0 <= hour < 7:   return "asian"
    if 7 <= hour < 13:  return "london"
    return "new_york"


class Optimizer:
    """
    Accumulates closed-trade records and adjusts min_confidence and
    strength_threshold in the live config every 10 trades.

    Rules (one change per analysis cycle, in priority order):
      A) If win-rate of the 60–65 confidence bucket < 40%
         → raise min_confidence by +2  (max 55)
      A) If overall win-rate > 60%
         → lower min_confidence by -1  (min 25)
      B) If trades with strength < 0.6 have win-rate < 40%
         → raise strength_threshold by +0.05  (max 0.80)

    Reverts the most recent change if the next 10-trade window shows a
    win-rate drop of >= 15 percentage points.
    """

    MAX_HISTORY      = 200
    ANALYZE_EVERY    = 10
    REVERT_THRESHOLD = 0.15
    CONF_BOUNDS      = (25.0, 55.0)   # must bracket the actual operating value; old (60,80) caused relax→raise inversion
    STR_BOUNDS       = (0.50, 0.80)

    def __init__(self, cfg: object, data_path: str = "data/trades_history.json") -> None:
        self._cfg   = cfg
        self._path  = Path(data_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._history:         List[Dict]      = self._load()
        self._since_analysis:  int             = 0
        self._last_change:     Optional[Dict]  = None   # {param, section, old_value, new_value}
        self._wr_at_change:    Optional[float] = None   # win-rate of the 10 trades that triggered it

    # ── Public API ────────────────────────────────────────────────────────────

    def record(
        self,
        symbol:     str,
        direction:  str,
        confidence: float,
        strength:   float,
        atr_pips:   float,
        utc_hour:   int,
        result:     str,    # "win" | "loss" | "be"
        rr:         float,
        pnl:        float,
        timestamp:  str,
    ) -> None:
        """Record one closed trade.  Triggers analysis every ANALYZE_EVERY calls."""
        entry: Dict = {
            "symbol":     symbol,
            "direction":  direction,
            "confidence": round(float(confidence), 2),
            "strength":   round(float(strength),   3),
            "atr_pips":   round(float(atr_pips),   1),
            "session":    _session_from_hour(utc_hour),
            "result":     result,
            "rr":         round(float(rr),  2),
            "pnl":        round(float(pnl), 2),
            "timestamp":  str(timestamp),
        }
        self._history.append(entry)
        if len(self._history) > self.MAX_HISTORY:
            self._history = self._history[-self.MAX_HISTORY:]
        self._save()
        self._since_analysis += 1
        if self._since_analysis >= self.ANALYZE_EVERY:
            self._since_analysis = 0
            self._analyze()

    # ── Analysis ──────────────────────────────────────────────────────────────

    def _analyze(self) -> None:
        log.warning("[AI OPTIMIZER] analyzing last %d trades", self.ANALYZE_EVERY)
        last_n     = self._history[-self.ANALYZE_EVERY:]
        current_wr = self._win_rate(last_n)

        # Revert check — did the previous change hurt performance?
        if self._last_change is not None and self._wr_at_change is not None:
            if current_wr < self._wr_at_change - self.REVERT_THRESHOLD:
                self._revert()
                return   # no new change immediately after reverting

        # ── Rule A1: low-confidence bucket performing poorly → raise threshold ─
        low_bucket = [t for t in last_n if 60.0 <= t["confidence"] < 65.0]
        if low_bucket and self._win_rate(low_bucket) < 0.40:
            cur = self._get("scoring", "min_confidence", 70.0)
            new = min(self.CONF_BOUNDS[1], cur + 2.0)
            if new != cur:
                self._apply("min_confidence", "scoring", cur, int(new), current_wr)
                return

        # ── Rule A2: strong overall win-rate → relax threshold ────────────────
        if current_wr > 0.60:
            cur = self._get("scoring", "min_confidence", 70.0)
            new = max(self.CONF_BOUNDS[0], cur - 1.0)
            if new != cur:
                self._apply("min_confidence", "scoring", cur, int(new), current_wr)
                return

        # ── Rule B: weak-strength trades losing → raise strength gate ─────────
        low_str = [t for t in last_n if t["strength"] < 0.60]
        if low_str and self._win_rate(low_str) < 0.40:
            cur = self._get("strategy", "strength_threshold", 0.60)
            new = round(min(self.STR_BOUNDS[1], cur + 0.05), 2)
            if new != cur:
                self._apply("strength_threshold", "strategy", cur, new, current_wr)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _win_rate(self, trades: List[Dict]) -> float:
        if not trades:
            return 0.0
        return sum(1 for t in trades if t["result"] == "win") / len(trades)

    def _get(self, section: str, attr: str, default: float) -> float:
        return float(getattr(getattr(self._cfg, section, None), attr, default) or default)

    def _apply(
        self, param: str, section: str,
        old_val: float, new_val: object, wr_before: float,
    ) -> None:
        sec = getattr(self._cfg, section, None)
        if sec is not None:
            setattr(sec, param, new_val)
        self._last_change  = {"param": param, "section": section,
                              "old_value": old_val, "new_value": new_val}
        self._wr_at_change = wr_before
        log.warning("[AI OPTIMIZER] %s -> %s", param, new_val)

    def _revert(self) -> None:
        if self._last_change is None:
            return
        sec = getattr(self._cfg, self._last_change["section"], None)
        if sec is not None:
            setattr(sec, self._last_change["param"], self._last_change["old_value"])
        log.warning("[AI OPTIMIZER] reverting change due to performance drop")
        self._last_change  = None
        self._wr_at_change = None

    def _load(self) -> List[Dict]:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data[-self.MAX_HISTORY:]
        except Exception:
            pass
        return []

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(self._history, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            log.debug("[AI OPTIMIZER] save failed: %s", exc)
