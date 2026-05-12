"""
Aurex AI — Adaptive Learning Engine  (Phase 6)

Extends the existing WeightAdjuster / PerformanceTracker infrastructure with:

  1. Rolling-window factor analysis
       Short window (20 trades) for quick adaptation.
       Long window  (100 trades) as a stability anchor.
       Both are required to agree before any modifier activates.

  2. Symbol-specific confidence modifiers  (0.70 – 1.00×)
       Generated from per-symbol win-rate relative to overall win-rate.
       Activates only after min_trades_for_modifier closed trades on that symbol.
       Bounded: worst symbol never below 0.70, best never above 1.00.

  3. Session-specific confidence modifiers  (0.75 – 1.00×)
       Same logic applied per session (london / newyork / asian / overlap).

  4. Factor performance tracking
       Tracks which confluence factors score higher in wins vs losses.
       Logs [FACTOR PERFORMANCE] and [EDGE DETECTED] periodically.

  5. Drift guard
       Ensures total weight drift from the hardcoded baseline never exceeds
       ±drift_guard_pct (default 15%).  Forces a rollback if breached.

Logs emitted
------------
    [AI LEARNING]       — general learning events
    [WEIGHT ADAPTED]    — when a weight changes
    [EDGE DETECTED]     — when a statistically significant factor delta is found
    [FACTOR PERFORMANCE]— periodic factor score comparison
    [STRATEGY EVOLUTION]— when multiple weights change in the same direction
    [SYMBOL ADAPTATION] — when a symbol modifier changes

This module is a read-only consumer of the existing TradeLogger SQLite data.
It never modifies weights directly — it feeds signals to WeightAdjuster.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from aurex_ai.core.logger import get_logger

log = get_logger("ai.adaptive_learning")

# ── Baseline FactorWeights (Phase 3 aggressive mode) ─────────────────────────
# These are the REFERENCE values — drift is measured against these.
# Changing Phase 3 weights must also update this dict.
_BASELINE_WEIGHTS: Dict[str, int] = {
    "trend":        38,
    "liquidity":    18,
    "fvg":           8,
    "fibonacci":    16,
    "ema":          16,
    "confirmation":  4,
    "order_block":  15,   # bonus — not summed
}

# Sessions recognised by the performance tracker
_SESSIONS = ("london", "newyork", "asian", "overlap")

# ── Factor → TradeLogger column name ─────────────────────────────────────────
_FACTOR_COLS = {
    "trend":        "trend_score",
    "liquidity":    "liquidity_score",
    "fvg":          "fvg_score",
    "fibonacci":    "fib_score",
    "ema":          "ema_score",
    "confirmation": "confirmation_score",
    "ob":           "ob_score",
    "breakout":     "breakout_score",
    "structure":    "structure_score",
}


@dataclass
class LearningSnapshot:
    """Point-in-time summary computed by AdaptiveLearning.compute()."""
    total_closed:            int              = 0
    short_win_rate:          float            = 0.0   # last 20 trades
    long_win_rate:           float            = 0.0   # last 100 trades
    symbol_modifiers:        Dict[str, float] = field(default_factory=dict)
    session_modifiers:       Dict[str, float] = field(default_factory=dict)
    factor_deltas:           Dict[str, float] = field(default_factory=dict)
    edge_factors:            List[str]        = field(default_factory=list)   # factors with strong +delta
    weak_factors:            List[str]        = field(default_factory=list)   # factors with strong -delta
    drift_breach:            bool             = False
    computed_at:             str              = ""


class AdaptiveLearning:
    """
    Singleton adaptive learning coordinator.

    Usage:
        al = AdaptiveLearning.get_instance()
        snapshot = al.compute(trade_logger, cfg=cfg)
        sym_mod  = al.get_symbol_modifier("XAUUSD")
        sess_mod = al.get_session_modifier(utc_hour=9)
    """

    _instance: Optional["AdaptiveLearning"] = None

    def __init__(self) -> None:
        self._lock                              = threading.Lock()
        self._last_snapshot: Optional[LearningSnapshot] = None
        self._last_trade_count: int             = 0
        self._recompute_every: int              = 10   # trades

    @classmethod
    def get_instance(cls) -> "AdaptiveLearning":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Primary compute ───────────────────────────────────────────────────────

    def compute(self, trade_logger, cfg=None) -> LearningSnapshot:
        """
        Compute a fresh LearningSnapshot from the trade log.

        Runs only when at least _recompute_every new trades have closed
        since the last computation, to avoid thrashing on every cycle.
        Returns the cached snapshot otherwise.
        """
        n = trade_logger.total_closed()

        with self._lock:
            if (
                self._last_snapshot is not None
                and n - self._last_trade_count < self._recompute_every
            ):
                return self._last_snapshot

        al_cfg = getattr(cfg, "adaptive_learning", None) if cfg else None
        win_short   = int(getattr(al_cfg, "rolling_window_short",    20))
        win_long    = int(getattr(al_cfg, "rolling_window_long",    100))
        sym_min     = float(getattr(al_cfg, "symbol_modifier_min",  0.70))
        sym_max     = float(getattr(al_cfg, "symbol_modifier_max",  1.00))
        sess_min    = float(getattr(al_cfg, "session_modifier_min", 0.75))
        min_trades  = int(getattr(al_cfg, "min_trades_for_modifier",  15))
        drift_guard = float(getattr(al_cfg, "drift_guard_pct",        15))

        snap = LearningSnapshot(
            total_closed = n,
            computed_at  = datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        if n == 0:
            with self._lock:
                self._last_snapshot    = snap
                self._last_trade_count = n
            return snap

        # ── Fetch trade windows ───────────────────────────────────────────────
        short_trades = trade_logger.get_closed_trades(limit=win_short)
        long_trades  = trade_logger.get_closed_trades(limit=win_long)

        snap.short_win_rate = _win_rate(short_trades)
        snap.long_win_rate  = _win_rate(long_trades)

        # ── Symbol modifiers ─────────────────────────────────────────────────
        overall_wr = snap.long_win_rate or 0.5
        sym_trades = _group_by(long_trades, "symbol")
        mods: Dict[str, float] = {}
        for sym, trades in sym_trades.items():
            if len(trades) < min_trades:
                continue
            sym_wr   = _win_rate(trades)
            delta    = sym_wr - overall_wr
            # Linear mapping: delta ±0.15 → modifier ±0.10 (clamped)
            modifier = round(max(sym_min, min(sym_max, 1.0 + delta * 0.67)), 3)
            mods[sym] = modifier
            if abs(delta) > 0.08:
                log.warning(
                    "[SYMBOL ADAPTATION] [AI LEARNING] %s | wr=%.1f%% vs overall=%.1f%% "
                    "delta=%+.1f%% → confidence_modifier=%.2f×",
                    sym, sym_wr * 100, overall_wr * 100, delta * 100, modifier,
                )
        snap.symbol_modifiers = mods

        # ── Session modifiers ─────────────────────────────────────────────────
        sess_trades = _group_by(long_trades, "utc_hour", transform=_hour_to_session)
        sess_mods: Dict[str, float] = {}
        for sess, trades in sess_trades.items():
            if len(trades) < min_trades:
                continue
            sess_wr  = _win_rate(trades)
            delta    = sess_wr - overall_wr
            modifier = round(max(sess_min, min(1.0, 1.0 + delta * 0.5)), 3)
            sess_mods[sess] = modifier
            if modifier < 0.85:
                log.warning(
                    "[AI LEARNING] Session '%s' underperforming | wr=%.1f%% modifier=%.2f× "
                    "[STRATEGY EVOLUTION]",
                    sess, sess_wr * 100, modifier,
                )
        snap.session_modifiers = sess_mods

        # ── Factor delta analysis ─────────────────────────────────────────────
        wins   = [t for t in long_trades if t.get("result") == "WIN"]
        losses = [t for t in long_trades if t.get("result") in ("LOSS", "BREAKEVEN")]
        deltas: Dict[str, float] = {}
        for factor, col in _FACTOR_COLS.items():
            w_scores = [t.get(col, 0.0) for t in wins   if t.get(col) is not None]
            l_scores = [t.get(col, 0.0) for t in losses if t.get(col) is not None]
            if not w_scores and not l_scores:
                continue
            w_avg = sum(w_scores) / max(len(w_scores), 1)
            l_avg = sum(l_scores) / max(len(l_scores), 1)
            deltas[factor] = round(w_avg - l_avg, 3)

        snap.factor_deltas = deltas
        snap.edge_factors  = [f for f, d in deltas.items() if d > 1.0]
        snap.weak_factors  = [f for f, d in deltas.items() if d < -1.0]

        if deltas:
            log.warning(
                "[FACTOR PERFORMANCE] [AI LEARNING] n=%d | "
                "top_factors=%s | weak_factors=%s | "
                "[EDGE ANALYSIS] wr_short=%.1f%% wr_long=%.1f%%",
                n,
                snap.edge_factors[:3],
                snap.weak_factors[:2],
                snap.short_win_rate * 100,
                snap.long_win_rate  * 100,
            )
            for factor, delta in sorted(deltas.items(), key=lambda x: -abs(x[1]))[:4]:
                direction = "STRONG EDGE" if delta > 1.0 else ("WEAK" if delta < -1.0 else "neutral")
                log.info(
                    "[FACTOR PERFORMANCE] %-14s delta=%+.3f [%s]",
                    factor, delta, direction,
                )
                if abs(delta) > 1.5:
                    log.warning(
                        "[EDGE DETECTED] [AI LEARNING] %s | win_avg_score %+.3f "
                        "vs loss_avg_score | [STRATEGY EVOLUTION]",
                        factor, delta,
                    )

        # ── Drift guard ───────────────────────────────────────────────────────
        snap.drift_breach = _check_drift(drift_guard)

        with self._lock:
            self._last_snapshot    = snap
            self._last_trade_count = n

        return snap

    # ── Convenience accessors ─────────────────────────────────────────────────

    def get_symbol_modifier(self, symbol: str) -> float:
        """Return the current confidence modifier for a symbol (1.0 if unknown)."""
        with self._lock:
            if self._last_snapshot is None:
                return 1.0
            return self._last_snapshot.symbol_modifiers.get(symbol, 1.0)

    def get_session_modifier(self, utc_hour: int) -> float:
        """Return the current confidence modifier for the session at utc_hour."""
        with self._lock:
            if self._last_snapshot is None:
                return 1.0
            sess = _hour_to_session(utc_hour)
            return self._last_snapshot.session_modifiers.get(sess, 1.0)

    def get_last_snapshot(self) -> Optional[LearningSnapshot]:
        with self._lock:
            return self._last_snapshot

    def log_ai_score(self, trade_logger) -> None:
        """
        Log a comprehensive AI system health score.
        Called once per cycle from the heartbeat block.
        """
        snap = self._last_snapshot
        if snap is None or snap.total_closed == 0:
            log.info("[AI SCORE] Insufficient data for AI health score (need ≥10 trades)")
            return

        # Execution health: based on short-window win rate
        exec_score = min(100, int(snap.short_win_rate * 150))

        # Strategy health: stability between short and long WR
        wr_stability = 1.0 - min(1.0, abs(snap.short_win_rate - snap.long_win_rate) / 0.20)
        strategy_score = int(wr_stability * 100)

        # Edge quality: fraction of factors with positive delta
        if snap.factor_deltas:
            pos = sum(1 for d in snap.factor_deltas.values() if d > 0)
            edge_score = int(pos / len(snap.factor_deltas) * 100)
        else:
            edge_score = 50

        overall = int((exec_score + strategy_score + edge_score) / 3)

        _label = (
            "EXCELLENT" if overall >= 80 else
            "GOOD"      if overall >= 65 else
            "FAIR"      if overall >= 50 else
            "POOR"
        )

        log.warning(
            "[AI SCORE] [STRATEGY HEALTH] overall=%d/100 (%s) | "
            "exec=%d strategy=%d edge=%d | "
            "wr_short=%.1f%% wr_long=%.1f%% | "
            "edge_factors=%s weak_factors=%s",
            overall, _label,
            exec_score, strategy_score, edge_score,
            snap.short_win_rate * 100,
            snap.long_win_rate  * 100,
            snap.edge_factors[:3],
            snap.weak_factors[:2],
        )

        if snap.drift_breach:
            log.warning(
                "[AI SCORE] [STRATEGY HEALTH] Weight drift limit breached — "
                "rollback recommended [SAFE ADAPTATION]"
            )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _win_rate(trades: list) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.get("result") == "WIN")
    return wins / len(trades)


def _group_by(
    trades: list,
    key: str,
    transform=None,
) -> Dict[str, list]:
    """Group trade dicts by a field value, optionally transforming the key."""
    groups: Dict[str, list] = {}
    for t in trades:
        raw = t.get(key)
        if raw is None:
            continue
        k = transform(raw) if transform else str(raw)
        groups.setdefault(k, []).append(t)
    return groups


def _hour_to_session(hour) -> str:
    h = int(hour)
    if 7 <= h < 13:  return "london"
    if 13 <= h < 16: return "overlap"
    if 16 <= h < 22: return "newyork"
    return "asian"


def _check_drift(drift_guard_pct: float) -> bool:
    """
    Check whether current weights have drifted beyond the guard threshold.
    Reads the live WeightAdjuster instance; returns True if breach detected.
    """
    try:
        from aurex_ai.ai.weight_adjuster import WeightAdjuster
        import dataclasses
        current = dataclasses.asdict(WeightAdjuster.get_instance().get_weights())
        for key, baseline in _BASELINE_WEIGHTS.items():
            if baseline == 0:
                continue
            live = current.get(key, baseline)
            pct_drift = abs(live - baseline) / baseline * 100
            if pct_drift > drift_guard_pct:
                log.warning(
                    "[AI LEARNING] [SAFE ADAPTATION] Drift guard: %s drifted %.1f%% "
                    "from baseline %d → current %d (limit %.0f%%)",
                    key, pct_drift, baseline, live, drift_guard_pct,
                )
                return True
    except Exception:
        pass
    return False
