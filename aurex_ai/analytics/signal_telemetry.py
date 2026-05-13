"""
Aurex AI — Signal Telemetry Engine  (Phase 12)

Provides institutional-grade signal telemetry across the full filter pipeline.

Tracks:
  1. Signal funnel — how many signals reach each pipeline stage
  2. Rejection attribution — which gates fire and at what rate
  3. Near-miss detection — signals within margin of execution threshold
  4. Pipeline stage visibility — PASS/BLOCK logging at each gate
  5. Filter analytics — periodic % breakdown of why signals are rejected

All counters are in-memory (reset on restart). Use get_funnel_stats()
for Supabase sync; emit log methods for observability.

Logs emitted
------------
  [SIGNAL PIPELINE]   — stage-by-stage PASS/BLOCK tracking
  [SIGNAL REJECTED]   — full factor scores on rejection
  [NEAR MISS]         — signal within threshold margin
  [SIGNAL FUNNEL]     — periodic funnel breakdown
  [FILTER ANALYTICS]  — periodic rejection reason % breakdown
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

from aurex_ai.core.logger import get_logger

log = get_logger("analytics.signal_telemetry")

# ── Pipeline stage names (in order) ──────────────────────────────────────────
STAGE_SCAN           = "SCAN"
STAGE_SESSION        = "SESSION"
STAGE_CAPACITY       = "CAPACITY"
STAGE_NEWS           = "NEWS"
STAGE_DATA           = "DATA"
STAGE_SPREAD         = "SPREAD"
STAGE_ATR            = "ATR"
STAGE_CONFIDENCE     = "CONFIDENCE"
STAGE_TREND          = "TREND"
STAGE_MTF            = "MTF"
STAGE_REGIME         = "REGIME"
STAGE_PAIR           = "PAIR"
STAGE_CONFLUENCE     = "CONFLUENCE"
STAGE_POSTCONFLUENCE = "POSTCONFLUENCE"
STAGE_EXECUTION      = "EXECUTION"

PIPELINE_STAGES = [
    STAGE_SCAN,
    STAGE_SESSION,
    STAGE_CAPACITY,
    STAGE_NEWS,
    STAGE_DATA,
    STAGE_SPREAD,
    STAGE_ATR,
    STAGE_CONFIDENCE,
    STAGE_TREND,
    STAGE_MTF,
    STAGE_REGIME,
    STAGE_PAIR,
    STAGE_CONFLUENCE,
    STAGE_POSTCONFLUENCE,
    STAGE_EXECUTION,
]

# Signals within this many pts of the execution threshold are flagged [NEAR MISS]
_NEAR_MISS_MARGIN = 8


@dataclass
class SignalContext:
    """Full signal context captured at post-confluence rejection time."""
    symbol:          str   = ""
    direction:       str   = ""
    stage:           str   = ""      # pipeline stage where rejected
    reason:          str   = ""      # rejection reason code
    signal_score:    float = 0.0
    confidence:      float = 0.0
    core_score:      float = 0.0
    quality_grade:   str   = ""      # A+/A/B/C
    tier:            int   = 0
    atr_pips:        float = 0.0
    spread_pips:     float = 0.0
    session:         str   = ""
    utc_hour:        int   = 0
    market_state:    str   = ""
    trend_score:     float = 0.0
    ob_score:        float = 0.0
    fvg_score:       float = 0.0
    liquidity_score: float = 0.0
    fib_score:       float = 0.0
    ema_score:       float = 0.0
    breakout_score:  float = 0.0
    structure_score: float = 0.0
    trend_strength:  float = 0.0
    near_threshold:  bool  = False
    threshold_used:  float = 0.0


class SignalTelemetry:
    """
    Singleton signal telemetry engine.

    All counters are in-memory and reset on process restart.

    Usage:
        tel = SignalTelemetry.get_instance()
        tel.count_scan(symbol)
        tel.pass_stage(STAGE_SESSION, symbol)
        tel.block_stage(STAGE_SESSION, "BAD_SESSION", symbol)
        tel.log_signal_rejected(ctx)          # post-confluence full rejection
        tel.log_near_miss(symbol, "BUY", 52, 58, "LOW_CONFIDENCE")
        # Periodic:
        tel.log_funnel()
        tel.log_filter_analytics()
    """

    _instance: Optional["SignalTelemetry"] = None

    def __init__(self) -> None:
        self._lock                         = threading.Lock()
        self._funnel: Dict[str, int]       = {s: 0 for s in PIPELINE_STAGES}
        self._rejected: Dict[str, int]     = {}
        self._near_miss_count: int         = 0
        self._total_scanned: int           = 0
        self._total_executed: int          = 0

    @classmethod
    def get_instance(cls) -> "SignalTelemetry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Funnel counting ───────────────────────────────────────────────────────

    def count_scan(self, symbol: str) -> None:
        """Call once at the entry of scan_symbol(). Increments SCAN counter."""
        with self._lock:
            self._funnel[STAGE_SCAN] += 1
            self._total_scanned      += 1

    def pass_stage(self, stage: str, symbol: str = "") -> None:
        """Record that a symbol-scan passed this pipeline stage (no log emitted)."""
        with self._lock:
            if stage in self._funnel:
                self._funnel[stage] += 1

    def block_stage(self, stage: str, reason: str, symbol: str = "") -> None:
        """
        Record that a symbol-scan was blocked at this stage.
        Emits [SIGNAL PIPELINE] info log and increments rejection counter.
        """
        with self._lock:
            key = reason or f"UNKNOWN_{stage}"
            self._rejected[key] = self._rejected.get(key, 0) + 1
        log.info(
            "[SIGNAL PIPELINE] stage=%-18s status=BLOCK  reason=%s  symbol=%s",
            stage, reason, symbol,
        )

    def count_executed(self) -> None:
        """Call immediately after a trade is successfully placed in MT5."""
        with self._lock:
            self._funnel[STAGE_EXECUTION] += 1
            self._total_executed          += 1

    # ── Rich rejection telemetry ──────────────────────────────────────────────

    def log_signal_rejected(self, ctx: SignalContext) -> None:
        """
        Emit [SIGNAL REJECTED] with all factor scores.

        Call for post-confluence rejections where full confluence data is available.
        Also emits [NEAR MISS] if the signal was within _NEAR_MISS_MARGIN of threshold.
        """
        _near_tag = " [NEAR MISS]" if ctx.near_threshold else ""
        log.warning(
            "[SIGNAL REJECTED] %s %s | reason=%s stage=%s%s | "
            "score=%.1f conf=%.0f core=%.1f quality=%s tier=%d | "
            "trend=%.1f ob=%.1f fvg=%.1f liq=%.1f fib=%.1f ema=%.1f bo=%.1f str=%.1f | "
            "atr=%.1f spread=%.1f session=%s utc=%02d market=%s | threshold=%.0f",
            ctx.symbol, ctx.direction, ctx.reason, ctx.stage, _near_tag,
            ctx.signal_score, ctx.confidence, ctx.core_score,
            ctx.quality_grade or "?", ctx.tier,
            ctx.trend_score, ctx.ob_score, ctx.fvg_score, ctx.liquidity_score,
            ctx.fib_score, ctx.ema_score, ctx.breakout_score, ctx.structure_score,
            ctx.atr_pips, ctx.spread_pips, ctx.session, ctx.utc_hour,
            ctx.market_state, ctx.threshold_used,
        )
        if ctx.near_threshold:
            log.warning(
                "[NEAR MISS] %s %s | score=%.1f vs threshold=%.0f (within %d pts) "
                "reason=%s quality=%s conf=%.0f",
                ctx.symbol, ctx.direction, ctx.signal_score, ctx.threshold_used,
                _NEAR_MISS_MARGIN, ctx.reason, ctx.quality_grade or "?", ctx.confidence,
            )
            with self._lock:
                self._near_miss_count += 1

    def log_near_miss(
        self,
        symbol:     str,
        direction:  str,
        score:      float,
        threshold:  float,
        reason:     str,
        confidence: float = 0.0,
        atr:        float = 0.0,
        session:    str   = "",
    ) -> None:
        """Emit [NEAR MISS] for pre-score-threshold proximity (standalone call)."""
        gap = threshold - score
        if 0 < gap <= _NEAR_MISS_MARGIN:
            log.warning(
                "[NEAR MISS] %s %s | score=%.1f vs threshold=%.0f gap=%.1f pts "
                "reason=%s conf=%.0f atr=%.1f session=%s",
                symbol, direction, score, threshold, gap,
                reason, confidence, atr, session,
            )
            with self._lock:
                self._near_miss_count += 1

    # ── Periodic reports ──────────────────────────────────────────────────────

    def log_funnel(self) -> None:
        """Emit [SIGNAL FUNNEL] with stage-by-stage counts and drop rates."""
        with self._lock:
            funnel   = dict(self._funnel)
            near     = self._near_miss_count
            executed = self._funnel[STAGE_EXECUTION]

        total = funnel[STAGE_SCAN] or 1
        rate  = executed / total * 100

        lines = [
            f"[SIGNAL FUNNEL] total_scans={total}  executed={executed}  "
            f"exec_rate={rate:.1f}%  near_misses={near}"
        ]
        prev = total
        for stage in PIPELINE_STAGES[1:]:
            n    = funnel.get(stage, 0)
            pct  = n / prev * 100 if prev else 0.0
            drop = prev - n
            lines.append(
                f"  {stage:<18} pass={n:>5}  through={pct:5.1f}%  dropped={drop}"
            )
            if n > 0:
                prev = n

        log.warning("\n".join(lines))

    def log_filter_analytics(self) -> None:
        """Emit [FILTER ANALYTICS] with rejection reason percentage breakdown."""
        with self._lock:
            rejected  = dict(self._rejected)
            near      = self._near_miss_count

        total_rej = sum(rejected.values()) or 1
        if not rejected:
            log.info("[FILTER ANALYTICS] No rejections recorded yet")
            return

        lines = [
            f"[FILTER ANALYTICS] total_rejected={total_rej}  near_misses={near}"
        ]
        for reason, count in sorted(rejected.items(), key=lambda x: -x[1]):
            pct = count / total_rej * 100
            lines.append(f"  {reason:<36} {count:>5}  ({pct:5.1f}%)")

        log.warning("\n".join(lines))

    # ── Stats export for Supabase ─────────────────────────────────────────────

    def get_funnel_stats(self) -> Dict[str, Any]:
        """Return in-memory telemetry stats for Supabase sync."""
        with self._lock:
            total    = self._funnel[STAGE_SCAN] or 1
            executed = self._funnel[STAGE_EXECUTION]
            return {
                "total_scanned":    self._total_scanned,
                "total_executed":   self._total_executed,
                "exec_rate":        round(executed / total, 4),
                "near_miss_count":  self._near_miss_count,
                "stage_confidence": self._funnel.get(STAGE_CONFIDENCE, 0),
                "stage_confluence": self._funnel.get(STAGE_CONFLUENCE, 0),
                "stage_execution":  self._funnel.get(STAGE_EXECUTION, 0),
                "top_rejections":   dict(
                    sorted(self._rejected.items(), key=lambda x: -x[1])[:5]
                ),
            }
