from __future__ import annotations

from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.promo import Favorite
from app.models.teacher import TeacherProfile
from app.models.user import User

router = APIRouter(tags=["favorites"])


@router.get("/")
async def list_favorites(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the current user's favorite teachers."""
    import json

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    favorites = db.exec(
        select(Favorite).where(Favorite.student_id == user.id)
    ).all()

    result = []
    for fav in favorites:
        teacher_profile = db.exec(
            select(TeacherProfile).where(TeacherProfile.user_id == fav.teacher_id)
        ).first()
        teacher_user = db.get(User, fav.teacher_id)

        if teacher_profile and teacher_user:
            result.append({
                "favorite_id": str(fav.id),
                "teacher_id": str(teacher_profile.id),
                "teacher_user_id": str(fav.teacher_id),
                "full_name": teacher_user.full_name,
                "avatar_url": teacher_user.avatar_url,
                "subjects": json.loads(teacher_profile.subjects or "[]"),
                "rating": teacher_profile.rating,
                "price_per_session": teacher_profile.price_per_session,
                "added_at": fav.created_at.isoformat(),
            })
    return {"items": result, "total": len(result)}


@router.post("/", status_code=status.HTTP_201_CREATED)
async def add_favorite(
    teacher_user_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add a teacher to favorites."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Verify teacher exists
    teacher = db.get(User, teacher_user_id)
    if teacher is None:
        raise HTTPException(status_code=404, detail="Teacher not found")

    # Check if already favorited
    existing = db.exec(
        select(Favorite).where(
            Favorite.student_id == user.id,
            Favorite.teacher_id == teacher_user_id,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already in favorites")

    fav = Favorite(student_id=user.id, teacher_id=teacher_user_id)
    db.add(fav)
    db.commit()
    db.refresh(fav)
    return {"id": str(fav.id), "message": "Added to favorites"}


@router.delete("/{teacher_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_favorite(
    teacher_user_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a teacher from favorites."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    fav = db.exec(
        select(Favorite).where(
            Favorite.student_id == user.id,
            Favorite.teacher_id == teacher_user_id,
        )
    ).first()
    if fav is None:
        raise HTTPException(status_code=404, detail="Not in favorites")

    db.delete(fav)
    db.commit()
    return None
