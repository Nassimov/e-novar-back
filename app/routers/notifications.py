from __future__ import annotations

import json
import math
from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from app.core.redis import get_redis_client
from app.dependencies import get_current_user, get_db
from app.models.notification import Notification
from app.models.user import User
from app.schemas.notification import NotificationPrefsUpdate, NotificationResponse

router = APIRouter(tags=["notifications"])


@router.get("/", response_model=Dict)
async def list_notifications(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    unread_only: bool = Query(False),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List notifications for the current user."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    query = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        query = query.where(Notification.is_read == False)
    query = query.order_by(Notification.created_at.desc())

    notifications = db.exec(query).all()
    total = len(notifications)
    offset = (page - 1) * size
    paginated = notifications[offset: offset + size]

    def _notification_to_response(n: Notification) -> NotificationResponse:
        data = None
        if n.data:
            try:
                data = json.loads(n.data)
            except Exception:
                pass
        return NotificationResponse(
            id=n.id,
            user_id=n.user_id,
            title=n.title,
            body=n.body,
            type=n.type,
            is_read=n.is_read,
            data=data,
            created_at=n.created_at,
        )

    return {
        "items": [_notification_to_response(n) for n in paginated],
        "total": total,
        "unread_count": sum(1 for n in notifications if not n.is_read),
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
    """Mark a notification as read."""
    notification = db.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None or notification.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    notification.is_read = True
    db.add(notification)
    db.commit()
    return {"message": "Marked as read"}


@router.patch("/read-all")
async def mark_all_notifications_read(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark all notifications as read."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    notifications = db.exec(
        select(Notification).where(
            Notification.user_id == user.id,
            Notification.is_read == False,
        )
    ).all()
    for n in notifications:
        n.is_read = True
        db.add(n)
    db.commit()
    return {"message": f"Marked {len(notifications)} notifications as read"}


@router.put("/preferences")
async def update_notification_preferences(
    payload: NotificationPrefsUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update notification preferences."""
    from datetime import datetime

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    user.notification_prefs = json.dumps(payload.model_dump())
    user.updated_at = datetime.utcnow()
    db.add(user)
    db.commit()
    return {"message": "Preferences updated", "prefs": payload.model_dump()}
