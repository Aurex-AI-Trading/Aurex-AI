"""
Aurex AI — Strategy Version Manager  (Phase 11)

Manages the AI_FORWARD_V2 clean-baseline forward-testing regime.

PROBLEM SOLVED
--------------
  Historical trades were recorded under multiple strategy iterations:
    • Phase 1-6  — experimental, over-filtered, poor quality
    • Phase 7-9  — optimisation phase; not statistically clean
    • Phase 10   — pair intelligence + trade-source separation added

  These legacy trades pollute:
    • Win rate (16% historical is NOT the current strategy's WR)
    • Drawdown (legacy -100% is NOT the current strategy's DD)
    • Learning engine weight adaptation
    • Performance attribution

SOLUTION
--------
  AI_FORWARD_V2 is the first clean autonomous forward-test baseline.
  All trades recorded from this version carry strategy_version='AI_FORWARD_V2'.
  Legacy trades are labelled 'LEGACY' and are EXCLUDED from:
    • learning engine weight adaptation
    • adaptive learning compute windows
    • performance dashboards (version-filtered queries)
    • sample status counting

  Raw data is NEVER deleted.  MANUAL and HYBRID histories are fully preserved.
  The LEGACY label is purely a query filter.

STATISTICAL VALIDATION
-----------------------
  MIN_VALID_SAMPLE = 100 AI_AUTO trades before any conclusions are valid.
  SampleStatus tracks progress toward this threshold.

  States:
    INSUFFICIENT (n < 20)     — no meaningful conclusions possible
    BUILDING     (20 ≤ n < 100) — data accumulating; treat all metrics as provisional
    VALID        (100 ≤ n < 300) — statistically meaningful; conclusions defensible
    MATURE       (n ≥ 300)    — institutional-grade sample; stable expectancy

  Adaptive learning weight changes should only activate at BUILDING+ to prevent
  premature adaptation on noise.

Log tags
--------
    [AI BASELINE RESET]    [AI SAMPLE STATUS]    [STRATEGY VERSION]
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aurex_ai.core.logger import get_logger

log = get_logger("analytics.strategy_version")

# ── Version constants ─────────────────────────────────────────────────────────

CURRENT_VERSION: str = "AI_FORWARD_V2"
LEGACY_VERSION:  str = "LEGACY"

# Minimum AI_AUTO closed trades before statistical conclusions are drawn.
# Below this threshold: all WR/PF/expectancy values are provisional and must
# not be used to modify weights, publish metrics, or judge strategy quality.
MIN_VALID_SAMPLE: int = 100


# ── Sample status ─────────────────────────────────────────────────────────────

@dataclass
class SampleStatus:
    """Statistical validity of the current AI_FORWARD_V2 dataset."""
    n:                int    = 0
    status:           str    = "INSUFFICIENT"
    confidence_pct:   float  = 0.0    # 0–100 progress toward MIN_VALID_SAMPLE
    strategy_version: str    = CURRENT_VERSION

    @classmethod
    def from_n(cls, n: int, version: str = CURRENT_VERSION) -> "SampleStatus":
        """
        Derive status and confidence_pct from trade count.

        Confidence_pct rises non-linearly:
          0-19   →  0-30%   (INSUFFICIENT — avoid any conclusions)
          20-99  → 30-80%   (BUILDING — metrics are provisional)
          100-299→ 80-95%  (VALID — conclusions defensible)
          300+   → 95%      (MATURE — institutional-grade)
        """
        if n < 20:
            status = "INSUFFICIENT"
            conf   = round(n / 20.0 * 30.0, 1)
        elif n < MIN_VALID_SAMPLE:
            status = "BUILDING"
            conf   = round(30.0 + (n - 20) / 80.0 * 50.0, 1)
        elif n < 300:
            status = "VALID"
            conf   = round(80.0 + (n - 100) / 200.0 * 15.0, 1)
        else:
            status = "MATURE"
            conf   = 95.0

        return cls(
            n                = n,
            status           = status,
            confidence_pct   = min(95.0, conf),
            strategy_version = version,
        )

    def log_status(self) -> None:
        """Emit the [AI SAMPLE STATUS] log line."""
        if self.status == "INSUFFICIENT":
            level = "info"
        elif self.status == "BUILDING":
            level = "info"
        else:
            level = "warning"

        getattr(log, level)(
            "[AI SAMPLE STATUS] version=%s trades=%d/%d status=%s confidence=%.0f%%",
            self.strategy_version,
            self.n,
            MIN_VALID_SAMPLE,
            self.status,
            self.confidence_pct,
        )

    @property
    def is_statistically_valid(self) -> bool:
        """True when we have enough data to draw defensible conclusions."""
        return self.n >= MIN_VALID_SAMPLE

    @property
    def allow_weight_adaptation(self) -> bool:
        """Weight adaptation requires VALID status (n >= 100) to prevent noise-driven drift."""
        return self.n >= MIN_VALID_SAMPLE  # was: n >= 20 (BUILDING); now: n >= 100 (VALID)


# ── Strategy version manager ─────────────────────────────────────────────────

class StrategyVersionManager:
    """
    Singleton version tracker.

    Responsibilities:
      • Provide CURRENT_VERSION constant to callers (no mutable state)
      • Log the [AI BASELINE RESET] banner at startup
      • Provide is_learnable() filter for individual trade records

    This class intentionally has no mutable state and writes nothing to disk.
    The version label lives in the trades table (strategy_version column).
    The migration in trade_logger._init_db() handles the column stamp.
    """

    _instance: Optional["StrategyVersionManager"] = None

    def __init__(self) -> None:
        pass

    @classmethod
    def get_instance(cls) -> "StrategyVersionManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def current_version() -> str:
        """Return the active strategy version string."""
        return CURRENT_VERSION

    @staticmethod
    def is_learnable(record: dict) -> bool:
        """
        Return True if a trade record should feed the learning engine.

        A trade must be both:
          1. source-learnable (AI_AUTO or HYBRID)
          2. current-version (not LEGACY)
        """
        from aurex_ai.core.trade_source import is_learnable_source
        source  = record.get("trade_source",    LEGACY_VERSION)
        version = record.get("strategy_version", LEGACY_VERSION)
        return is_learnable_source(source) and version == CURRENT_VERSION

    def log_baseline_banner(self, ai_forward_count: int = 0) -> None:
        """
        Emit the startup [AI BASELINE RESET] banner.

        Call once at run_live() startup after the trade logger is ready.
        """
        status = SampleStatus.from_n(ai_forward_count)
        log.warning(
            "\n"
            "  ┌──────────────────────────────────────────────────────────────┐\n"
            "  │              AI FORWARD BASELINE — PHASE 11                  │\n"
            "  ├──────────────────────────────────────────────────────────────┤\n"
            "  │  Strategy Version  : %-40s│\n"
            "  │  Forward Trades    : %-40s│\n"
            "  │  Sample Status     : %-40s│\n"
            "  │  Confidence        : %-40s│\n"
            "  │  Legacy Trades     : %-40s│\n"
            "  │  Learning Gate     : %-40s│\n"
            "  └──────────────────────────────────────────────────────────────┘",
            CURRENT_VERSION,
            f"{ai_forward_count} closed AI_AUTO trades",
            status.status,
            f"{status.confidence_pct:.0f}% toward {MIN_VALID_SAMPLE}-trade minimum",
            "preserved in DB — excluded from all learning/analytics",
            "AI_FORWARD_V2 + AI_AUTO/HYBRID only",
        )
        log.warning(
            "[AI BASELINE RESET] strategy_version=%s forward_trades=%d "
            "status=%s confidence=%.0f%%",
            CURRENT_VERSION, ai_forward_count, status.status, status.confidence_pct,
        )
