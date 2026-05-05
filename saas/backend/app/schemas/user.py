from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr

from app.models.user import UserRole


class UserOut(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    is_verified: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = None


class UserAdminUpdate(UserUpdate):
    role: Optional[UserRole] = None
    is_verified: Optional[bool] = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class UserListOut(BaseModel):
    users: list[UserOut]
    total: int
    page: int
    per_page: int
