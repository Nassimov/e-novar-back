from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.notification import Notification, NotificationPreference
from app.models.profile import Profile
from app.schemas.notification import NotificationPrefsUpdate, NotificationResponse

router = APIRouter(tags=["Notifications"])


@router.get("/", response_model=Dict)
async def list_notifications(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    unread_only: bool = Query(False),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List notifications for the current user, newest first."""
    user_id = UUID(current_user["id"])

    query = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        query = query.where(Notification.read_at == None)  # noqa: E711
    query = query.order_by(Notification.created_at.desc())

    notifications = db.exec(query).all()
    total = len(notifications)
    offset = (page - 1) * size
    paginated = notifications[offset : offset + size]

    return {
        "items": [NotificationResponse.model_validate(n) for n in paginated],
        "total": total,
        "unread_count": sum(1 for n in notifications if n.read_at is None),
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.patch("/{notification_id}/read")
async def mark_notification_read(
    notification_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a single notification as read."""
    user_id = UUID(current_user["id"])
    notification = db.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notification.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    notification.read_at = datetime.now(timezone.utc)
    db.add(notification)
    db.commit()
    return {"message": "Marked as read"}


@router.patch("/read-all")
async def mark_all_notifications_read(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark all unread notifications as read."""
    user_id = UUID(current_user["id"])
    notifications = db.exec(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.read_at == None,  # noqa: E711
        )
    ).all()
    now = datetime.now(timezone.utc)
    for n in notifications:
        n.read_at = now
        db.add(n)
    db.commit()
    return {"message": f"Marked {len(notifications)} notifications as read"}


@router.get("/preferences")
async def get_notification_preferences(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the current user's notification preferences."""
    user_id = UUID(current_user["id"])
    prefs = db.get(NotificationPreference, user_id)
    if prefs is None:
        raise HTTPException(status_code=404, detail="Preferences not found")
    return prefs


@router.put("/preferences")
async def update_notification_preferences(
    payload: NotificationPrefsUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the current user's notification preferences."""
    user_id = UUID(current_user["id"])
    prefs = db.get(NotificationPreference, user_id)
    if prefs is None:
        prefs = NotificationPreference(user_id=user_id)
        db.add(prefs)

    for field, value in payload.model_dump().items():
        setattr(prefs, field, value)
    db.add(prefs)
    db.commit()
    db.refresh(prefs)
    return prefs
