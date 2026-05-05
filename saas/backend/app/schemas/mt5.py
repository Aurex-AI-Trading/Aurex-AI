from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator

from app.models.mt5_account import MT5ConnectionStatus


class MT5ConnectRequest(BaseModel):
    account_number: int
    password: str
    server: str

    @field_validator("account_number")
    @classmethod
    def validate_account(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Account number must be positive")
        return v

    @field_validator("server")
    @classmethod
    def validate_server(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Server name is required")
        return v


class MT5AccountOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    account_number: int
    server: str
    broker_name: Optional[str]
    connection_status: MT5ConnectionStatus
    is_active: bool
    last_connected_at: Optional[datetime]
    last_error: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class MT5StatusOut(BaseModel):
    user_id: str
    account_number: Optional[int]
    server: Optional[str]
    connected: bool
    last_heartbeat: Optional[datetime]
    balance: Optional[float]
    equity: Optional[float]
    error: Optional[str]


class MT5DisconnectResponse(BaseModel):
    success: bool
    message: str
