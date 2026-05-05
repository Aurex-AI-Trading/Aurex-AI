"""
Aurex AI — Learning Engine

Central coordinator for the self-learning subsystem.

Responsibilities:
  1. Record trade outcomes (delegates to PerformanceTracker)
  2. Trigger daily weight updates (delegates to WeightAdjuster)
  3. Expose dynamic scoring thresholds (adjusted based on overall win rate)

Call points:
  - After every trade close : engine.record_outcome(...)
  - Once per scan cycle     : engine.run_daily_update()
  - Before each scan        : thresholds = engine.get_thresholds()
                              weights    = engine.get_weights()
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from aurex_ai.core.mt5_bridge import get_mt5_time
from typing import Optional

from aurex_ai.ai.performance_tracker import PerformanceTracker
from aurex_ai.ai.weight_adjuster import WeightAdjuster, FactorWeights
from aurex_ai.core.logger import get_logger

log = get_logger("ai.learning_engine")

# Base thresholds for the three-tier system
_BASE_T1 = 75   # Tier 1: full-size execution
_BASE_T2 = 60   # Tier 2: half-size execution
_BASE_T3 = 50   # Tier 3: quarter-size fallback


@dataclass
class DynamicThresholds:
    tier1: int
    tier2: int
    tier3: int


class LearningEngine:
    """
    Singleton self-learning coordinator.

    Usage:
        engine = LearningEngine.get_instance()
        engine.record_outcome(symbol, tier, setup_type, won, pnl, rr)
        engine.run_daily_update()
        thresholds = engine.get_thresholds()
        weights    = engine.get_weights()
    """
    _instance: Optional["LearningEngine"] = None

    def __init__(self) -> None:
        self._tracker          = PerformanceTracker.get_instance()
        self._adjuster         = WeightAdjuster.get_instance()
        self._last_update_date: Optional[date] = None

    @classmethod
    def get_instance(cls) -> "LearningEngine":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Record trade outcomes ─────────────────────────────────────────────────

    def record_outcome(
        self,
        symbol:     str,
        tier:       int,
        setup_type: str,
        won:        bool,
        pnl:        float = 0.0,
        rr:         float = 0.0,
        utc_hour:   Optional[int] = None,
    ) -> None:
        """Record a closed trade result into the performance database."""
        self._tracker.record_trade(
            symbol     = symbol,
            tier       = tier,
            setup_type = setup_type,
            won        = won,
            pnl        = pnl,
            rr         = rr,
            utc_hour   = utc_hour,
        )

    # ── Daily weight update ───────────────────────────────────────────────────

    def run_daily_update(self) -> None:
        """
        Idempotent per-UTC-day update. Logs a performance snapshot and
        triggers weight adjustment if conditions are met.
        """
        today = get_mt5_time().date()
        if self._last_update_date == today:
            return

        self._last_update_date = today
        stats = self._tracker.get_overall_stats()

        log.warning(
            "[LEARNING] Daily snapshot | trades=%d wr=%.1f%% avg_rr=%.2f pnl=%.2f",
            stats["total_trades"],
            stats["win_rate"] * 100,
            stats["avg_rr"],
            stats["total_pnl"],
        )

        # Per-symbol breakdown
        for sym, v in stats.get("by_symbol", {}).items():
            if v["trades"] >= 5:
                log.info(
                    "[LEARNING]   %s  trades=%d  wr=%.1f%%",
                    sym, v["trades"], v["win_rate"] * 100,
                )

        self._adjuster.update_from_performance(self._tracker)

    # ── Dynamic thresholds ────────────────────────────────────────────────────

    def get_thresholds(
        self,
        idle_cycles: int = 0,
        base_t1:     int = _BASE_T1,
        base_t2:     int = _BASE_T2,
        base_t3:     int = _BASE_T3,
    ) -> DynamicThresholds:
        """
        Return scoring thresholds adjusted by overall win rate.

        Base values default to _BASE_T1/T2/T3 but can be overridden from
        settings.yaml (scoring.thresholds) so config is the authoritative source.

        If overall WR ≥ 60%: raise thresholds (be more selective)
        If overall WR < 45%: lower thresholds (capture more setups)
        If idle_cycles ≥ 10: lower Tier 3 by extra 5 pts (fallback mode)

        No adjustment is made until MIN_TRADES (20) have been logged.
        """
        stats = self._tracker.get_overall_stats()
        total = stats["total_trades"]
        wr    = stats["win_rate"]

        t1, t2, t3 = base_t1, base_t2, base_t3

        if total >= 20:
            if wr >= 0.60:
                t1, t2, t3 = t1 + 3, t2 + 3, t3 + 3
            elif wr < 0.45:
                t1, t2, t3 = t1 - 3, t2 - 3, t3 - 3

        if idle_cycles >= 10:
            t3 = max(32, t3 - 5)  # floor was 40 — a no-op when base_t3=40
            log.info("[LEARNING] Fallback mode: idle=%d -> Tier3 threshold=%d", idle_cycles, t3)

        return DynamicThresholds(tier1=t1, tier2=t2, tier3=t3)

    # ── Weights ───────────────────────────────────────────────────────────────

    def get_weights(self) -> FactorWeights:
        """Return the current dynamically-adjusted factor weights."""
        return self._adjuster.get_weights()

    # ── Convenience accessors ─────────────────────────────────────────────────

    def get_tracker(self) -> PerformanceTracker:
        return self._tracker
