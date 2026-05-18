"""
Aurex AI — Performance Analytics Engine

Computes profitability metrics from the TradeLogger's persistent history.
Designed to run in-process after each trade close and once at startup.

Source separation (Phase 10):
  All compute paths now accept an optional `source_filter` parameter so
  metrics can be computed independently for AI_AUTO, MANUAL, and HYBRID trades.

  compute(trade_logger, source_filter=["AI_AUTO"])  → AI-only metrics
  compute(trade_logger, source_filter=["MANUAL"])   → Manual-only metrics
  compute_by_source(trade_logger)                   → {source: PerformanceReport}

  Use the [AI PERFORMANCE], [MANUAL PERFORMANCE], and [HYBRID PERFORMANCE]
  log tags to distinguish analytics blocks in the logs.

Metrics:
  win_rate          — wins / closed_trades
  avg_rr            — mean realised R:R of winning trades only (logged as avg_win_rr)
  profit_factor     — gross_profit / |gross_loss| (> 1.0 is profitable)
  max_drawdown_pct  — largest peak-to-trough equity drawdown as a %
  total_pnl_zar     — cumulative net profit/loss
  trades_per_day    — closed trades / active trading days
  best_session      — session with highest win rate (≥ 5 trades)
  best_direction    — BUY or SELL with higher win rate (≥ 5 trades)
  best_setup        — setup_type with highest win rate (≥ 5 trades)
  worst_session     — session with lowest win rate (≥ 5 trades)
  component_deltas  — dict of (component -> win_avg_score - loss_avg_score)
                      Used by WeightAdjuster to identify high-signal factors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from aurex_ai.core.logger import get_logger
from aurex_ai.core.trade_source import AI_AUTO, MANUAL, HYBRID, ALL_SOURCES
from aurex_ai.analytics.strategy_version import (
    CURRENT_VERSION, MIN_VALID_SAMPLE, SampleStatus,
)

log = get_logger("analytics.performance")

# ── Sessions ──────────────────────────────────────────────────────────────────
_SESSIONS = {
    "london":   range(7,  13),
    "overlap":  range(13, 16),
    "newyork":  range(16, 22),
    "asian":    range(0,  7),
}

_COMPONENT_KEYS = (
    "trend_score", "liquidity_score", "fvg_score", "fib_score",
    "ema_score", "confirmation_score", "structure_score",
    "ob_score", "breakout_score",
)


def _session_for_hour(hour: int) -> str:
    for name, rng in _SESSIONS.items():
        if hour in rng:
            return name
    return "asian"


@dataclass
class PerformanceReport:
    """Snapshot of system performance computed from trade history."""
    source:             str          = "ALL"     # AI_AUTO | MANUAL | HYBRID | ALL
    total_closed:       int          = 0
    total_open:         int          = 0
    win_rate:           float        = 0.0     # 0.0 – 1.0
    avg_rr:             float        = 0.0
    profit_factor:      float        = 0.0     # > 1.0 = profitable
    max_drawdown_pct:   float        = 0.0     # negative value
    total_pnl_zar:      float        = 0.0
    trades_per_day:     float        = 0.0
    best_session:       str          = ""
    worst_session:      str          = ""
    best_direction:     str          = ""
    best_setup:         str          = ""
    avg_duration_min:   float        = 0.0     # average trade duration in minutes
    tp_hit_rate:        float        = 0.0     # profitable closes / total closed
    session_pnl:        Dict[str, float] = field(default_factory=dict)
    symbol_win_rates:   Dict[str, float] = field(default_factory=dict)
    symbol_pnl:         Dict[str, float] = field(default_factory=dict)
    component_deltas:   Dict[str, float] = field(default_factory=dict)
    session_win_rates:  Dict[str, float] = field(default_factory=dict)
    expectancy:         float        = 0.0     # avg P&L per trade

    def _source_tag(self) -> str:
        tag_map = {
            AI_AUTO: "[AI PERFORMANCE]",
            MANUAL:  "[MANUAL PERFORMANCE]",
            HYBRID:  "[HYBRID PERFORMANCE]",
        }
        return tag_map.get(self.source, "[PERFORMANCE ANALYTICS]")

    def to_log_str(self) -> str:
        tag = self._source_tag()
        lines = [
            f"{tag} source={self.source} trades={self.total_closed} "
            f"wr={self.win_rate:.1%} tp_hit={self.tp_hit_rate:.1%} "
            f"avg_win_rr={self.avg_rr:.2f} pf={self.profit_factor:.2f} "
            f"pnl={self.total_pnl_zar:.2f} dd={self.max_drawdown_pct:.1f}% "
            f"expectancy={self.expectancy:.2f} avg_dur={self.avg_duration_min:.0f}min",
            f"  [STRATEGY METRICS] best_session={self.best_session or 'n/a'} "
            f"worst={self.worst_session or 'n/a'} "
            f"best_dir={self.best_direction or 'n/a'} "
            f"best_setup={self.best_setup or 'n/a'}",
        ]
        if self.session_pnl:
            pnl_str = "  ".join(f"{s}={v:+.1f}" for s, v in self.session_pnl.items())
            wr_str  = "  ".join(
                f"{s}={v:.1%}" for s, v in self.session_win_rates.items()
            )
            lines.append(f"  [SESSION PERFORMANCE] pnl: {pnl_str}")
            lines.append(f"  [SESSION PERFORMANCE] wr:  {wr_str}")
        if self.symbol_pnl:
            sym_str = "  ".join(
                f"{s}={v:+.1f}({self.symbol_win_rates.get(s, 0):.0%})"
                for s, v in self.symbol_pnl.items()
            )
            lines.append(f"  [STRATEGY METRICS] symbols: {sym_str}")
        if self.component_deltas:
            top3  = sorted(self.component_deltas.items(), key=lambda x: x[1], reverse=True)[:3]
            worst = sorted(self.component_deltas.items(), key=lambda x: x[1])[:1]
            lines.append(
                f"  [STRATEGY METRICS] top_factors={[k for k, _ in top3]} "
                f"weakest={[k for k, _ in worst]}"
            )
        return "\n".join(lines)


class PerformanceEngine:
    """
    Stateless analytics engine.  Call `compute()` to get a fresh report.

    Usage:
        from aurex_ai.analytics.trade_logger import TradeLogger
        from aurex_ai.analytics.performance  import PerformanceEngine

        # AI-only analytics (learning-clean):
        ai_report = PerformanceEngine.compute(
            TradeLogger.get_instance(), source_filter=[AI_AUTO]
        )
        log.warning(ai_report.to_log_str())

        # Full source breakdown:
        by_source = PerformanceEngine.compute_by_source(TradeLogger.get_instance())
    """

    @staticmethod
    def compute(
        trade_logger,
        window:           Optional[int]       = None,
        source_filter:    Optional[List[str]] = None,
        version_filter:   Optional[str]       = None,
    ) -> PerformanceReport:
        """
        Compute analytics from the trade log.

        Args:
            trade_logger:   TradeLogger instance.
            window:         Most recent N closed trades (None = all).
            source_filter:  Filter by trade_source list. e.g. ["AI_AUTO"].
            version_filter: Filter by strategy_version. e.g. "AI_FORWARD_V2".
                            When set, only trades with this version are included.
                            Use this to get clean Phase 11+ metrics only.

        Returns:
            PerformanceReport with all metrics filled in.
        """
        source_label = source_filter[0] if (source_filter and len(source_filter) == 1) else "ALL"

        if version_filter:
            all_closed = trade_logger.get_closed_trades_by_version(
                version=version_filter,
                sources=source_filter,
                limit=window or 2000,
            )
        elif source_filter:
            all_closed = trade_logger.get_closed_trades_by_source(
                sources=source_filter, limit=window or 2000
            )
        else:
            all_closed = trade_logger.get_closed_trades(limit=window or 2000)

        open_trades = trade_logger.get_open_trades()

        if not all_closed:
            return PerformanceReport(source=source_label, total_open=len(open_trades))

        report = PerformanceReport(
            source       = source_label,
            total_closed = len(all_closed),
            total_open   = len(open_trades),
        )

        # ── Core P&L metrics ──────────────────────────────────────────────────
        wins   = [t for t in all_closed if t.get("result") == "WIN"]
        losses = [t for t in all_closed if t.get("result") in ("LOSS", "BREAKEVEN")]

        report.win_rate      = len(wins) / len(all_closed)
        gross_profit         = sum(t.get("profit_zar", 0.0) for t in wins)
        gross_loss_abs       = abs(sum(t.get("profit_zar", 0.0) for t in losses))
        report.total_pnl_zar = gross_profit - gross_loss_abs
        report.profit_factor = (
            round(gross_profit / gross_loss_abs, 2) if gross_loss_abs > 0 else
            round(gross_profit, 2) if gross_profit > 0 else 0.0
        )

        # Expectancy: average P&L per trade (positive = edge)
        report.expectancy = round(
            report.total_pnl_zar / len(all_closed) if all_closed else 0.0, 2
        )

        # avg_rr: winning trades only
        rr_vals = [t.get("risk_reward", 0.0) for t in wins if t.get("risk_reward", 0.0) > 0]
        report.avg_rr = round(sum(rr_vals) / len(rr_vals), 2) if rr_vals else 0.0

        # ── Max drawdown (equity curve) ───────────────────────────────────────
        sorted_by_time = sorted(all_closed, key=lambda t: t.get("opened_at", ""))
        equity  = 0.0
        peak    = 0.0
        max_dd  = 0.0
        for t in sorted_by_time:
            equity += t.get("profit_zar", 0.0)
            if equity > peak:
                peak = equity
            dd = equity - peak
            if dd < max_dd:
                max_dd = dd
        # When peak == 0 (no profitable trades yet), drawdown % is undefined.
        # Return 0.0 rather than -100.0 to avoid alarming dashboards.
        report.max_drawdown_pct = round(
            max(max_dd / peak * 100.0, -100.0) if peak > 0 else 0.0, 2
        )

        # ── Trades per day ────────────────────────────────────────────────────
        dates = set()
        for t in all_closed:
            ts = t.get("opened_at", "")
            if ts:
                dates.add(ts[:10])
        report.trades_per_day = round(len(all_closed) / max(len(dates), 1), 1)

        # ── Best/worst session ────────────────────────────────────────────────
        session_wins: Dict[str, int]    = {}
        session_total: Dict[str, int]   = {}
        for t in all_closed:
            s = _session_for_hour(t.get("utc_hour", 12))
            session_total[s] = session_total.get(s, 0) + 1
            if t.get("result") == "WIN":
                session_wins[s] = session_wins.get(s, 0) + 1

        session_wr: Dict[str, float] = {}
        for s, total in session_total.items():
            if total >= 5:
                session_wr[s] = session_wins.get(s, 0) / total

        report.session_win_rates = {s: round(v, 3) for s, v in session_wr.items()}
        if session_wr:
            report.best_session  = max(session_wr, key=session_wr.get)   # type: ignore[arg-type]
            report.worst_session = min(session_wr, key=session_wr.get)   # type: ignore[arg-type]

        # ── Best direction ────────────────────────────────────────────────────
        dir_wins:  Dict[str, int] = {}
        dir_total: Dict[str, int] = {}
        for t in all_closed:
            d = t.get("direction", "BUY")
            dir_total[d] = dir_total.get(d, 0) + 1
            if t.get("result") == "WIN":
                dir_wins[d] = dir_wins.get(d, 0) + 1
        dir_wr = {d: dir_wins.get(d, 0) / n for d, n in dir_total.items() if n >= 5}
        if dir_wr:
            report.best_direction = max(dir_wr, key=dir_wr.get)  # type: ignore[arg-type]

        # ── Best setup ────────────────────────────────────────────────────────
        setup_wins:  Dict[str, int] = {}
        setup_total: Dict[str, int] = {}
        for t in all_closed:
            st = t.get("setup_type", "")
            if not st or st == "MANUAL":
                continue
            setup_total[st] = setup_total.get(st, 0) + 1
            if t.get("result") == "WIN":
                setup_wins[st] = setup_wins.get(st, 0) + 1
        setup_wr = {s: setup_wins.get(s, 0) / n for s, n in setup_total.items() if n >= 5}
        if setup_wr:
            report.best_setup = max(setup_wr, key=setup_wr.get)  # type: ignore[arg-type]

        # ── Average trade duration ────────────────────────────────────────────
        durations = [
            t.get("duration_minutes", 0)
            for t in all_closed
            if t.get("duration_minutes", 0) > 0
        ]
        report.avg_duration_min = round(sum(durations) / len(durations), 1) if durations else 0.0

        # ── TP hit rate ───────────────────────────────────────────────────────
        tp_hits = sum(1 for t in all_closed if t.get("profit_zar", 0.0) > 0.5)
        report.tp_hit_rate = round(tp_hits / len(all_closed), 3) if all_closed else 0.0

        # ── Session P&L breakdown ─────────────────────────────────────────────
        session_pnl: Dict[str, float] = {}
        for t in all_closed:
            s   = _session_for_hour(t.get("utc_hour", 12))
            pnl = t.get("profit_zar", 0.0)
            session_pnl[s] = round(session_pnl.get(s, 0.0) + pnl, 2)
        report.session_pnl = session_pnl

        # ── Per-symbol win rate and P&L ───────────────────────────────────────
        sym_wins:  Dict[str, int]   = {}
        sym_total: Dict[str, int]   = {}
        sym_pnl:   Dict[str, float] = {}
        for t in all_closed:
            s   = t.get("symbol", "")
            if not s:
                continue
            sym_total[s] = sym_total.get(s, 0) + 1
            sym_pnl[s]   = round(sym_pnl.get(s, 0.0) + t.get("profit_zar", 0.0), 2)
            if t.get("result") == "WIN":
                sym_wins[s] = sym_wins.get(s, 0) + 1
        report.symbol_win_rates = {
            s: round(sym_wins.get(s, 0) / n, 3)
            for s, n in sym_total.items() if n >= 3
        }
        report.symbol_pnl = sym_pnl

        # ── Component score deltas (win vs loss) ──────────────────────────────
        report.component_deltas = _compute_component_deltas(wins, losses)

        return report

    @staticmethod
    def compute_by_source(
        trade_logger,
        window: Optional[int] = None,
    ) -> Dict[str, PerformanceReport]:
        """
        Compute separate PerformanceReport for every trade source with data.

        Returns a dict keyed by source string: {"AI_AUTO": ..., "MANUAL": ..., ...}
        Only includes sources that have at least one closed trade.

        Used for dashboard source comparison and the startup [AI PERFORMANCE] banner.
        """
        results: Dict[str, PerformanceReport] = {}
        for source in (AI_AUTO, MANUAL, HYBRID):
            n = trade_logger.total_closed(sources=[source])
            if n == 0:
                continue
            results[source] = PerformanceEngine.compute(
                trade_logger, window=window, source_filter=[source]
            )
        return results

    @staticmethod
    def compute_ai_forward(
        trade_logger,
        window: Optional[int] = None,
    ) -> PerformanceReport:
        """
        Compute AI_FORWARD_V2-only performance metrics.

        This is the PRIMARY analytics call for the Phase 11 clean baseline.
        Only includes AI_AUTO + HYBRID trades tagged strategy_version=AI_FORWARD_V2.
        Historical LEGACY trades are completely excluded.

        Returns a PerformanceReport with source="AI_AUTO" reflecting only
        post-Phase-11 forward-test data.
        """
        return PerformanceEngine.compute(
            trade_logger,
            window         = window,
            source_filter  = [AI_AUTO, HYBRID],
            version_filter = CURRENT_VERSION,
        )

    @staticmethod
    def get_sample_status(trade_logger) -> SampleStatus:
        """
        Return the statistical validity state for the AI_FORWARD_V2 dataset.

        Reads the count of AI_AUTO + HYBRID closed trades in the current version
        and returns a SampleStatus reflecting how close we are to MIN_VALID_SAMPLE.
        Call this once per cycle to emit [AI SAMPLE STATUS].
        """
        n = trade_logger.total_version_closed(
            version=CURRENT_VERSION,
            sources=[AI_AUTO, HYBRID],
        )
        return SampleStatus.from_n(n)

    @staticmethod
    def log_source_comparison(trade_logger, window: Optional[int] = None) -> None:
        """
        Emit a full per-source performance breakdown to the log.

        Emits [AI PERFORMANCE], [MANUAL PERFORMANCE], [HYBRID PERFORMANCE] blocks
        followed by a combined summary.  Call once per day or on-demand.
        """
        by_source = PerformanceEngine.compute_by_source(trade_logger, window=window)
        if not by_source:
            log.info("[PERFORMANCE] No closed trades to report")
            return

        for source, report in by_source.items():
            log.warning(report.to_log_str())

        # Combined summary line
        all_report = PerformanceEngine.compute(trade_logger, window=window)
        log.warning(
            "[PERFORMANCE SUMMARY] ALL SOURCES | trades=%d wr=%.1f%% pf=%.2f pnl=%.2f dd=%.1f%%",
            all_report.total_closed, all_report.win_rate,
            all_report.profit_factor, all_report.total_pnl_zar,
            all_report.max_drawdown_pct,
        )


def _compute_component_deltas(
    wins:   List[Dict],
    losses: List[Dict],
) -> Dict[str, float]:
    """
    For each scoring component, return the difference between its average score
    in winning trades and its average score in losing/breakeven trades.

    Returns a dict: { "trend_score": +3.2, "fvg_score": -0.5, ... }
    """
    deltas: Dict[str, float] = {}

    for key in _COMPONENT_KEYS:
        win_scores  = [t.get(key, 0.0) for t in wins   if t.get(key) is not None]
        loss_scores = [t.get(key, 0.0) for t in losses if t.get(key) is not None]
        if not win_scores and not loss_scores:
            continue
        win_avg  = sum(win_scores)  / max(len(win_scores),  1)
        loss_avg = sum(loss_scores) / max(len(loss_scores), 1)
        delta = round(win_avg - loss_avg, 3)
        factor = key.replace("_score", "").replace("liquidity", "liquidity")
        if factor == "confirmation":
            factor = "confirmation"
        deltas[factor] = delta

    return deltas
