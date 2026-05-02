from __future__ import annotations

import math
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.dependencies import get_db, require_role
from app.models.teacher import TeacherProfile
from app.models.user import User
from app.schemas.admin import TeacherApprovalAction

router = APIRouter(tags=["admin-teachers"])

admin_required = require_role("admin")


@router.get("/pending")
async def list_pending_teachers(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """List teachers pending approval."""
    import json

    stmt = select(TeacherProfile, User).join(User, User.id == TeacherProfile.user_id).where(
        TeacherProfile.is_approved == False
    )
    results = db.exec(stmt).all()

    total = len(results)
    offset = (page - 1) * size
    paginated = results[offset: offset + size]

    return {
        "items": [
            {
                "profile_id": str(p.id),
                "user_id": str(u.id),
                "full_name": u.full_name,
                "email": u.email,
                "avatar_url": u.avatar_url,
                "subjects": json.loads(p.subjects or "[]"),
                "levels": json.loads(p.levels or "[]"),
                "price_per_session": p.price_per_session,
                "experience_years": p.experience_years,
                "diplomas": json.loads(p.diplomas or "[]"),
                "created_at": p.created_at.isoformat(),
            }
            for p, u in paginated
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.put("/{teacher_profile_id}/approve")
async def approve_teacher(
    teacher_profile_id: UUID,
    payload: TeacherApprovalAction,
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Approve a teacher's profile."""
    from datetime import datetime

    profile = db.get(TeacherProfile, teacher_profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    profile.is_approved = True
    profile.is_verified = True
    profile.updated_at = datetime.utcnow()
    db.add(profile)
    db.commit()

    # Send notification to teacher
    teacher_user = db.get(User, profile.user_id)
    if teacher_user:
        from app.models.notification import Notification
        notif = Notification(
            user_id=teacher_user.id,
            title="Profil approuvé !",
            body="Félicitations ! Votre profil de tuteur a été approuvé. Vous pouvez maintenant recevoir des réservations.",
            type="system",
        )
        db.add(notif)
        db.commit()

    return {"message": "Teacher approved", "profile_id": str(teacher_profile_id)}


@router.put("/{teacher_profile_id}/reject")
async def reject_teacher(
    teacher_profile_id: UUID,
    payload: TeacherApprovalAction,
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Reject a teacher's profile."""
    from datetime import datetime

    profile = db.get(TeacherProfile, teacher_profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    profile.is_approved = False
    profile.is_verified = False
    profile.updated_at = datetime.utcnow()
    db.add(profile)
    db.commit()

    # Notify teacher
    teacher_user = db.get(User, profile.user_id)
    if teacher_user:
        from app.models.notification import Notification
        reason = payload.reason or "Votre profil n'a pas pu être approuvé."
        notif = Notification(
            user_id=teacher_user.id,
            title="Profil non approuvé",
            body=f"Votre profil de tuteur n'a pas été approuvé. Raison: {reason}",
            type="system",
        )
        db.add(notif)
        db.commit()

    return {"message": "Teacher rejected", "profile_id": str(teacher_profile_id), "reason": payload.reason}
