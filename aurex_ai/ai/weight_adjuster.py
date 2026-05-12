"""
Aurex AI — Dynamic Weight Adjuster

Adjusts confluence scoring weights in two modes:

1. Legacy mode (update_from_performance):
   Uses setup-type keyword matching on the PerformanceTracker.
   Retained for backward-compatibility. Fires at most once per UTC day.

2. Score-component mode (update_from_trade_log):
   Uses actual component scores from the TradeLogger.
   Computes (win_avg_score - loss_avg_score) per factor and nudges
   weights accordingly.  More precise signal than keyword matching.
   Fires after every N closed trades (rolling window, default 20).

Safety constraints (both modes):
  - Maximum ±2 points change per factor per adjustment cycle
  - Core weights (trend + liquidity + fvg + fib + ema + confirmation) always sum to 100
  - No factor below 5 or above 35
  - Minimum 10 trades before score-component adjustment (20 for legacy)
  - order_block is a bonus (not in the sum-to-100 core)

Weights persisted to: aurex_ai/ai/data/weights.json
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Optional

from aurex_ai.core.logger import get_logger

log = get_logger("ai.weight_adjuster")

_DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "weights.json"

# ── Boundaries ────────────────────────────────────────────────────────────────
MIN_WEIGHT       = 5
MAX_WEIGHT       = 35
MAX_DAILY_CHANGE = 2      # max points change per factor per cycle
MIN_TRADES       = 20     # legacy mode: min trades before adjusting
MIN_TRADES_COMP  = 10     # component mode: min trades before adjusting

# Minimum absolute component delta to trigger an adjustment.
# Prevents adjusting on noise (< 0.5 pt difference is not actionable).
_DELTA_UP   =  0.5    # delta >  _DELTA_UP   → increase weight
_DELTA_DOWN = -0.5    # delta < _DELTA_DOWN  → decrease weight

# How often (in closed trades) to run component-score adjustment.
_ADJUST_EVERY = 10


@dataclass
class FactorWeights:
    trend:        int = 35   # Phase 3: elevated — directional bias is the core edge
    liquidity:    int = 20   # Phase 3: reduced — sweep confirms but doesn't gate continuation
    fvg:          int = 10   # absent in Asian/ranging sessions; secondary signal
    fibonacci:    int = 15   # pullback-to-golden-zone is the primary entry mechanism
    ema:          int = 13   # Phase 3: elevated — continuation entry proximity matters
    confirmation: int = 7    # Phase 3: reduced — pattern confirms, doesn't lead
    # bonus factor — not summed to 100, added on top of core score
    order_block:  int = 15

    def as_dict(self) -> Dict[str, int]:
        return asdict(self)

    def validate(self) -> None:
        core = (
            self.trend + self.liquidity + self.fvg +
            self.fibonacci + self.ema + self.confirmation
        )
        if core != 100:
            raise ValueError(f"Core weights must sum to 100, got {core}")


_CORE_KEYS = ["trend", "liquidity", "fvg", "fibonacci", "ema", "confirmation"]

# Mapping from TradeLogger / PerformanceEngine component key → FactorWeights field
_COMPONENT_TO_WEIGHT: Dict[str, str] = {
    "trend":        "trend",
    "liquidity":    "liquidity",
    "fvg":          "fvg",
    "fib":          "fibonacci",       # logger uses fib_score; delta key = "fib"
    "fibonacci":    "fibonacci",
    "ema":          "ema",
    "confirmation": "confirmation",
    "structure":    "trend",           # structure bonus correlated with trend — nudges trend weight
    "ob":           "order_block",
    "breakout":     "order_block",     # breakout and OB share the bonus slot
}


class WeightAdjuster:
    """
    Singleton dynamic weight manager.

    Usage:
        adj = WeightAdjuster.get_instance()
        weights = adj.get_weights()

        # Called by LearningEngine after each batch of closed trades:
        adj.update_from_trade_log(trade_logger)

        # Called by LearningEngine once per day (legacy path):
        adj.update_from_performance(performance_tracker)
    """

    _instance: Optional["WeightAdjuster"] = None

    def __init__(self, path: Path = _DEFAULT_PATH) -> None:
        self._path                = path
        self._lock                = threading.Lock()
        self._weights             = self._load()
        self._last_adjusted_date: Optional[date]  = None
        self._last_trade_count:   int              = 0   # closed-trade count at last adjustment

    @classmethod
    def get_instance(cls, path: Path = _DEFAULT_PATH) -> "WeightAdjuster":
        if cls._instance is None:
            cls._instance = cls(path)
        return cls._instance

    def get_weights(self) -> FactorWeights:
        with self._lock:
            return FactorWeights(**asdict(self._weights))

    # ── Score-component mode (primary, rolling) ───────────────────────────────

    def update_from_trade_log(
        self,
        trade_logger,
        window: int = 40,
    ) -> None:
        """
        Adjust weights based on component scores of recent closed trades.

        Algorithm:
          For each scoring factor F:
            delta = mean(F_score | WIN) - mean(F_score | LOSS/BE)
            delta >  _DELTA_UP   → +1 to weight (capped at MAX_WEIGHT)
            delta < _DELTA_DOWN  → -1 to weight (floored at MIN_WEIGHT)
          Re-normalise core weights to sum to 100.

        Runs every _ADJUST_EVERY new closed trades to avoid over-fitting.
        """
        n_closed = trade_logger.total_closed()
        if n_closed < MIN_TRADES_COMP:
            return

        # Only adjust when we have _ADJUST_EVERY new trades since last run
        if n_closed - self._last_trade_count < _ADJUST_EVERY:
            return

        try:
            from aurex_ai.analytics.performance import PerformanceEngine
        except ImportError:
            return

        from aurex_ai.analytics.performance import PerformanceEngine
        report = PerformanceEngine.compute(trade_logger, window=window)
        deltas = report.component_deltas

        if not deltas:
            return

        with self._lock:
            w = asdict(self._weights)
            changes: Dict[str, int] = {}

            for comp_key, delta in deltas.items():
                wt_key = _COMPONENT_TO_WEIGHT.get(comp_key)
                if wt_key is None or wt_key not in w:
                    continue

                if delta > _DELTA_UP and w[wt_key] < MAX_WEIGHT:
                    change = min(1, MAX_WEIGHT - w[wt_key], MAX_DAILY_CHANGE)
                    w[wt_key] += change
                    changes[wt_key] = changes.get(wt_key, 0) + change

                elif delta < _DELTA_DOWN and w[wt_key] > MIN_WEIGHT:
                    change = min(1, w[wt_key] - MIN_WEIGHT, MAX_DAILY_CHANGE)
                    w[wt_key] -= change
                    changes[wt_key] = changes.get(wt_key, 0) - change

            _renormalize_core(w)
            self._weights = FactorWeights(**w)

        self._last_trade_count = n_closed

        if changes:
            log.warning(
                "[AI LEARNING] [WEIGHT ADAPTED] Component-weight update | changes=%s | "
                "trades=%d wr=%.1f%% pf=%.2f | [STRATEGY EVOLUTION]",
                changes, n_closed,
                report.win_rate * 100,
                report.profit_factor,
            )
            self._save()
            log.warning(
                "[AI LEARNING] New weights | trend=%d liq=%d fvg=%d fib=%d ema=%d conf=%d ob=%d"
                " | [OPTIMIZATION APPLIED]",
                self._weights.trend, self._weights.liquidity, self._weights.fvg,
                self._weights.fibonacci, self._weights.ema, self._weights.confirmation,
                self._weights.order_block,
            )
            # Highlight factors with large deltas as detected edges
            strong = {k: v for k, v in report.component_deltas.items() if abs(v) > 1.0}
            if strong:
                log.warning(
                    "[EDGE DETECTED] [AI LEARNING] High-signal factors: %s | "
                    "[FACTOR PERFORMANCE] deltas=%s",
                    list(strong.keys()),
                    {k: f"{v:+.3f}" for k, v in strong.items()},
                )
        else:
            log.info(
                "[AI LEARNING] Component check: no adjustment (deltas within thresholds) "
                "trades=%d [SAFE ADAPTATION]", n_closed
            )

    # ── Legacy mode (daily, setup-type keyword matching) ─────────────────────

    def update_from_performance(self, tracker) -> None:
        """
        Daily heuristic adjustment using setup-type win-rate comparison.
        Retained for backward-compatibility with LearningEngine.
        """
        from aurex_ai.core.mt5_bridge import get_mt5_time
        today = get_mt5_time().date()
        if self._last_adjusted_date == today:
            return

        stats        = tracker.get_overall_stats()
        total_trades = stats["total_trades"]

        if total_trades < MIN_TRADES:
            log.info(
                "WeightAdjuster: skipping legacy — only %d trades (need %d)",
                total_trades, MIN_TRADES,
            )
            return

        overall_wr = stats["win_rate"]

        with self._lock:
            w = asdict(self._weights)
            changes: Dict[str, int] = {}

            setup_stats  = stats.get("by_setup", {})
            _factor_map  = {
                "FVG":   "fvg",
                "OB":    "order_block",
                "Sweep": "liquidity",
                "Fib":   "fibonacci",
                "EMA":   "ema",
            }

            for kw, factor_key in _factor_map.items():
                related_wr = [
                    v["win_rate"]
                    for k, v in setup_stats.items()
                    if kw in k and v["trades"] >= 5
                ]
                if not related_wr:
                    continue

                avg_related_wr = sum(related_wr) / len(related_wr)
                delta = avg_related_wr - overall_wr

                if delta > 0.05 and w[factor_key] < MAX_WEIGHT:
                    change = min(1, MAX_WEIGHT - w[factor_key], MAX_DAILY_CHANGE)
                    w[factor_key] += change
                    changes[factor_key] = change
                elif delta < -0.05 and w[factor_key] > MIN_WEIGHT:
                    change = min(1, w[factor_key] - MIN_WEIGHT, MAX_DAILY_CHANGE)
                    w[factor_key] -= change
                    changes[factor_key] = -change

            _renormalize_core(w)
            self._weights = FactorWeights(**w)
            self._last_adjusted_date = today

        if changes:
            log.warning(
                "[AI LEARNING] [WEIGHT ADAPTED] Legacy update | changes=%s | "
                "trades=%d wr=%.1f%% | [AUTO OPTIMIZATION]",
                changes, total_trades, overall_wr * 100,
            )
            self._save()
        else:
            log.info("[AI LEARNING] Legacy check: no adjustment needed [SAFE ADAPTATION]")

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> FactorWeights:
        if self._path.exists():
            try:
                with self._path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
                weights = raw.get("weights", {})
                loaded = FactorWeights(
                    trend        = int(weights.get("trend",        35)),
                    liquidity    = int(weights.get("liquidity",    20)),
                    fvg          = int(weights.get("fvg",          10)),
                    fibonacci    = int(weights.get("fibonacci",    15)),
                    ema          = int(weights.get("ema",          13)),
                    confirmation = int(weights.get("confirmation",  7)),
                    order_block  = int(weights.get("order_block",  15)),
                )
                # Validate on load and reset to defaults if corrupt
                try:
                    loaded.validate()
                    return loaded
                except ValueError:
                    log.warning("[AI LEARNING] Loaded weights invalid — resetting to defaults [SAFE ADAPTATION]")
            except Exception as exc:
                log.warning("Failed to load weights: %s — using defaults", exc)
        return FactorWeights()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            payload = {
                "weights":      asdict(self._weights),
                "last_updated": _now_iso(),
            }
        try:
            with self._path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            log.error("Failed to save weights: %s", exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _renormalize_core(w: Dict[str, int]) -> None:
    """
    After weight nudges the core may no longer sum to 100.
    Adjust the largest core factor (trend by default if tied) to compensate.
    Ensures all factors stay within [MIN_WEIGHT, MAX_WEIGHT].
    """
    core_sum = sum(w[k] for k in _CORE_KEYS)
    if core_sum == 100:
        return
    diff    = 100 - core_sum
    largest = max(_CORE_KEYS, key=lambda k: w[k])
    new_val = w[largest] + diff
    if MIN_WEIGHT <= new_val <= MAX_WEIGHT:
        w[largest] = new_val
    else:
        # Distribute diff across multiple factors if single adjustment is out-of-bounds
        remaining = diff
        for k in _CORE_KEYS:
            if remaining == 0:
                break
            step = 1 if remaining > 0 else -1
            if MIN_WEIGHT < w[k] < MAX_WEIGHT:
                w[k]      += step
                remaining -= step


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
