from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import BaseModel


class EquityPoint(BaseModel):
    date: date
    balance: float
    equity: float
    net_pnl: float


class PerformanceSummary(BaseModel):
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: Optional[float]
    net_pnl: float
    gross_profit: float
    gross_loss: float
    max_drawdown: float
    avg_rr: Optional[float]
    best_trade: Optional[float]
    worst_trade: Optional[float]
    avg_win: Optional[float]
    avg_loss: Optional[float]


class SymbolBreakdown(BaseModel):
    symbol: str
    trades: int
    wins: int
    net_pnl: float
    win_rate: float


class AnalyticsDashboard(BaseModel):
    summary: PerformanceSummary
    equity_curve: List[EquityPoint]
    symbol_breakdown: List[SymbolBreakdown]
    monthly_pnl: List[dict]
