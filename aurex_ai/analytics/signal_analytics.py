"""
Aurex AI — Signal Analytics Engine  (Phase 12)

Reads from SignalStore to compute and log:
  - Filter effectiveness (false-negatives / false-positives)
  - Confidence band statistics (near-threshold win rates)
  - Rejection reason breakdown (which gates cost us the most)
  - Setup type expectancy (which setups win most reliably)
  - Threshold calibration advisories (slow, statistically gated)

This module is READ-ONLY from SignalStore — it never writes to it.
All advisories are informational; no parameters are auto-adjusted here.

Logs emitted
------------
  [FILTER EFFECTIVENESS] — summary of executed vs rejected win rates
  [CONFIDENCE BAND]      — per-band win rate for threshold calibration
  [REJECTION ANALYSIS]   — which rejection reasons have highest false-neg rate
  [SETUP EXPECTANCY]     — per setup type win rate and avg RR
  [THRESHOLD ADVISORY]   — recommendation only (never auto-applied)
"""
from __future__ import annotations

from typing import Optional

from aurex_ai.core.logger import get_logger

log = get_logger("analytics.signal_analytics")

# Minimum sample sizes for each analytics type before reporting
_MIN_SIGNALS_TOTAL   = 10
_MIN_BAND_SAMPLE     = 5
_MIN_REASON_SAMPLE   = 5
_MIN_SETUP_SAMPLE    = 5


class SignalAnalytics:
    """
    Computes and logs institutional signal analytics.

    Usage:
        sa = SignalAnalytics(signal_store)
        sa.log_filter_effectiveness()
        sa.log_confidence_band_stats()
        sa.log_rejection_analysis()
        sa.log_setup_expectancy()
    """

    def __init__(self, signal_store) -> None:
        self._store = signal_store

    # ── Primary analytics ─────────────────────────────────────────────────────

    def log_filter_effectiveness(self, window: int = 200) -> None:
        """Log executed vs rejected win rates and false-negative / false-positive rates."""
        metrics = self._store.get_filter_effectiveness(window=window)
        if not metrics or metrics.get("total_signals", 0) < _MIN_SIGNALS_TOTAL:
            log.info("[SIGNAL ANALYTICS] Insufficient signal data (n<%d)", _MIN_SIGNALS_TOTAL)
            return

        log.warning(
            "[FILTER EFFECTIVENESS] [SIGNAL INTELLIGENCE] "
            "n=%d | executed=%d rejected=%d | "
            "exec_wr=%.1f%% fn_rate=%.1f%% fp_rate=%.1f%% | "
            "missed_opps=%d successful_rejections=%d",
            metrics["total_signals"],
            metrics["executed_count"],
            metrics["rejected_count"],
            metrics["exec_win_rate"]        * 100,
            metrics["false_negative_rate"]  * 100,
            metrics["false_positive_rate"]  * 100,
            metrics["missed_opportunities"],
            metrics["successful_rejections"],
        )

        # Top rejection reasons
        reasons = metrics.get("rejection_reasons", {})
        if reasons:
            for reason, count in sorted(reasons.items(), key=lambda x: -x[1])[:5]:
                log.info(
                    "[FILTER EFFECTIVENESS]   %-30s count=%d",
                    reason, count,
                )

        # Near-band advisory
        nb    = metrics.get("near_band_count", 0)
        nb_wr = metrics.get("near_band_win_rate", 0.0)
        if nb >= _MIN_BAND_SAMPLE:
            log.warning(
                "[CONFIDENCE BAND] Near-threshold signals | n=%d would-have-won=%.1f%%",
                nb, nb_wr * 100,
            )
            if nb_wr > 0.60:
                log.warning(
                    "[THRESHOLD ADVISORY] Near-band WR=%.1f%% exceeds 60%% — "
                    "statistical review recommended (require n≥30 before any change)",
                    nb_wr * 100,
                )

    def log_confidence_band_stats(self) -> None:
        """Log win rate per confidence band (BELOW / NEAR / ABOVE)."""
        bands = self._store.get_confidence_band_stats()
        if not bands:
            return
        any_reported = False
        for b in bands:
            if b["n"] >= _MIN_BAND_SAMPLE:
                log.warning(
                    "[CONFIDENCE BAND] %-6s | n=%d wr=%.1f%% avg_rr=%.2f",
                    b["band"], b["n"], b["win_rate"] * 100, b["avg_rr"],
                )
                any_reported = True
        if not any_reported:
            log.info("[CONFIDENCE BAND] Insufficient band data (need n≥%d per band)", _MIN_BAND_SAMPLE)

    def log_rejection_analysis(self) -> None:
        """Log false-negative rate per rejection reason — which filters cost us most."""
        breakdown = self._store.get_rejection_breakdown()
        if not breakdown:
            return
        reportable = {r: s for r, s in breakdown.items() if s["n"] >= _MIN_REASON_SAMPLE}
        if not reportable:
            log.info("[REJECTION ANALYSIS] Insufficient rejection data (need n≥%d per reason)", _MIN_REASON_SAMPLE)
            return
        log.warning("[REJECTION ANALYSIS] [SIGNAL INTELLIGENCE] False-negative rates by filter:")
        for reason, stats in sorted(reportable.items(), key=lambda x: -x[1]["fn_rate"]):
            log.warning(
                "[REJECTION ANALYSIS]   %-32s n=%d would_have_won=%d fn_rate=%.1f%%",
                reason, stats["n"], stats["would_have_won"], stats["fn_rate"] * 100,
            )

    def log_setup_expectancy(self) -> None:
        """Log win rate and avg RR per setup type."""
        setups = self._store.get_setup_expectancy()
        if not setups:
            return
        any_reported = False
        for s in setups:
            if s["n"] >= _MIN_SETUP_SAMPLE:
                log.warning(
                    "[SETUP EXPECTANCY] %-22s | n=%d wr=%.1f%% avg_rr=%.2f",
                    s["setup_type"], s["n"], s["win_rate"] * 100, s["avg_rr"],
                )
                any_reported = True
        if not any_reported:
            log.info("[SETUP EXPECTANCY] Insufficient setup data (need n≥%d per type)", _MIN_SETUP_SAMPLE)

    def log_full_report(self) -> None:
        """Log all analytics sections — called periodically from main loop."""
        self.log_filter_effectiveness()
        self.log_confidence_band_stats()
        self.log_rejection_analysis()
        self.log_setup_expectancy()
