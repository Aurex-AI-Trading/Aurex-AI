from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TradeStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    PENDING = "pending"


class TradeDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    mt5_ticket: Mapped[int] = mapped_column(BigInteger, nullable=True, index=True)
    signal_id: Mapped[str] = mapped_column(String(100), nullable=True)

    # Trade details
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[TradeDirection] = mapped_column(String(10), nullable=False)
    lot_size: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float] = mapped_column(Float, nullable=True)
    close_price: Mapped[float] = mapped_column(Float, nullable=True)

    # Risk / scoring
    score: Mapped[float] = mapped_column(Float, nullable=True)
    risk_amount: Mapped[float] = mapped_column(Float, nullable=True)
    rr_ratio: Mapped[float] = mapped_column(Float, nullable=True)
    sl_pips: Mapped[float] = mapped_column(Float, nullable=True)

    # P&L
    pnl: Mapped[float] = mapped_column(Float, nullable=True)
    commission: Mapped[float] = mapped_column(Float, nullable=True)
    swap: Mapped[float] = mapped_column(Float, nullable=True)

    # Status
    status: Mapped[TradeStatus] = mapped_column(
        String(20), default=TradeStatus.OPEN, nullable=False, index=True
    )
    is_dry_run: Mapped[bool] = mapped_column(default=False, nullable=False)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="trades")  # noqa: F821
