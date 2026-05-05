"""
Aurex AI SaaS — Trade Routes
GET /trades          — list user's trades (paginated, filtered)
GET /trades/{id}     — single trade detail
GET /trades/open     — currently open positions
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.trade import Trade, TradeDirection, TradeStatus
from app.models.user import User
from app.schemas.trade import TradeListOut, TradeOut

router = APIRouter()


@router.get("", response_model=TradeListOut)
async def list_trades(
    symbol: Optional[str] = Query(None),
    status: Optional[TradeStatus] = Query(None),
    direction: Optional[TradeDirection] = Query(None),
    days: Optional[int] = Query(None, ge=1, le=3650, description="Limit to trades opened in last N days"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from datetime import timedelta, date
    query = select(Trade).where(Trade.user_id == current_user.id)
    count_query = select(func.count()).select_from(Trade).where(Trade.user_id == current_user.id)

    if symbol:
        query = query.where(Trade.symbol == symbol.upper())
        count_query = count_query.where(Trade.symbol == symbol.upper())
    if status:
        query = query.where(Trade.status == status)
        count_query = count_query.where(Trade.status == status)
    if direction:
        query = query.where(Trade.direction == direction)
        count_query = count_query.where(Trade.direction == direction)
    if days:
        since = date.today() - timedelta(days=days)
        query = query.where(Trade.opened_at >= since)
        count_query = count_query.where(Trade.opened_at >= since)

    total = (await db.execute(count_query)).scalar_one()
    offset = (page - 1) * per_page
    result = await db.execute(
        query.order_by(Trade.opened_at.desc()).offset(offset).limit(per_page)
    )
    trades = result.scalars().all()

    return TradeListOut(trades=list(trades), total=total, page=page, per_page=per_page)


@router.get("/open", response_model=list[TradeOut])
async def list_open_trades(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Trade).where(
            Trade.user_id == current_user.id,
            Trade.status == TradeStatus.OPEN,
        ).order_by(Trade.opened_at.desc())
    )
    return result.scalars().all()


@router.get("/{trade_id}", response_model=TradeOut)
async def get_trade(
    trade_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Trade).where(Trade.id == trade_id, Trade.user_id == current_user.id)
    )
    trade = result.scalar_one_or_none()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    return trade
