"""
Aurex AI SaaS — User Self-Service Routes
GET  /users/me
PUT  /users/me
POST /users/me/change-password
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.security import get_password_hash, verify_password
from app.models.user import User
from app.schemas.user import PasswordChange, UserOut, UserUpdate

router = APIRouter()


@router.get("/me", response_model=UserOut)
async def get_profile(current_user: User = Depends(get_current_user)):
    return current_user


@router.put("/me", response_model=UserOut)
async def update_profile(
    payload: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(current_user, field, value)
    await db.flush()
    return current_user


@router.post("/me/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChange,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    from app.schemas.auth import RegisterRequest
    # Re-use password validation
    try:
        RegisterRequest(
            email=current_user.email,
            password=payload.new_password,
            full_name=current_user.full_name,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    current_user.hashed_password = get_password_hash(payload.new_password)
    await db.flush()
