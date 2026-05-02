from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db, require_role
from app.models.teacher import TeacherProfile, TeacherSlot, TeacherWithdrawal
from app.models.user import User
from app.models.review import Review
from app.schemas.teacher import (
    DiplomaResponse,
    EvaluationCreate,
    SlotCreate,
    SlotResponse,
    SlotUpdate,
    TeacherDetailResponse,
    TeacherListItem,
    TeacherListResponse,
    TeacherProfileUpdate,
    WithdrawalRequest,
    WithdrawalResponse,
)

router = APIRouter(tags=["teachers"])


def _profile_to_detail(profile: TeacherProfile, user: User) -> TeacherDetailResponse:
    return TeacherDetailResponse(
        id=profile.id,
        user_id=profile.user_id,
        full_name=user.full_name,
        avatar_url=user.avatar_url,
        bio=profile.bio,
        subjects=json.loads(profile.subjects or "[]"),
        levels=json.loads(profile.levels or "[]"),
        price_per_session=profile.price_per_session,
        modes=json.loads(profile.modes or '["online"]'),
        rating=profile.rating,
        reviews_count=profile.reviews_count,
        badge=profile.badge,
        wilaya=user.wilaya,
        experience_years=profile.experience_years,
        is_approved=profile.is_approved,
        is_verified=profile.is_verified,
        diplomas=json.loads(profile.diplomas or "[]"),
    )


@router.get("/", response_model=TeacherListResponse)
async def search_teachers(
    query: Optional[str] = Query(None),
    wilaya: Optional[str] = Query(None),
    subject: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    price_min: Optional[int] = Query(None),
    price_max: Optional[int] = Query(None),
    min_rating: Optional[float] = Query(None),
    mode: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Search and filter teachers."""
    stmt = select(TeacherProfile, User).join(User, User.id == TeacherProfile.user_id).where(
        TeacherProfile.is_approved == True,
        User.is_active == True,
    )

    if price_min is not None:
        stmt = stmt.where(TeacherProfile.price_per_session >= price_min)
    if price_max is not None:
        stmt = stmt.where(TeacherProfile.price_per_session <= price_max)
    if min_rating is not None:
        stmt = stmt.where(TeacherProfile.rating >= min_rating)
    if wilaya:
        stmt = stmt.where(User.wilaya == wilaya)

    results = db.exec(stmt).all()

    items = []
    for profile, user in results:
        subjects_list = json.loads(profile.subjects or "[]")
        levels_list = json.loads(profile.levels or "[]")
        modes_list = json.loads(profile.modes or '["online"]')

        if subject and subject not in subjects_list:
            continue
        if level and level not in levels_list:
            continue
        if mode and mode not in modes_list:
            continue
        if query and query.lower() not in user.full_name.lower():
            continue

        items.append(TeacherListItem(
            id=profile.id,
            user_id=profile.user_id,
            full_name=user.full_name,
            avatar_url=user.avatar_url,
            subjects=subjects_list,
            levels=levels_list,
            price_per_session=profile.price_per_session,
            modes=modes_list,
            rating=profile.rating,
            reviews_count=profile.reviews_count,
            badge=profile.badge,
            wilaya=user.wilaya,
            experience_years=profile.experience_years,
            is_approved=profile.is_approved,
        ))

    total = len(items)
    offset = (page - 1) * size
    paginated = items[offset: offset + size]

    return TeacherListResponse(
        items=paginated,
        total=total,
        page=page,
        size=size,
        pages=math.ceil(total / size) if total else 0,
    )


@router.get("/me", response_model=TeacherDetailResponse)
async def get_my_teacher_profile(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Get the logged-in teacher's profile."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    stmt2 = select(TeacherProfile).where(TeacherProfile.user_id == user.id)
    profile = db.exec(stmt2).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    return _profile_to_detail(profile, user)


@router.put("/me", response_model=TeacherDetailResponse)
async def update_my_teacher_profile(
    payload: TeacherProfileUpdate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Update the logged-in teacher's profile."""
    from datetime import datetime

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    stmt2 = select(TeacherProfile).where(TeacherProfile.user_id == user.id)
    profile = db.exec(stmt2).first()
    if profile is None:
        profile = TeacherProfile(user_id=user.id)

    if payload.subjects is not None:
        profile.subjects = json.dumps(payload.subjects)
    if payload.levels is not None:
        profile.levels = json.dumps(payload.levels)
    if payload.price_per_session is not None:
        profile.price_per_session = payload.price_per_session
    if payload.modes is not None:
        profile.modes = json.dumps(payload.modes)
    if payload.bio is not None:
        profile.bio = payload.bio
    if payload.experience_years is not None:
        profile.experience_years = payload.experience_years
    profile.updated_at = datetime.utcnow()

    db.add(profile)
    db.commit()
    db.refresh(profile)
    return _profile_to_detail(profile, user)


@router.post("/me/diplomas", status_code=status.HTTP_201_CREATED)
async def upload_diploma(
    file: UploadFile,
    name: str = Query(...),
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Upload a diploma or certificate for the teacher."""
    from datetime import datetime
    import uuid
    from app.services.storage import upload_file

    contents = await file.read()
    url = upload_file(contents, file.filename or "diploma.pdf", file.content_type, folder="diplomas")

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    stmt2 = select(TeacherProfile).where(TeacherProfile.user_id == user.id)
    profile = db.exec(stmt2).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    diplomas = json.loads(profile.diplomas or "[]")
    diploma_id = str(uuid.uuid4())
    diplomas.append({"id": diploma_id, "name": name, "url": url})
    profile.diplomas = json.dumps(diplomas)
    profile.updated_at = datetime.utcnow()
    db.add(profile)
    db.commit()
    return {"id": diploma_id, "name": name, "url": url}


@router.delete("/me/diplomas/{diploma_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_diploma(
    diploma_id: str,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Remove a diploma from the teacher's profile."""
    from datetime import datetime

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    stmt2 = select(TeacherProfile).where(TeacherProfile.user_id == user.id)
    profile = db.exec(stmt2).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    diplomas = json.loads(profile.diplomas or "[]")
    diplomas = [d for d in diplomas if d.get("id") != diploma_id]
    profile.diplomas = json.dumps(diplomas)
    profile.updated_at = datetime.utcnow()
    db.add(profile)
    db.commit()
    return None


@router.put("/me/bank-card")
async def update_bank_card(
    iban: str,
    holder: str,
    last4: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Update the teacher's bank card / withdrawal info."""
    from datetime import datetime

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    stmt2 = select(TeacherProfile).where(TeacherProfile.user_id == user.id)
    profile = db.exec(stmt2).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    profile.bank_iban = iban
    profile.bank_holder = holder
    profile.bank_last4 = last4
    profile.updated_at = datetime.utcnow()
    db.add(profile)
    db.commit()
    return {"message": "Bank card updated"}


@router.get("/me/slots", response_model=List[SlotResponse])
async def list_my_slots(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """List the teacher's availability slots."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    slots = db.exec(select(TeacherSlot).where(TeacherSlot.teacher_id == user.id)).all()
    return [SlotResponse.model_validate(s) for s in slots]


@router.post("/me/slots", response_model=SlotResponse, status_code=status.HTTP_201_CREATED)
async def create_slot(
    payload: SlotCreate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Add an availability slot."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    slot = TeacherSlot(
        teacher_id=user.id,
        day_of_week=payload.day_of_week,
        start_time=payload.start_time,
        end_time=payload.end_time,
    )
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return SlotResponse.model_validate(slot)


@router.put("/me/slots/{slot_id}", response_model=SlotResponse)
async def update_slot(
    slot_id: UUID,
    payload: SlotUpdate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Update an availability slot."""
    slot = db.get(TeacherSlot, slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail="Slot not found")

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None or slot.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if payload.is_available is not None:
        slot.is_available = payload.is_available
    if payload.start_time:
        slot.start_time = payload.start_time
    if payload.end_time:
        slot.end_time = payload.end_time

    db.add(slot)
    db.commit()
    db.refresh(slot)
    return SlotResponse.model_validate(slot)


@router.delete("/me/slots/{slot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_slot(
    slot_id: UUID,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Delete an availability slot."""
    slot = db.get(TeacherSlot, slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail="Slot not found")

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None or slot.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    db.delete(slot)
    db.commit()
    return None


@router.get("/me/students")
async def get_my_students(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Get the list of students who have booked with this teacher."""
    from app.models.booking import Booking

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    bookings = db.exec(
        select(Booking).where(Booking.teacher_id == user.id)
    ).all()
    student_ids = list({b.student_id for b in bookings})
    students = db.exec(select(User).where(User.id.in_(student_ids))).all()

    return [
        {"id": str(s.id), "full_name": s.full_name, "email": s.email, "avatar_url": s.avatar_url}
        for s in students
    ]


@router.post("/me/invitations")
async def send_student_invitation(
    email: str,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
):
    """Invite a student to join the platform."""
    from app.workers.email_tasks import send_welcome_email
    # Send invitation email
    send_welcome_email.delay(email, "Étudiant")
    return {"message": f"Invitation sent to {email}"}


@router.get("/me/wallet")
async def get_wallet(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Get the teacher's wallet balance."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    stmt2 = select(TeacherProfile).where(TeacherProfile.user_id == user.id)
    profile = db.exec(stmt2).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    return {
        "balance": profile.withdrawal_balance,
        "currency": "DZD",
        "bank_iban": profile.bank_iban,
        "bank_holder": profile.bank_holder,
        "bank_last4": profile.bank_last4,
    }


@router.get("/me/withdrawals", response_model=List[WithdrawalResponse])
async def list_withdrawals(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """List withdrawal requests for the teacher."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    withdrawals = db.exec(
        select(TeacherWithdrawal).where(TeacherWithdrawal.teacher_id == user.id)
    ).all()
    return [WithdrawalResponse.model_validate(w) for w in withdrawals]


@router.post("/me/withdrawals", response_model=WithdrawalResponse, status_code=status.HTTP_201_CREATED)
async def request_withdrawal(
    payload: WithdrawalRequest,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Request a withdrawal."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    stmt2 = select(TeacherProfile).where(TeacherProfile.user_id == user.id)
    profile = db.exec(stmt2).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    if profile.withdrawal_balance < payload.amount:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance")

    iban = payload.iban or profile.bank_iban
    holder = payload.holder or profile.bank_holder

    withdrawal = TeacherWithdrawal(
        teacher_id=user.id,
        amount=payload.amount,
        iban=iban,
        holder=holder,
    )
    db.add(withdrawal)
    db.commit()
    db.refresh(withdrawal)
    return WithdrawalResponse.model_validate(withdrawal)


@router.get("/me/evaluations")
async def list_evaluations(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """List evaluations written by the teacher."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    reviews = db.exec(
        select(Review).where(Review.teacher_id == user.id)
    ).all()
    return [
        {
            "id": str(r.id),
            "student_id": str(r.student_id),
            "rating": r.rating,
            "comment": r.comment,
            "created_at": r.created_at.isoformat(),
        }
        for r in reviews
    ]


@router.post("/me/evaluations", status_code=status.HTTP_201_CREATED)
async def create_evaluation(
    payload: EvaluationCreate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Create a student evaluation."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    review = Review(
        teacher_id=user.id,
        student_id=payload.student_id,
        rating=payload.rating,
        comment=payload.comment,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return {"id": str(review.id), "message": "Evaluation created"}


@router.put("/me/evaluations/{evaluation_id}")
async def update_evaluation(
    evaluation_id: UUID,
    payload: EvaluationCreate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Update a student evaluation."""
    from datetime import datetime

    review = db.get(Review, evaluation_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Evaluation not found")

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None or review.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    review.rating = payload.rating
    review.comment = payload.comment
    db.add(review)
    db.commit()
    return {"message": "Evaluation updated"}


@router.get("/{teacher_id}", response_model=TeacherDetailResponse)
async def get_teacher(teacher_id: UUID, db: Session = Depends(get_db)):
    """Get a teacher's public profile."""
    stmt = select(TeacherProfile, User).join(User, User.id == TeacherProfile.user_id).where(
        TeacherProfile.id == teacher_id
    )
    result = db.exec(stmt).first()
    if result is None:
        raise HTTPException(status_code=404, detail="Teacher not found")
    profile, user = result
    return _profile_to_detail(profile, user)


@router.get("/{teacher_id}/reviews")
async def get_teacher_reviews(
    teacher_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Get reviews for a teacher."""
    stmt = select(TeacherProfile).where(TeacherProfile.id == teacher_id)
    profile = db.exec(stmt).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher not found")

    reviews = db.exec(
        select(Review).where(Review.teacher_id == profile.user_id)
    ).all()
    total = len(reviews)
    offset = (page - 1) * size
    paginated = reviews[offset: offset + size]

    return {
        "items": [
            {
                "id": str(r.id),
                "rating": r.rating,
                "comment": r.comment,
                "created_at": r.created_at.isoformat(),
            }
            for r in paginated
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/{teacher_id}/schedule")
async def get_teacher_schedule(teacher_id: UUID, db: Session = Depends(get_db)):
    """Get the booked sessions for a teacher (public)."""
    from app.models.booking import Booking

    stmt = select(TeacherProfile).where(TeacherProfile.id == teacher_id)
    profile = db.exec(stmt).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher not found")

    bookings = db.exec(
        select(Booking).where(
            Booking.teacher_id == profile.user_id,
            Booking.status == "confirmed",
        )
    ).all()
    return [
        {"date": str(b.date), "slot_time": b.slot_time, "duration_minutes": b.duration_minutes}
        for b in bookings
        if b.date and b.slot_time
    ]


@router.get("/{teacher_id}/availability")
async def get_teacher_availability(teacher_id: UUID, db: Session = Depends(get_db)):
    """Get the available slots for a teacher."""
    stmt = select(TeacherProfile).where(TeacherProfile.id == teacher_id)
    profile = db.exec(stmt).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher not found")

    slots = db.exec(
        select(TeacherSlot).where(
            TeacherSlot.teacher_id == profile.user_id,
            TeacherSlot.is_available == True,
        )
    ).all()
    return [SlotResponse.model_validate(s) for s in slots]
