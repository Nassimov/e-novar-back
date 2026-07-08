from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db, require_role
from app.models.teacher import TeacherPayout, TeacherProfile, TeacherSlot
from app.models.user import User
from app.models.review import Review
from app.schemas.teacher import (
    DiplomaResponse,
    DzdWithdrawalRequest,
    EvaluationCreate,
    PayoutModeUpdate,
    SlotCreate,
    SlotResponse,
    SlotUpdate,
    TeacherBookingItem,
    TeacherBookingStudentInfo,
    TeacherDetailResponse,
    TeacherListItem,
    TeacherListResponse,
    TeacherProfileUpdate,
    WalletResponse,
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


def _slot_to_response(s: TeacherSlot) -> SlotResponse:
    return SlotResponse(
        id=str(s.id),
        date=s.slot_date.isoformat(),
        start_time=str(s.start_time)[:5],
        end_time=str(s.end_time)[:5],
        type=s.type,
        max_students=s.max_students,
        mode=s.mode,
        price=s.price,
        status=s.status,
    )


@router.get("/me/slots", response_model=List[SlotResponse])
async def list_my_slots(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """List the teacher's slots."""
    import datetime as dt
    teacher_id = UUID(current_user["id"])
    today = dt.date.today()
    slots = db.exec(
        select(TeacherSlot)
        .where(TeacherSlot.teacher_id == teacher_id)
        .where(TeacherSlot.slot_date >= today)
        .order_by(TeacherSlot.slot_date, TeacherSlot.start_time)
    ).all()
    return [_slot_to_response(s) for s in slots]


@router.post("/me/slots", response_model=SlotResponse, status_code=status.HTTP_201_CREATED)
async def create_slot(
    payload: SlotCreate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Create a concrete-date availability slot."""
    import datetime as dt
    teacher_id = UUID(current_user["id"])
    try:
        slot_date = dt.date.fromisoformat(payload.date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format — use YYYY-MM-DD")

    try:
        start_time = dt.time.fromisoformat(payload.start_time)
        end_time = dt.time.fromisoformat(payload.end_time)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid time format — use HH:MM")

    if end_time <= start_time:
        raise HTTPException(status_code=422, detail="end_time must be after start_time")

    slot = TeacherSlot(
        teacher_id=teacher_id,
        slot_date=slot_date,
        start_time=start_time,
        end_time=end_time,
        type=payload.type,
        max_students=payload.max_students,
        mode=payload.mode,
        price=payload.price,
        status=payload.status,
    )
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return _slot_to_response(slot)


@router.put("/me/slots/{slot_id}", response_model=SlotResponse)
async def update_slot(
    slot_id: UUID,
    payload: SlotUpdate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Update a slot."""
    import datetime as dt
    teacher_id = UUID(current_user["id"])
    slot = db.get(TeacherSlot, slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail="Slot not found")
    if slot.teacher_id != teacher_id:
        raise HTTPException(status_code=403, detail="Access denied")

    if payload.date is not None:
        slot.slot_date = dt.date.fromisoformat(payload.date)
    if payload.start_time is not None:
        slot.start_time = dt.time.fromisoformat(payload.start_time)
    if payload.end_time is not None:
        slot.end_time = dt.time.fromisoformat(payload.end_time)
    if payload.type is not None:
        slot.type = payload.type
    if payload.max_students is not None:
        slot.max_students = payload.max_students
    if payload.mode is not None:
        slot.mode = payload.mode
    if payload.price is not None:
        slot.price = payload.price
    if payload.status is not None:
        slot.status = payload.status

    db.add(slot)
    db.commit()
    db.refresh(slot)
    return _slot_to_response(slot)


@router.delete("/me/slots/{slot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_slot(
    slot_id: UUID,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Delete a slot (only if not booked)."""
    teacher_id = UUID(current_user["id"])
    slot = db.get(TeacherSlot, slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail="Slot not found")
    if slot.teacher_id != teacher_id:
        raise HTTPException(status_code=403, detail="Access denied")
    if slot.status == "booked":
        raise HTTPException(status_code=409, detail="Cannot delete a booked slot")

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


@router.get("/me/wallet", response_model=WalletResponse)
async def get_wallet(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Get the teacher's DZD wallet, EP balance, and bank info."""
    from app.models.kp import KpBalance
    from app.models.profile import TeacherProfile as _TeacherProfile

    uid = UUID(current_user["id"])
    kp = db.exec(select(KpBalance).where(KpBalance.user_id == uid)).first()
    tp = db.exec(select(_TeacherProfile).where(_TeacherProfile.user_id == uid)).first()

    return WalletResponse(
        wallet_balance_dzd=tp.wallet_balance_dzd if tp else 0,
        payout_mode=tp.payout_mode if tp else "platform",
        ep_balance=kp.balance if kp else 0,
        ep_total_earned=kp.total_earned if kp else 0,
        iban=tp.iban if tp else None,
        bank_holder=tp.bank_holder if tp else None,
        bank_last4=tp.bank_last4 if tp else None,
    )


@router.patch("/me/payout-mode")
async def update_payout_mode(
    payload: PayoutModeUpdate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Update the teacher's preferred payout mode."""
    from app.models.profile import TeacherProfile as _TeacherProfile

    if payload.payout_mode not in ("platform", "direct"):
        raise HTTPException(status_code=422, detail="payout_mode must be 'platform' or 'direct'")

    uid = UUID(current_user["id"])
    tp = db.exec(select(_TeacherProfile).where(_TeacherProfile.user_id == uid)).first()
    if tp is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    tp.payout_mode = payload.payout_mode
    db.add(tp)
    db.commit()
    return {"payout_mode": tp.payout_mode}


@router.get("/me/bookings", response_model=List[TeacherBookingItem])
async def list_teacher_bookings(
    booking_status: Optional[str] = Query(None, alias="status"),
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """List bookings for the logged-in teacher."""
    from app.models.booking import Booking
    from app.models.profile import Profile

    teacher_id = UUID(current_user["id"])
    stmt = select(Booking).where(Booking.teacher_id == teacher_id)
    if booking_status:
        stmt = stmt.where(Booking.status == booking_status)
    stmt = stmt.order_by(Booking.created_at.desc())
    bookings = db.exec(stmt).all()

    student_ids = list({b.student_id for b in bookings})
    profiles = db.exec(select(Profile).where(Profile.id.in_(student_ids))).all() if student_ids else []
    prof_map = {p.id: p for p in profiles}

    result = []
    for b in bookings:
        prof = prof_map.get(b.student_id)
        student_info = (
            TeacherBookingStudentInfo(
                id=str(b.student_id),
                full_name=prof.full_name or "",
                avatar_url=prof.avatar_url,
            )
            if prof else None
        )
        result.append(TeacherBookingItem(
            id=str(b.id),
            student=student_info,
            formula=b.formula,
            mode=b.mode,
            date=b.booking_date.isoformat() if b.booking_date else None,
            slot_time=str(b.slot_time)[:5] if b.slot_time else None,
            duration_min=b.duration_min,
            amount=b.amount,
            status=b.status,
            stripe_cs_id=b.stripe_cs_id,
            stripe_pi_id=b.stripe_pi_id,
            created_at=b.created_at.isoformat(),
        ))
    return result


@router.post("/me/bookings/{booking_id}/accept")
async def accept_booking(
    booking_id: UUID,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Accept a booking: capture Stripe payment and credit wallet."""
    from app.models.booking import Booking
    from app.models.profile import TeacherProfile as _TeacherProfile
    from app.services.stripe import capture_payment_intent, get_checkout_session

    teacher_id = UUID(current_user["id"])
    booking = db.get(Booking, booking_id)
    if booking is None or booking.teacher_id != teacher_id:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.status not in ("pending", "awaiting_teacher"):
        raise HTTPException(status_code=409, detail=f"Cannot accept a booking with status '{booking.status}'")

    # Retrieve PI ID if we only have the CS ID
    pi_id = booking.stripe_pi_id
    if not pi_id and booking.stripe_cs_id:
        try:
            session_data = get_checkout_session(booking.stripe_cs_id)
            pi_id = session_data.get("payment_intent")
            if pi_id:
                booking.stripe_pi_id = pi_id
        except Exception:
            pass

    # Capture payment
    if pi_id:
        try:
            capture_payment_intent(pi_id)
        except Exception as exc:
            raise HTTPException(status_code=402, detail=f"Payment capture failed: {exc}")

    booking.status = "confirmed"
    db.add(booking)

    # Credit teacher wallet
    tp = db.exec(select(_TeacherProfile).where(_TeacherProfile.user_id == teacher_id)).first()
    if tp:
        tp.wallet_balance_dzd += booking.amount
        db.add(tp)

    db.commit()
    return {"status": "confirmed", "amount_credited": booking.amount}


@router.post("/me/bookings/{booking_id}/refuse")
async def refuse_booking(
    booking_id: UUID,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Refuse a booking: cancel/refund Stripe PaymentIntent."""
    from app.models.booking import Booking
    from app.services.stripe import cancel_payment_intent, get_checkout_session

    teacher_id = UUID(current_user["id"])
    booking = db.get(Booking, booking_id)
    if booking is None or booking.teacher_id != teacher_id:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.status not in ("pending", "awaiting_teacher"):
        raise HTTPException(status_code=409, detail=f"Cannot refuse a booking with status '{booking.status}'")

    # Retrieve PI ID if we only have the CS ID
    pi_id = booking.stripe_pi_id
    if not pi_id and booking.stripe_cs_id:
        try:
            session_data = get_checkout_session(booking.stripe_cs_id)
            pi_id = session_data.get("payment_intent")
            if pi_id:
                booking.stripe_pi_id = pi_id
        except Exception:
            pass

    # Cancel/refund payment
    if pi_id:
        try:
            cancel_payment_intent(pi_id)
        except Exception:
            pass  # If already cancelled or expired, proceed anyway

    booking.status = "cancelled"
    db.add(booking)
    db.commit()
    return {"status": "cancelled"}


@router.post("/me/withdrawals/dzd", status_code=status.HTTP_201_CREATED)
async def request_dzd_withdrawal(
    payload: DzdWithdrawalRequest,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Request a DZD wallet withdrawal. Requires at least 1 completed session."""
    from app.models.booking import Booking
    from app.models.profile import TeacherProfile as _TeacherProfile

    teacher_id = UUID(current_user["id"])
    tp = db.exec(select(_TeacherProfile).where(_TeacherProfile.user_id == teacher_id)).first()
    if tp is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    if tp.wallet_balance_dzd < payload.amount_dzd:
        raise HTTPException(
            status_code=400,
            detail=f"Solde insuffisant. Disponible : {tp.wallet_balance_dzd} DA",
        )

    # Deduct from wallet (admin processes the transfer manually)
    tp.wallet_balance_dzd -= payload.amount_dzd
    db.add(tp)
    db.commit()
    return {
        "message": f"Demande de retrait de {payload.amount_dzd} DA enregistrée. Un virement sera effectué sous 72h.",
        "remaining_balance": tp.wallet_balance_dzd,
    }


@router.get("/me/withdrawals", response_model=List[WithdrawalResponse])
async def list_withdrawals(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """List payout requests for the teacher."""
    uid = UUID(current_user["id"])
    payouts = db.exec(
        select(TeacherPayout)
        .where(TeacherPayout.teacher_id == uid)
        .order_by(TeacherPayout.requested_at.desc())
    ).all()
    return [WithdrawalResponse.model_validate(p) for p in payouts]


@router.post("/me/withdrawals", response_model=WithdrawalResponse, status_code=status.HTTP_201_CREATED)
async def request_withdrawal(
    payload: WithdrawalRequest,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Request an EP → DZD payout. Requires at least 1 completed session."""
    from app.models.kp import KpBalance
    from app.models.profile import TeacherProfile as _TeacherProfile

    uid = UUID(current_user["id"])

    # Verify at least 1 completed session
    from app.models.booking import Booking
    done = db.exec(
        select(Booking)
        .where(Booking.teacher_id == uid)
        .where(Booking.status == "completed")
    ).first()
    if done is None:
        raise HTTPException(
            status_code=403,
            detail="Au moins une séance complétée est requise avant de demander un retrait.",
        )

    # Check EP balance
    kp = db.exec(select(KpBalance).where(KpBalance.user_id == uid)).first()
    available = kp.balance if kp else 0
    if available < payload.ep_amount:
        raise HTTPException(
            status_code=400,
            detail=f"Solde EP insuffisant. Disponible : {available} EP.",
        )

    payout = TeacherPayout(
        teacher_id=uid,
        ep_amount=payload.ep_amount,
        iban=payload.iban,
        bank_holder=payload.bank_holder,
    )
    db.add(payout)
    db.commit()
    db.refresh(payout)
    return WithdrawalResponse.model_validate(payout)


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
        {
            "date": b.booking_date.isoformat() if b.booking_date else None,
            "slot_time": str(b.slot_time)[:5] if b.slot_time else None,
            "duration_min": b.duration_min,
        }
        for b in bookings
        if b.booking_date
    ]


@router.get("/{teacher_id}/availability")
async def get_teacher_availability(teacher_id: UUID, db: Session = Depends(get_db)):
    """Get the open slots for a teacher."""
    import datetime as dt
    stmt = select(TeacherProfile).where(TeacherProfile.user_id == teacher_id)
    profile = db.exec(stmt).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher not found")

    today = dt.date.today()
    slots = db.exec(
        select(TeacherSlot).where(
            TeacherSlot.teacher_id == teacher_id,
            TeacherSlot.status == "open",
            TeacherSlot.slot_date >= today,
        )
    ).all()
    return [_slot_to_response(s) for s in slots]
