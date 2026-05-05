"""
Aurex AI SaaS — Analytics Routes
GET /analytics/dashboard   — full analytics dashboard
GET /analytics/equity      — equity curve data points
GET /analytics/summary     — performance summary
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.analytics_snapshot import AnalyticsSnapshot
from app.models.trade import Trade, TradeStatus
from app.models.user import User
from app.schemas.analytics import (
    AnalyticsDashboard,
    EquityPoint,
    PerformanceSummary,
    SymbolBreakdown,
)

router = APIRouter()


@router.get("/dashboard", response_model=AnalyticsDashboard)
async def analytics_dashboard(
    days: int = Query(30, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    since = date.today() - timedelta(days=days)

    # Closed trades in period
    trades_result = await db.execute(
        select(Trade).where(
            Trade.user_id == current_user.id,
            Trade.status == TradeStatus.CLOSED,
            Trade.closed_at >= since,
        ).order_by(Trade.closed_at)
    )
    trades = trades_result.scalars().all()

    summary = _compute_summary(trades)
    equity_curve = await _build_equity_curve(db, current_user.id, since)
    symbol_breakdown = _compute_symbol_breakdown(trades)
    monthly_pnl = _compute_monthly_pnl(trades)

    return AnalyticsDashboard(
        summary=summary,
        equity_curve=equity_curve,
        symbol_breakdown=symbol_breakdown,
        monthly_pnl=monthly_pnl,
    )


@router.get("/equity", response_model=List[EquityPoint])
async def equity_curve(
    days: int = Query(30, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    since = date.today() - timedelta(days=days)
    return await _build_equity_curve(db, current_user.id, since)


@router.get("/summary", response_model=PerformanceSummary)
async def performance_summary(
    days: int = Query(30, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    since = date.today() - timedelta(days=days)
    result = await db.execute(
        select(Trade).where(
            Trade.user_id == current_user.id,
            Trade.status == TradeStatus.CLOSED,
            Trade.closed_at >= since,
        )
    )
    trades = result.scalars().all()
    return _compute_summary(trades)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_summary(trades) -> PerformanceSummary:
    closed = [t for t in trades if t.pnl is not None]
    if not closed:
        return PerformanceSummary(
            total_trades=0, wins=0, losses=0, win_rate=0.0,
            profit_factor=None, net_pnl=0.0, gross_profit=0.0,
            gross_loss=0.0, max_drawdown=0.0, avg_rr=None,
            best_trade=None, worst_trade=None, avg_win=None, avg_loss=None,
        )

    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    net_pnl = gross_profit - gross_loss
    win_rate = len(wins) / len(closed) * 100 if closed else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

    pnl_vals = [t.pnl for t in closed]
    avg_rr_vals = [t.rr_ratio for t in closed if t.rr_ratio]

    # Running max drawdown
    equity = 0.0; peak = 0.0; max_dd = 0.0
    for t in closed:
        equity += t.pnl
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    return PerformanceSummary(
        total_trades=len(closed),
        wins=len(wins),
        losses=len(losses),
        win_rate=round(win_rate, 1),
        profit_factor=round(profit_factor, 2) if profit_factor else None,
        net_pnl=round(net_pnl, 2),
        gross_profit=round(gross_profit, 2),
        gross_loss=round(gross_loss, 2),
        max_drawdown=round(max_dd, 2),
        avg_rr=round(sum(avg_rr_vals) / len(avg_rr_vals), 2) if avg_rr_vals else None,
        best_trade=round(max(pnl_vals), 2),
        worst_trade=round(min(pnl_vals), 2),
        avg_win=round(gross_profit / len(wins), 2) if wins else None,
        avg_loss=round(gross_loss / len(losses), 2) if losses else None,
    )


async def _build_equity_curve(db, user_id, since: date) -> List[EquityPoint]:
    result = await db.execute(
        select(AnalyticsSnapshot).where(
            AnalyticsSnapshot.user_id == user_id,
            AnalyticsSnapshot.snapshot_date >= since,
        ).order_by(AnalyticsSnapshot.snapshot_date)
    )
    snapshots = result.scalars().all()
    return [
        EquityPoint(
            date=s.snapshot_date,
            balance=s.balance,
            equity=s.equity,
            net_pnl=s.net_pnl,
        )
        for s in snapshots
    ]


def _compute_symbol_breakdown(trades) -> List[SymbolBreakdown]:
    symbols: dict = {}
    for t in trades:
        if t.pnl is None:
            continue
        s = symbols.setdefault(t.symbol, {"trades": 0, "wins": 0, "net_pnl": 0.0})
        s["trades"] += 1
        s["net_pnl"] += t.pnl
        if t.pnl > 0:
            s["wins"] += 1

    return [
        SymbolBreakdown(
            symbol=sym,
            trades=v["trades"],
            wins=v["wins"],
            net_pnl=round(v["net_pnl"], 2),
            win_rate=round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0.0,
        )
        for sym, v in sorted(symbols.items(), key=lambda x: x[1]["net_pnl"], reverse=True)
    ]


def _compute_monthly_pnl(trades) -> List[dict]:
    months: dict = {}
    for t in trades:
        if t.pnl is None or t.closed_at is None:
            continue
        key = t.closed_at.strftime("%Y-%m")
        months[key] = months.get(key, 0.0) + t.pnl

    return [
        {"month": k, "pnl": round(v, 2)}
        for k, v in sorted(months.items())
    ]


# ── AI Weights endpoint ───────────────────────────────────────────────────────

_DEFAULT_WEIGHTS = {
    "trend": 25, "liquidity": 20, "fvg": 20,
    "fibonacci": 15, "ema": 10, "confirmation": 10,
    "order_block": 15,
}


@router.get("/weights")
async def get_ai_weights(
    current_user: User = Depends(get_current_user),
):
    """Return the current AI confluence scoring weights from the bot's weight file."""
    import json
    from pathlib import Path

    weights_file = (
        Path(__file__).resolve().parents[5]
        / "aurex_ai" / "ai" / "data" / "weights.json"
    )
    if weights_file.exists():
        try:
            data = json.loads(weights_file.read_text(encoding="utf-8"))
            return {
                "weights":      data.get("weights", _DEFAULT_WEIGHTS),
                "last_updated": data.get("last_updated"),
            }
        except Exception:
            pass
    return {"weights": _DEFAULT_WEIGHTS, "last_updated": None}
