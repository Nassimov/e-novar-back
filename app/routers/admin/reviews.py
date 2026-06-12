from __future__ import annotations

import math
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.review import Review
from app.models.user import User
from app.schemas.admin import ReviewModerationAction

router = APIRouter(tags=["admin-reviews"])




@router.get("/")
async def list_reviews(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    flagged_only: bool = Query(False),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List all reviews, optionally filtered to flagged ones."""
    query = select(Review)
    if flagged_only:
        query = query.where(Review.is_flagged == True)

    reviews = db.exec(query.order_by(Review.created_at.desc())).all()
    total = len(reviews)
    offset = (page - 1) * size
    paginated = reviews[offset: offset + size]

    result = []
    for r in paginated:
        student = db.get(User, r.student_id)
        teacher = db.get(User, r.teacher_id)
        result.append({
            "id": str(r.id),
            "rating": r.rating,
            "comment": r.comment,
            "is_flagged": r.is_flagged,
            "flag_reason": r.flag_reason,
            "student_name": student.full_name if student else "Unknown",
            "teacher_name": teacher.full_name if teacher else "Unknown",
            "booking_id": str(r.booking_id) if r.booking_id else None,
            "created_at": r.created_at.isoformat(),
        })

    return {
        "items": result,
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.delete("/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review(
    review_id: UUID,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Delete a review."""
    review = db.get(Review, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    # Update teacher's rating
    from app.models.teacher import TeacherProfile
    from app.models.session import Session as SessionModel

    teacher_profile = db.exec(
        select(TeacherProfile).where(TeacherProfile.user_id == review.teacher_id)
    ).first()

    db.delete(review)
    db.commit()

    # Recalculate teacher rating
    if teacher_profile:
        remaining = db.exec(
            select(Review).where(Review.teacher_id == review.teacher_id)
        ).all()
        if remaining:
            teacher_profile.rating = sum(r.rating for r in remaining) / len(remaining)
            teacher_profile.reviews_count = len(remaining)
        else:
            teacher_profile.rating = 0.0
            teacher_profile.reviews_count = 0
        db.add(teacher_profile)
        db.commit()

    return None
