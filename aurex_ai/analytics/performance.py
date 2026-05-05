"""
Aurex AI — Performance Analytics Engine

Computes profitability metrics from the TradeLogger's persistent history.
Designed to run in-process after each trade close and once at startup.

Metrics:
  win_rate          — wins / closed_trades
  avg_rr            — mean realised R:R of winning trades only (logged as avg_win_rr)
  profit_factor     — gross_profit / |gross_loss| (> 1.0 is profitable)
  max_drawdown_pct  — largest peak-to-trough equity drawdown as a %
  total_pnl_usd     — cumulative net profit/loss
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
    total_closed:      int          = 0
    total_open:        int          = 0
    win_rate:          float        = 0.0     # 0.0 – 1.0
    avg_rr:            float        = 0.0
    profit_factor:     float        = 0.0     # > 1.0 = profitable
    max_drawdown_pct:  float        = 0.0     # negative value
    total_pnl_usd:     float        = 0.0
    trades_per_day:    float        = 0.0
    best_session:      str          = ""
    worst_session:     str          = ""
    best_direction:    str          = ""
    best_setup:        str          = ""
    # component_deltas: factor -> (win_avg_score - loss_avg_score)
    # Positive = this factor scores higher in winning trades
    component_deltas:  Dict[str, float] = field(default_factory=dict)
    # per-session win rate for logging
    session_win_rates: Dict[str, float] = field(default_factory=dict)

    def to_log_str(self) -> str:
        lines = [
            f"[PERFORMANCE] trades={self.total_closed} wr={self.win_rate:.1%} "
            f"avg_win_rr={self.avg_rr:.2f} pf={self.profit_factor:.2f} "
            f"pnl={self.total_pnl_usd:.2f} dd={self.max_drawdown_pct:.1f}%",
            f"             best_session={self.best_session or 'n/a'} "
            f"best_dir={self.best_direction or 'n/a'} "
            f"best_setup={self.best_setup or 'n/a'}",
        ]
        if self.component_deltas:
            top3 = sorted(self.component_deltas.items(), key=lambda x: x[1], reverse=True)[:3]
            worst = sorted(self.component_deltas.items(), key=lambda x: x[1])[:1]
            lines.append(
                f"             top_factors={[k for k,_ in top3]} "
                f"weakest={[k for k,_ in worst]}"
            )
        return "\n".join(lines)


class PerformanceEngine:
    """
    Stateless analytics engine.  Call `compute()` to get a fresh report.

    Usage:
        from aurex_ai.analytics.trade_logger import TradeLogger
        from aurex_ai.analytics.performance  import PerformanceEngine

        report = PerformanceEngine.compute(TradeLogger.get_instance())
        log.warning(report.to_log_str())
    """

    @staticmethod
    def compute(
        trade_logger,
        window: Optional[int] = None,
    ) -> PerformanceReport:
        """
        Compute analytics from the trade log.

        Args:
            trade_logger: TradeLogger instance.
            window:       If set, only consider the most recent N closed trades.
                          None = use all closed trades.

        Returns:
            PerformanceReport with all metrics filled in.
        """
        all_closed = trade_logger.get_closed_trades(limit=window or 2000)
        open_trades = trade_logger.get_open_trades()

        if not all_closed:
            return PerformanceReport(total_open=len(open_trades))

        report = PerformanceReport(
            total_closed = len(all_closed),
            total_open   = len(open_trades),
        )

        # ── Core P&L metrics ──────────────────────────────────────────────────
        wins   = [t for t in all_closed if t.get("result") == "WIN"]
        losses = [t for t in all_closed if t.get("result") in ("LOSS", "BREAKEVEN")]

        report.win_rate      = len(wins) / len(all_closed)
        gross_profit         = sum(t.get("profit_usd", 0.0) for t in wins)
        gross_loss_abs       = abs(sum(t.get("profit_usd", 0.0) for t in losses))
        report.total_pnl_usd = gross_profit - gross_loss_abs
        report.profit_factor = (
            round(gross_profit / gross_loss_abs, 2) if gross_loss_abs > 0 else
            round(gross_profit, 2) if gross_profit > 0 else 0.0
        )

        # avg_rr: winning trades only (losses have no meaningful realised R:R stored)
        rr_vals = [t.get("risk_reward", 0.0) for t in wins if t.get("risk_reward", 0.0) > 0]
        report.avg_rr = round(sum(rr_vals) / len(rr_vals), 2) if rr_vals else 0.0

        # ── Max drawdown (equity curve) ───────────────────────────────────────
        # Build equity curve from opened_at-sorted trades.
        sorted_by_time = sorted(all_closed, key=lambda t: t.get("opened_at", ""))
        equity  = 0.0
        peak    = 0.0
        max_dd  = 0.0
        for t in sorted_by_time:
            equity += t.get("profit_usd", 0.0)
            if equity > peak:
                peak = equity
            dd = equity - peak
            if dd < max_dd:
                max_dd = dd
        # When peak > 0: standard peak-to-trough %.
        # When peak <= 0 (no profitable trades): every dollar lost is pure drawdown;
        # report -100.0 to expose the all-loss case rather than hiding it as 0%.
        report.max_drawdown_pct = round(
            (max_dd / peak * 100.0) if peak > 0 else (-100.0 if max_dd < 0 else 0.0), 2
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
            report.best_session  = max(session_wr, key=session_wr.get)  # type: ignore[arg-type]
            report.worst_session = min(session_wr, key=session_wr.get)  # type: ignore[arg-type]

        # ── Best direction ────────────────────────────────────────────────────
        dir_wins:  Dict[str, int] = {}
        dir_total: Dict[str, int] = {}
        for t in all_closed:
            d = t.get("direction", "BUY")
            dir_total[d] = dir_total.get(d, 0) + 1
            if t.get("result") == "WIN":
                dir_wins[d] = dir_wins.get(d, 0) + 1
        dir_wr = {
            d: dir_wins.get(d, 0) / n
            for d, n in dir_total.items() if n >= 5
        }
        if dir_wr:
            report.best_direction = max(dir_wr, key=dir_wr.get)  # type: ignore[arg-type]

        # ── Best setup ────────────────────────────────────────────────────────
        setup_wins:  Dict[str, int] = {}
        setup_total: Dict[str, int] = {}
        for t in all_closed:
            st = t.get("setup_type", "")
            if not st:
                continue
            setup_total[st] = setup_total.get(st, 0) + 1
            if t.get("result") == "WIN":
                setup_wins[st] = setup_wins.get(st, 0) + 1
        setup_wr = {
            s: setup_wins.get(s, 0) / n
            for s, n in setup_total.items() if n >= 5
        }
        if setup_wr:
            report.best_setup = max(setup_wr, key=setup_wr.get)  # type: ignore[arg-type]

        # ── Component score deltas (win vs loss) ──────────────────────────────
        # Positive delta = factor reliably predicts wins.
        # Used by WeightAdjuster to nudge weights toward high-signal factors.
        report.component_deltas = _compute_component_deltas(wins, losses)

        return report


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
        # Normalise by the factor's max scale so all deltas are comparable
        delta = round(win_avg - loss_avg, 3)
        # Map DB column names to weight adjuster factor keys
        factor = key.replace("_score", "").replace("liquidity", "liquidity")
        if factor == "confirmation":
            factor = "confirmation"
        deltas[factor] = delta

    return deltas
