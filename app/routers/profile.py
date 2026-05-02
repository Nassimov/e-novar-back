from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.user import User
from app.schemas.user import (
    NotificationPrefsUpdate,
    PaymentMethodCreate,
    PaymentMethodResponse,
    ProfileUpdateRequest,
    UserResponse,
)
from app.services.storage import upload_file

router = APIRouter(tags=["profile"])


def _get_user_from_supabase_id(supabase_id: str, db: Session) -> User:
    statement = select(User).where(User.supabase_id == supabase_id)
    user = db.exec(statement).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/", response_model=UserResponse)
async def get_profile(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the current user's profile."""
    user = _get_user_from_supabase_id(current_user["id"], db)
    return UserResponse.model_validate(user)


@router.put("/", response_model=UserResponse)
async def update_profile(
    payload: ProfileUpdateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the current user's profile."""
    from datetime import datetime

    user = _get_user_from_supabase_id(current_user["id"], db)

    update_data = payload.model_dump(exclude_none=True)
    for field, value in update_data.items():
        setattr(user, field, value)
    user.updated_at = datetime.utcnow()

    db.add(user)
    db.commit()
    db.refresh(user)
    return UserResponse.model_validate(user)


@router.post("/avatar", response_model=UserResponse)
async def upload_avatar(
    file: UploadFile,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload and update user avatar."""
    from datetime import datetime

    if file.content_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:  # 5 MB limit
        raise HTTPException(status_code=400, detail="File too large (max 5MB)")

    url = upload_file(contents, file.filename or "avatar.jpg", file.content_type, folder="avatars")

    user = _get_user_from_supabase_id(current_user["id"], db)
    user.avatar_url = url
    user.updated_at = datetime.utcnow()
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserResponse.model_validate(user)


@router.put("/notifications")
async def update_notification_prefs(
    payload: NotificationPrefsUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update notification preferences."""
    from datetime import datetime

    user = _get_user_from_supabase_id(current_user["id"], db)
    user.notification_prefs = json.dumps(payload.model_dump())
    user.updated_at = datetime.utcnow()
    db.add(user)
    db.commit()
    return {"message": "Notification preferences updated", "prefs": payload.model_dump()}


@router.get("/payment-methods", response_model=List[PaymentMethodResponse])
async def list_payment_methods(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List saved payment methods for current user."""
    user = _get_user_from_supabase_id(current_user["id"], db)
    # Payment methods stored in Redis or a separate table in production
    # Returning empty list as placeholder for DB-backed implementation
    return []


@router.post("/payment-methods", response_model=PaymentMethodResponse, status_code=status.HTTP_201_CREATED)
async def add_payment_method(
    payload: PaymentMethodCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add a new payment method."""
    import uuid
    method_id = str(uuid.uuid4())
    return PaymentMethodResponse(
        id=method_id,
        type=payload.type,
        holder_name=payload.holder_name,
        card_last4=payload.card_last4,
        bank_name=payload.bank_name,
        is_default=False,
    )


@router.delete("/payment-methods/{method_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payment_method(
    method_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Remove a saved payment method."""
    return None
