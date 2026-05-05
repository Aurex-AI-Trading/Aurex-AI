from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.models.trade import TradeDirection, TradeStatus


class TradeOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    mt5_ticket: Optional[int]
    symbol: str
    direction: TradeDirection
    lot_size: float
    entry_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    close_price: Optional[float]
    score: Optional[float]
    risk_amount: Optional[float]
    rr_ratio: Optional[float]
    pnl: Optional[float]
    status: TradeStatus
    is_dry_run: bool
    opened_at: datetime
    closed_at: Optional[datetime]

    model_config = {"from_attributes": True}


class TradeListOut(BaseModel):
    trades: list[TradeOut]
    total: int
    page: int
    per_page: int


class TradeFilters(BaseModel):
    symbol: Optional[str] = None
    status: Optional[TradeStatus] = None
    direction: Optional[TradeDirection] = None
    page: int = 1
    per_page: int = 20
