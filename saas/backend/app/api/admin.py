"""
Aurex AI SaaS — Admin Routes (require ADMIN role)
GET  /admin/users              — list all users
GET  /admin/users/{id}
PUT  /admin/users/{id}
DELETE /admin/users/{id}
GET  /admin/trades             — all trades, all users
GET  /admin/analytics          — platform-wide analytics
GET  /admin/mt5/sessions       — all active MT5 sessions
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_admin
from app.models.trade import Trade
from app.models.user import User
from app.schemas.trade import TradeListOut
from app.schemas.user import UserAdminUpdate, UserListOut, UserOut
from app.services.mt5_bridge_service import MT5BridgeService

router = APIRouter()


@router.get("/users", response_model=UserListOut)
async def list_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    query = select(User)
    count_q = select(func.count()).select_from(User)

    if search:
        like = f"%{search}%"
        from sqlalchemy import or_
        query = query.where(or_(User.email.ilike(like), User.full_name.ilike(like)))
        count_q = count_q.where(or_(User.email.ilike(like), User.full_name.ilike(like)))

    total = (await db.execute(count_q)).scalar_one()
    offset = (page - 1) * per_page
    result = await db.execute(
        query.order_by(User.created_at.desc()).offset(offset).limit(per_page)
    )
    users = result.scalars().all()
    return UserListOut(users=list(users), total=total, page=page, per_page=per_page)


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.put("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    payload: UserAdminUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(user, field, value)
    await db.flush()
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.delete(user)


@router.get("/trades", response_model=TradeListOut)
async def list_all_trades(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    total = (await db.execute(select(func.count()).select_from(Trade))).scalar_one()
    offset = (page - 1) * per_page
    result = await db.execute(
        select(Trade).order_by(Trade.opened_at.desc()).offset(offset).limit(per_page)
    )
    trades = result.scalars().all()
    return TradeListOut(trades=list(trades), total=total, page=page, per_page=per_page)


@router.get("/analytics")
async def platform_analytics(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    total_users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    active_users = (await db.execute(
        select(func.count()).select_from(User).where(User.is_active == True)
    )).scalar_one()
    total_trades = (await db.execute(select(func.count()).select_from(Trade))).scalar_one()

    from app.models.trade import TradeStatus
    open_trades = (await db.execute(
        select(func.count()).select_from(Trade).where(Trade.status == TradeStatus.OPEN)
    )).scalar_one()

    return {
        "total_users": total_users,
        "active_users": active_users,
        "total_trades": total_trades,
        "open_trades": open_trades,
    }


@router.get("/mt5/sessions")
async def all_mt5_sessions(_: User = Depends(require_admin)):
    svc = MT5BridgeService.get_instance()
    return await svc.get_all_statuses()
