from __future__ import annotations

import math
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.dependencies import get_db, require_role
from app.models.user import User, UserRole
from app.schemas.admin import UserListParams, UserStatusUpdate

router = APIRouter(tags=["admin-users"])

admin_required = require_role("admin")


@router.get("/")
async def list_users(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    role: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """List all users with filters."""
    query = select(User)

    if role:
        try:
            query = query.where(User.role == UserRole(role))
        except ValueError:
            pass
    if is_active is not None:
        query = query.where(User.is_active == is_active)

    users = db.exec(query).all()

    if search:
        users = [
            u for u in users
            if search.lower() in u.full_name.lower() or search.lower() in u.email.lower()
        ]

    total = len(users)
    offset = (page - 1) * size
    paginated = users[offset: offset + size]

    return {
        "items": [
            {
                "id": str(u.id),
                "email": u.email,
                "full_name": u.full_name,
                "role": u.role.value,
                "is_active": u.is_active,
                "is_verified": u.is_verified,
                "wilaya": u.wilaya,
                "created_at": u.created_at.isoformat(),
            }
            for u in paginated
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.get("/{user_id}")
async def get_user(
    user_id: UUID,
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Get a specific user."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role.value,
        "is_active": user.is_active,
        "is_verified": user.is_verified,
        "wilaya": user.wilaya,
        "phone": user.phone,
        "bio": user.bio,
        "avatar_url": user.avatar_url,
        "onboarding_completed": user.onboarding_completed,
        "created_at": user.created_at.isoformat(),
        "updated_at": user.updated_at.isoformat(),
    }


@router.put("/{user_id}/status")
async def update_user_status(
    user_id: UUID,
    payload: UserStatusUpdate,
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Activate or deactivate a user."""
    from datetime import datetime

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = payload.is_active
    user.updated_at = datetime.utcnow()
    db.add(user)
    db.commit()

    action = "activated" if payload.is_active else "deactivated"
    return {"message": f"User {action} successfully", "user_id": str(user_id)}


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: UUID,
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Delete a user account."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    db.delete(user)
    db.commit()
    return None


@router.post("/{user_id}/invite")
async def invite_user(
    user_id: UUID,
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Send an invitation email to a user."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    from app.workers.email_tasks import send_welcome_email
    send_welcome_email.delay(user.email, user.full_name)

    return {"message": f"Invitation sent to {user.email}"}
