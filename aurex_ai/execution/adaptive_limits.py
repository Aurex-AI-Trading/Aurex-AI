"""
Aurex AI — Adaptive Trade Limits

Computes the dynamic maximum daily trades based on session performance.
Pure function — no state stored here; all inputs come from _DailyState.

Rules (checked in priority order):
  daily_pnl_pct <= -max_daily_loss_pct  →  STOP  (0 trades — absolute halt)
  consecutive_losses >= 2               →  REDUCED (1 trade, capped at max_daily_trades)
  consecutive_wins   >= 2               →  SCALED  (4 trades, capped at max_daily_trades)
  otherwise                             →  NORMAL  (3 trades, capped at max_daily_trades)

max_daily_trades is always the hard ceiling — adaptive boosts cannot exceed it.
"""
from __future__ import annotations

from typing import Tuple

LIMIT_STOP    = 0   # drawdown exceeded
LIMIT_REDUCED = 1   # loss streak
LIMIT_NORMAL  = 3   # baseline
LIMIT_SCALED  = 4   # win streak


def get_adaptive_limit(
    consecutive_losses: int,
    consecutive_wins:   int,
    daily_pnl_pct:      float,
    max_daily_loss_pct: float = 2.0,
    max_daily_trades:   int   = 3,
) -> Tuple[int, str]:
    """
    Return (dynamic_max_daily_trades, mode_label).

    max_daily_trades is a hard ceiling: no adaptive mode can exceed it.

    mode_label values:
      "drawdown_stop"  — pnl breached max_daily_loss_pct; return 0
      "loss_reduction" — 2+ consecutive losses; return min(1, cap)
      "win_increase"   — 2+ consecutive wins; return min(4, cap)
      "normal"         — baseline; return min(3, cap)
    """
    cap = max(1, int(max_daily_trades))
    if daily_pnl_pct <= -abs(max_daily_loss_pct):
        return LIMIT_STOP, "drawdown_stop"
    if consecutive_losses >= 2:
        return min(LIMIT_REDUCED, cap), "loss_reduction"
    if consecutive_wins >= 2:
        return min(LIMIT_SCALED, cap), "win_increase"
    return min(LIMIT_NORMAL, cap), "normal"
