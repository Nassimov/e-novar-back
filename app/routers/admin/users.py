"""Admin — platform users (students/teachers/parents), real data.

Rewritten from a legacy version that queried `User.role` (enum) / `User.
is_active` / `User.is_verified` — none of which exist on the current
`Profile` model (role lives separately in `user_roles`; there's no generic
account-suspension flag for students/parents, only `TeacherProfile.status`,
already managed on the dedicated Admin — Teachers page). Every call to the
old endpoints would 500 with an AttributeError.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.profile import ParentProfile, Profile, StudentProfile, TeacherProfile, UserRole

router = APIRouter(tags=["admin-users"])


def _roles_for(db: Session, user_ids: List[UUID]) -> Dict[UUID, List[str]]:
    if not user_ids:
        return {}
    rows = db.exec(select(UserRole).where(UserRole.user_id.in_(user_ids))).all()
    out: Dict[UUID, List[str]] = {}
    for r in rows:
        out.setdefault(r.user_id, []).append(r.role)
    return out


@router.get("/")
async def list_users(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    role: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List platform users (students/teachers/parents), optionally filtered
    by role and/or a name/email search."""
    query = select(Profile)

    if role:
        role_user_ids = db.exec(select(UserRole.user_id).where(UserRole.role == role)).all()
        if not role_user_ids:
            return {"items": [], "total": 0, "page": page, "size": size, "pages": 0}
        query = query.where(Profile.id.in_(role_user_ids))

    if search:
        s = f"%{search.lower()}%"
        query = query.where(
            (Profile.full_name.ilike(s)) | (Profile.email.ilike(s))
        )

    profiles = db.exec(query.order_by(Profile.created_at.desc())).all()
    total = len(profiles)
    offset = (page - 1) * size
    paginated = profiles[offset: offset + size]

    roles_map = _roles_for(db, [p.id for p in paginated])
    teacher_ids = {p.id for p in paginated if "teacher" in roles_map.get(p.id, [])}
    teacher_profiles = (
        db.exec(select(TeacherProfile).where(TeacherProfile.user_id.in_(teacher_ids))).all()
        if teacher_ids else []
    )
    teacher_status = {t.user_id: t.status for t in teacher_profiles}

    return {
        "items": [
            {
                "id": str(p.id),
                "email": p.email,
                "full_name": p.full_name,
                "roles": roles_map.get(p.id, []),
                "avatar_url": p.avatar_url,
                "wilaya": p.wilaya,
                "onboarding_completed": p.onboarding_completed,
                "teacher_status": teacher_status.get(p.id),
                "created_at": p.created_at.isoformat(),
                "last_seen_at": p.last_seen_at.isoformat() if p.last_seen_at else None,
            }
            for p in paginated
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.get("/{user_id}")
async def get_user(
    user_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    user = db.get(Profile, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    roles = db.exec(select(UserRole.role).where(UserRole.user_id == user_id)).all()
    student = db.get(StudentProfile, user_id)
    teacher = db.get(TeacherProfile, user_id)
    parent = db.get(ParentProfile, user_id)

    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "roles": list(roles),
        "phone": user.phone,
        "bio": user.bio,
        "wilaya": user.wilaya,
        "city": user.city,
        "school": user.school,
        "avatar_url": user.avatar_url,
        "onboarding_completed": user.onboarding_completed,
        "created_at": user.created_at.isoformat(),
        "updated_at": user.updated_at.isoformat(),
        "last_seen_at": user.last_seen_at.isoformat() if user.last_seen_at else None,
        "student_profile": {"level_main": student.level_main} if student else None,
        "teacher_profile": {"status": teacher.status, "rating_avg": teacher.rating_avg} if teacher else None,
        "parent_profile": {"parent_code": parent.parent_code} if parent else None,
    }


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Delete a platform user's profile row. Note: this does not delete the
    underlying Supabase auth.users row — use the Supabase service client for
    that if full account deletion (including login credentials) is needed."""
    user = db.get(Profile, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    db.delete(user)
    db.commit()
    return None
