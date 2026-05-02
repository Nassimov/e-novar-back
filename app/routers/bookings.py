from __future__ import annotations

import math
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.booking import Booking
from app.models.promo import PromoCode
from app.models.teacher import TeacherProfile
from app.models.user import User
from app.schemas.booking import BookingCreate, BookingResponse, BookingUpdate

router = APIRouter(tags=["bookings"])

FORMULA_PRICES = {
    "single": 1,
    "pack5": 5,
    "monthly": 20,
}

FORMULA_KP = {
    "single": 50,
    "pack5": 300,
    "monthly": 1200,
}


@router.post("/", response_model=BookingResponse, status_code=status.HTTP_201_CREATED)
async def create_booking(
    payload: BookingCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new booking."""
    from datetime import datetime

    stmt = select(User).where(User.supabase_id == current_user["id"])
    student = db.exec(stmt).first()
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")

    # Verify teacher exists
    teacher_profile = db.exec(
        select(TeacherProfile).where(TeacherProfile.id == payload.teacher_id)
    ).first()
    if teacher_profile is None:
        raise HTTPException(status_code=404, detail="Teacher not found")
    if not teacher_profile.is_approved:
        raise HTTPException(status_code=400, detail="Teacher is not yet approved")

    amount = teacher_profile.price_per_session * FORMULA_PRICES.get(payload.formula, 1)
    kp_reward = FORMULA_KP.get(payload.formula, 50)

    # Apply promo code if provided
    if payload.promo_code:
        promo = db.exec(
            select(PromoCode).where(
                PromoCode.code == payload.promo_code,
                PromoCode.is_active == True,
            )
        ).first()
        if promo:
            if promo.discount_percent:
                amount = int(amount * (1 - promo.discount_percent / 100))
            elif promo.discount_dzd:
                amount = max(0, amount - promo.discount_dzd)
            promo.used_count += 1
            db.add(promo)

    booking = Booking(
        student_id=student.id,
        teacher_id=teacher_profile.user_id,
        formula=payload.formula,
        mode=payload.mode,
        date=payload.date,
        slot_time=payload.slot_time,
        duration_minutes=payload.duration_minutes,
        amount_dzd=amount,
        kp_reward=kp_reward,
        promo_code=payload.promo_code,
        payment_method=payload.payment_method,
        subject=payload.subject,
        level=payload.level,
        notes=payload.notes,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    # Send booking confirmation email async
    try:
        teacher_user = db.get(User, teacher_profile.user_id)
        from app.workers.email_tasks import send_booking_confirmation

        send_booking_confirmation.delay(
            student.email,
            {
                "teacher_name": teacher_user.full_name if teacher_user else "",
                "date": str(payload.date or ""),
                "slot_time": payload.slot_time or "",
                "amount_dzd": amount,
                "subject": payload.subject or "",
            },
        )
    except Exception:
        pass

    return BookingResponse.model_validate(booking)


@router.get("/", response_model=Dict)
async def list_bookings(
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List bookings for the current user."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    role = current_user.get("role", "student")
    if role == "teacher":
        query = select(Booking).where(Booking.teacher_id == user.id)
    else:
        query = select(Booking).where(Booking.student_id == user.id)

    if status:
        query = query.where(Booking.status == status)

    bookings = db.exec(query).all()
    total = len(bookings)
    offset = (page - 1) * size
    paginated = bookings[offset: offset + size]

    return {
        "items": [BookingResponse.model_validate(b) for b in paginated],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.get("/{booking_id}", response_model=BookingResponse)
async def get_booking(
    booking_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a specific booking."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()

    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    if user and booking.student_id != user.id and booking.teacher_id != user.id:
        if current_user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Access denied")

    return BookingResponse.model_validate(booking)


@router.put("/{booking_id}", response_model=BookingResponse)
async def update_booking(
    booking_id: UUID,
    payload: BookingUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a booking (reschedule or change status)."""
    from datetime import datetime

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()

    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    if user and booking.student_id != user.id and booking.teacher_id != user.id:
        if current_user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Access denied")

    valid_statuses = {"pending", "confirmed", "cancelled", "completed"}
    if payload.status:
        if payload.status not in valid_statuses:
            raise HTTPException(status_code=400, detail="Invalid status")
        booking.status = payload.status
    if payload.date:
        booking.date = payload.date
    if payload.slot_time:
        booking.slot_time = payload.slot_time
    if payload.notes is not None:
        booking.notes = payload.notes

    booking.updated_at = datetime.utcnow()
    db.add(booking)
    db.commit()
    db.refresh(booking)
    return BookingResponse.model_validate(booking)


@router.delete("/{booking_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_booking(
    booking_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel a booking."""
    from datetime import datetime

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()

    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    if user and booking.student_id != user.id and booking.teacher_id != user.id:
        if current_user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Access denied")

    booking.status = "cancelled"
    booking.updated_at = datetime.utcnow()
    db.add(booking)
    db.commit()
    return None
