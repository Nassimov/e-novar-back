from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db, require_role
from app.models.teacher import TeacherPayout, TeacherProfile, TeacherSlot
from app.models.scheduling import TeacherSlotSubject
from app.models.catalog import Level, Subject, TeacherDeliveryOption, TeacherDiploma, TeacherSubjectPrice
from app.models.profile import Profile
from app.models.user import User
from app.models.review import Review
from app.services.pricing import compute_pack_prices, get_platform_settings
from app.schemas.teacher import (
    BoostActivateRequest,
    BoostStatusResponse,
    DeliveryOption,
    DiplomaResponse,
    DzdWithdrawalRequest,
    EvaluationCreate,
    PayoutModeUpdate,
    SlotCreate,
    SlotResponse,
    SlotSubjectLevel,
    SlotSubjectLevelResponse,
    SlotUpdate,
    TeacherBookingItem,
    TeacherBookingStudentInfo,
    TeacherDetailResponse,
    TeacherDiplomaItem,
    TeacherListItem,
    TeacherListResponse,
    TeacherPaymentItem,
    TeacherPayoutInfoUpdate,
    TeacherProfileUpdate,
    TeacherSubjectItem,
    WalletResponse,
    WithdrawalRequest,
    WithdrawalResponse,
)

router = APIRouter(tags=["teachers"])

# NOTE: the bare `GET /` (search), `GET /{teacher_id}`, `GET /{teacher_id}/reviews`,
# `GET /{teacher_id}/schedule` and `GET /{teacher_id}/availability` endpoints that used
# to live in this file were dead code — confirmed via a frontend grep that every
# teacher-search/detail call goes through `/api/student/teachers/*`
# (app/routers/student_teachers.py) instead. They referenced non-existent model
# fields (TeacherProfile.id/.is_approved, Profile.is_active) and would have thrown
# on the first request had anything ever called them. Removed rather than fixed,
# same treatment as the dead app/routers/bookings.py in Phase 2.


def _delivery_options_for(teacher_id: UUID, db: Session) -> List[DeliveryOption]:
    rows = db.exec(
        select(TeacherDeliveryOption).where(TeacherDeliveryOption.teacher_id == teacher_id)
    ).all()
    return [DeliveryOption(mode=r.mode, type=r.type) for r in rows]


def _subjects_for(teacher_id: UUID, db: Session, settings) -> List[TeacherSubjectItem]:
    rows = db.exec(
        select(TeacherSubjectPrice, Subject, Level)
        .join(Subject, Subject.id == TeacherSubjectPrice.subject_id)
        .join(Level, Level.id == TeacherSubjectPrice.level_id)
        .where(TeacherSubjectPrice.teacher_id == teacher_id, TeacherSubjectPrice.active == True)
    ).all()
    items = []
    for tsp, subj, lvl in rows:
        packs = compute_pack_prices(tsp.price_single, settings)
        items.append(TeacherSubjectItem(
            subject_id=subj.id,
            subject_name=subj.name,
            level_id=lvl.id,
            level_code=lvl.code,
            level_name=lvl.label,
            price_single=tsp.price_single,
            price_pack5=packs["pack5"],
            price_pack10=packs["pack10"],
        ))
    return items


def _diplomas_for(teacher_id: UUID, db: Session) -> List[TeacherDiplomaItem]:
    rows = db.exec(select(TeacherDiploma).where(TeacherDiploma.teacher_id == teacher_id)).all()
    return [
        TeacherDiplomaItem(id=r.id, name=r.name, file_url=r.file_url, file_type=r.file_type, verified=r.verified)
        for r in rows
    ]


def _profile_to_detail(profile: TeacherProfile, user: Profile, db: Session) -> TeacherDetailResponse:
    settings = get_platform_settings(db)
    from app.services.boost import is_boost_active
    return TeacherDetailResponse(
        id=profile.user_id,
        user_id=profile.user_id,
        full_name=user.full_name or "",
        avatar_url=user.avatar_url,
        active_sticker_url=user.active_sticker_url,
        bio=profile.bio_long,
        headline=profile.headline,
        subjects=_subjects_for(profile.user_id, db, settings),
        price_per_session=profile.price_per_session,
        delivery_options=_delivery_options_for(profile.user_id, db),
        rating=profile.rating_avg,
        reviews_count=profile.reviews_count,
        badge=profile.badge,
        wilaya=user.wilaya,
        teaching_wilaya=profile.teaching_wilaya,
        teaching_wilayas=profile.teaching_wilayas or [],
        teaching_nationwide=profile.teaching_nationwide,
        languages=profile.languages or [],
        experience_years=profile.experience_years,
        success_rate=profile.success_rate,
        students_count=profile.students_count,
        hours_taught=profile.hours_taught,
        is_approved=(profile.status == "approved"),
        is_verified=profile.verified,
        diplomas=_diplomas_for(profile.user_id, db),
        boost_active=is_boost_active(profile),
        boost_expires_at=profile.boost_expires_at,
    )


@router.get("/me", response_model=TeacherDetailResponse)
async def get_my_teacher_profile(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Get the logged-in teacher's profile."""
    user = db.get(Profile, UUID(current_user["id"]))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    stmt2 = select(TeacherProfile).where(TeacherProfile.user_id == user.id)
    profile = db.exec(stmt2).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    from app.services.boost import clear_expired
    clear_expired(profile, db)

    return _profile_to_detail(profile, user, db)


@router.get("/me/boost", response_model=BoostStatusResponse)
async def get_my_boost_status(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Current visibility-boost status + the fixed server-side plans/pricing."""
    from app.services.boost import BOOST_PLANS, clear_expired, is_boost_active
    from app.services.kp import get_or_create_kp_account

    uid = UUID(current_user["id"])
    profile = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == uid)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    clear_expired(profile, db)
    account = get_or_create_kp_account(uid, db)

    return BoostStatusResponse(
        active=is_boost_active(profile),
        expires_at=profile.boost_expires_at,
        plans=BOOST_PLANS,
        balance=account.balance,
    )


@router.post("/me/boost", response_model=BoostStatusResponse)
async def activate_my_boost(
    payload: BoostActivateRequest,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Spend EP to activate (or extend) the teacher's visibility boost —
    real EP debit + real promotion in student search/recommendation ranking
    (see app.services.boost.is_boost_active, consumed by
    app.services.recommendation and app.routers.student_teachers)."""
    from app.services.boost import BOOST_PLANS, activate_boost, is_boost_active
    from app.services.kp import get_or_create_kp_account

    uid = UUID(current_user["id"])
    profile = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == uid)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    try:
        profile = activate_boost(profile, payload.days, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    account = get_or_create_kp_account(uid, db)
    return BoostStatusResponse(
        active=is_boost_active(profile),
        expires_at=profile.boost_expires_at,
        plans=BOOST_PLANS,
        balance=account.balance,
    )


_VALID_DELIVERY_MODES = {"online", "at_student", "at_home"}
_VALID_DELIVERY_TYPES = {"individual", "group"}

# Payment methods with no real-time gateway integration in this stack (see
# app/routers/admin/bookings.py) — a booking on one of these rails can only
# move out of "pending" via admin manual confirmation (approve/reject
# manual payment), never via the teacher's own accept/refuse. This closes a
# trust gap: without this guard a teacher could accept (and the student
# would be told the booking is "confirmed") before anyone had verified the
# cash/transfer payment was actually received.
#
# edahabia is NOT here: it's automated via Chargily Pay (real-time gateway,
# see app/services/chargily.py + app/routers/chargily_webhook.py) — its own
# gate is `booking.chargily_paid_at is not None`, checked directly in
# accept_booking/refuse_booking below, not this admin-manual-approval path.
_MANUAL_PAYMENT_METHODS = ("cash", "transfer")


@router.put("/me", response_model=TeacherDetailResponse)
async def update_my_teacher_profile(
    payload: TeacherProfileUpdate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Update the logged-in teacher's profile. Every field is optional/partial —
    only fields present in the request body are touched. `delivery_options` and
    `subjects`, when provided, fully replace the teacher's current set."""
    user = db.get(Profile, UUID(current_user["id"]))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    profile = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == user.id)).first()
    if profile is None:
        profile = TeacherProfile(user_id=user.id)

    if payload.headline is not None:
        profile.headline = payload.headline
    if payload.bio is not None:
        profile.bio_long = payload.bio
    if payload.experience_years is not None:
        profile.experience_years = payload.experience_years
    if payload.price_per_session is not None:
        profile.price_per_session = payload.price_per_session
    if payload.teaching_wilaya is not None:
        profile.teaching_wilaya = payload.teaching_wilaya
    if payload.teaching_wilayas is not None:
        profile.teaching_wilayas = payload.teaching_wilayas
    if payload.teaching_nationwide is not None:
        profile.teaching_nationwide = payload.teaching_nationwide
    if payload.languages is not None:
        profile.languages = payload.languages
    db.add(profile)
    db.commit()
    db.refresh(profile)

    # Delivery options: at-least-one rule + at_student-is-individual-only rule,
    # enforced here as defense in depth alongside the DB CHECK constraint
    # (migration 046) — see Spec B #8.
    if payload.delivery_options is not None:
        for opt in payload.delivery_options:
            if opt.mode not in _VALID_DELIVERY_MODES or opt.type not in _VALID_DELIVERY_TYPES:
                raise HTTPException(status_code=422, detail=f"Invalid delivery option: {opt.mode}/{opt.type}")
            if opt.mode == "at_student" and opt.type == "group":
                raise HTTPException(
                    status_code=422,
                    detail="Un cours 'chez l'élève' ne peut pas être en groupe (individuel uniquement).",
                )
        # De-duplicate before checking emptiness, so callers can't sneak past the
        # at-least-one rule with repeated identical entries.
        deduped = {(o.mode, o.type) for o in payload.delivery_options}
        if not deduped:
            raise HTTPException(
                status_code=422,
                detail="Au moins un mode d'enseignement doit être sélectionné.",
            )
        for row in db.exec(
            select(TeacherDeliveryOption).where(TeacherDeliveryOption.teacher_id == user.id)
        ).all():
            db.delete(row)
        db.commit()
        for mode, type_ in deduped:
            db.add(TeacherDeliveryOption(teacher_id=user.id, mode=mode, type=type_))
        db.commit()

    # Subjects: full replace against the caller-provided set only. Upsert by
    # name/code (same helpers onboarding uses) so the frontend never needs to
    # resolve subject/level UUIDs itself.
    if payload.subjects is not None:
        from app.routers.onboarding import _upsert_level, _upsert_subject

        for row in db.exec(
            select(TeacherSubjectPrice).where(TeacherSubjectPrice.teacher_id == user.id)
        ).all():
            db.delete(row)
        db.commit()
        for item in payload.subjects:
            subj_id = _upsert_subject(item.subject, db)
            level_id = _upsert_level(item.level, db)
            db.add(TeacherSubjectPrice(
                teacher_id=user.id,
                subject_id=subj_id,
                level_id=level_id,
                price_single=item.price_single,
            ))
        db.commit()

    db.refresh(profile)
    return _profile_to_detail(profile, user, db)


@router.post("/me/diplomas", response_model=DiplomaResponse, status_code=status.HTTP_201_CREATED)
async def upload_diploma(
    file: UploadFile,
    name: str = Query(...),
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Upload a diploma or certificate for the teacher."""
    from app.services.storage import upload_file

    contents = await file.read()
    url = upload_file(contents, file.filename or "diploma.pdf", file.content_type, folder="diplomas")

    user = db.get(Profile, UUID(current_user["id"]))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    profile = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == user.id)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    diploma = TeacherDiploma(teacher_id=user.id, name=name, file_url=url, file_type=file.content_type)
    db.add(diploma)
    db.commit()
    db.refresh(diploma)
    return DiplomaResponse(id=str(diploma.id), name=diploma.name, url=diploma.file_url or "")


@router.delete("/me/diplomas/{diploma_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_diploma(
    diploma_id: UUID,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Remove a diploma from the teacher's profile."""
    user = db.get(Profile, UUID(current_user["id"]))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    diploma = db.get(TeacherDiploma, diploma_id)
    if diploma is None or diploma.teacher_id != user.id:
        raise HTTPException(status_code=404, detail="Diploma not found")

    db.delete(diploma)
    db.commit()
    return None


@router.put("/me/payout-info")
async def update_payout_info(
    payload: TeacherPayoutInfoUpdate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Update where the teacher's earnings are sent — bank RIB/IBAN or the
    BaridiMob mobile wallet (never a full card number). Same validation as
    onboarding (app.services.payout), reused so both entry points can never
    disagree. Used by the teacher profile page's payout-destination editor
    and the wallet page's "Modifier" modal."""
    from app.services.payout import validate_payout_fields

    user = db.get(Profile, UUID(current_user["id"]))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    profile = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == user.id)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    rail = validate_payout_fields(
        payload.payout_rail, payload.iban, payload.bank_holder, payload.payout_phone, payload.bank_last4
    )

    profile.payout_rail = rail
    if rail == "bank":
        profile.iban = payload.iban.strip() if payload.iban else None
        profile.bank_holder = payload.bank_holder.strip() if payload.bank_holder else None
        profile.bank_last4 = payload.bank_last4.strip() if payload.bank_last4 else None
        profile.payout_phone = None
    else:
        profile.payout_phone = payload.payout_phone.strip() if payload.payout_phone else None
        profile.iban = None
        profile.bank_holder = None
        profile.bank_last4 = None

    db.add(profile)
    db.commit()
    db.refresh(profile)
    return {
        "payout_rail": profile.payout_rail,
        "iban": profile.iban,
        "bank_holder": profile.bank_holder,
        "bank_last4": profile.bank_last4,
        "payout_phone": profile.payout_phone,
    }


def _slot_subject_levels_for(slot_id: UUID, db: Session) -> List[SlotSubjectLevelResponse]:
    rows = db.exec(
        select(TeacherSlotSubject, Subject, Level)
        .join(Subject, Subject.id == TeacherSlotSubject.subject_id)
        .join(Level, Level.id == TeacherSlotSubject.level_id)
        .where(TeacherSlotSubject.slot_id == slot_id)
    ).all()
    return [
        SlotSubjectLevelResponse(
            subject_id=subj.id, subject_name=subj.name, level_id=lvl.id, level_name=lvl.label,
        )
        for _, subj, lvl in rows
    ]


def _validate_subject_levels(
    teacher_id: UUID, subject_levels: List[SlotSubjectLevel], db: Session
) -> None:
    """Every (subject_id, level_id) combo must already be an active row in this
    teacher's own subject catalog — a teacher can't offer a slot for something
    they haven't configured in their profile."""
    catalog = set(
        db.exec(
            select(TeacherSubjectPrice.subject_id, TeacherSubjectPrice.level_id).where(
                TeacherSubjectPrice.teacher_id == teacher_id,
                TeacherSubjectPrice.active == True,
            )
        ).all()
    )
    for combo in subject_levels:
        if (combo.subject_id, combo.level_id) not in catalog:
            subj = db.get(Subject, combo.subject_id)
            lvl = db.get(Level, combo.level_id)
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Vous n'enseignez pas « {subj.name if subj else combo.subject_id} — "
                    f"{lvl.label if lvl else combo.level_id} » dans votre profil. "
                    "Ajoutez cette matière/niveau avant de créer un créneau pour elle."
                ),
            )


def _validate_delivery_option(teacher_id: UUID, mode: str, type_: str, db: Session) -> None:
    """The (mode, type) pair for a slot must be compatible with what the
    teacher actually declared in their profile.

    mode/type can each be "both" ("peu importe" — the teacher doesn't mind
    which of their declared options applies; the student picks at booking
    time). A "both" axis simply isn't filtered on below, so the check
    becomes "the teacher has at least one declared option matching whatever
    IS fixed" instead of requiring one exact row.

    at_student is always individual-only — "both"/"group" are never valid
    for it, full stop (never presented by the create-slot form, but
    rejected here too as a safety net)."""
    if mode == "at_student" and type_ in ("group", "both"):
        raise HTTPException(
            status_code=422,
            detail="Les cours collectifs ne sont pas proposés chez l'élève (individuel uniquement).",
        )
    query = select(TeacherDeliveryOption).where(TeacherDeliveryOption.teacher_id == teacher_id)
    if mode != "both":
        query = query.where(TeacherDeliveryOption.mode == mode)
    if type_ != "both":
        query = query.where(TeacherDeliveryOption.type == type_)
    exists = db.exec(query).first()
    if exists is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Vous n'avez pas déclaré la capacité « {mode} / {type_} » dans votre profil. "
                "Modifiez vos préférences d'enseignement avant de créer ce créneau."
            ),
        )


def _validate_capacity(type_: str, max_students: int) -> None:
    """type_ "both" ("peu importe") must still support the group path (a
    student may end up booking it as a group session), so it needs the same
    capacity floor as "group"."""
    if type_ == "individual" and max_students != 1:
        raise HTTPException(
            status_code=422,
            detail="Un créneau individuel ne peut accueillir qu'un seul étudiant.",
        )
    if type_ in ("group", "both") and max_students < 2:
        raise HTTPException(
            status_code=422,
            detail="Un créneau collectif (ou « peu importe ») doit accepter au moins 2 étudiants.",
        )


def _check_overlap(
    teacher_id: UUID,
    slot_date,
    start_time,
    end_time,
    db: Session,
    exclude_slot_id: Optional[UUID] = None,
) -> None:
    # Slots are hard-deleted on cancellation (see delete_slot) — there is no
    # "cancelled" value in the slot_status enum (open/booked/blocked/draft),
    # so every remaining row for this date is relevant to overlap checking.
    existing = db.exec(
        select(TeacherSlot).where(
            TeacherSlot.teacher_id == teacher_id,
            TeacherSlot.slot_date == slot_date,
        )
    ).all()
    for other in existing:
        if exclude_slot_id and other.id == exclude_slot_id:
            continue
        if start_time < other.end_time and end_time > other.start_time:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Chevauchement avec un créneau existant "
                    f"({str(other.start_time)[:5]}–{str(other.end_time)[:5]})."
                ),
            )


def _slot_to_response(s: TeacherSlot, db: Session, student_name: Optional[str] = None) -> SlotResponse:
    from app.services.pricing import compute_pack_prices, get_platform_settings

    packs = compute_pack_prices(s.price, get_platform_settings(db))
    return SlotResponse(
        id=str(s.id),
        date=s.slot_date.isoformat(),
        start_time=str(s.start_time)[:5],
        end_time=str(s.end_time)[:5],
        type=s.type,
        max_students=s.max_students,
        mode=s.mode,
        price=s.price,
        price_pack5=packs["pack5"],
        price_pack10=packs["pack10"],
        status=s.status,
        student_name=student_name,
        subject_levels=_slot_subject_levels_for(s.id, db),
    )


@router.get("/me/slots", response_model=List[SlotResponse])
async def list_my_slots(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """List the teacher's slots, enriched with student name for booked slots."""
    import datetime as dt
    from app.models.booking import Booking
    from app.models.profile import Profile

    teacher_id = UUID(current_user["id"])
    today = dt.date.today()
    slots = db.exec(
        select(TeacherSlot)
        .where(TeacherSlot.teacher_id == teacher_id)
        .where(TeacherSlot.slot_date >= today)
        .order_by(TeacherSlot.slot_date, TeacherSlot.start_time)
    ).all()

    # Build slot_id → student_name map from confirmed bookings
    slot_ids = [s.id for s in slots if s.status == "booked"]
    student_name_map: dict = {}
    if slot_ids:
        bookings = db.exec(
            select(Booking)
            .where(Booking.slot_id.in_(slot_ids))
            .where(Booking.status == "confirmed")
        ).all()
        student_ids = [b.student_id for b in bookings]
        profiles = db.exec(select(Profile).where(Profile.id.in_(student_ids))).all() if student_ids else []
        prof_map = {p.id: p for p in profiles}
        for b in bookings:
            if b.slot_id:
                prof = prof_map.get(b.student_id)
                if prof:
                    student_name_map[b.slot_id] = prof.full_name or ""

    return [_slot_to_response(s, db, student_name_map.get(s.id)) for s in slots]


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

    _validate_capacity(payload.type, payload.max_students)
    _validate_delivery_option(teacher_id, payload.mode, payload.type, db)
    _validate_subject_levels(teacher_id, payload.subject_levels, db)
    _check_overlap(teacher_id, slot_date, start_time, end_time, db)

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

    for combo in payload.subject_levels:
        db.add(TeacherSlotSubject(slot_id=slot.id, subject_id=combo.subject_id, level_id=combo.level_id))
    db.commit()

    return _slot_to_response(slot, db)


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

    if slot.end_time <= slot.start_time:
        raise HTTPException(status_code=422, detail="end_time must be after start_time")
    _validate_capacity(slot.type, slot.max_students)
    _validate_delivery_option(teacher_id, slot.mode, slot.type, db)
    _check_overlap(teacher_id, slot.slot_date, slot.start_time, slot.end_time, db, exclude_slot_id=slot.id)

    if payload.subject_levels is not None:
        if len(payload.subject_levels) == 0:
            raise HTTPException(
                status_code=422,
                detail="Un créneau doit accepter au moins une matière/niveau.",
            )
        _validate_subject_levels(teacher_id, payload.subject_levels, db)

    db.add(slot)
    db.commit()
    db.refresh(slot)

    if payload.subject_levels is not None:
        existing = db.exec(
            select(TeacherSlotSubject).where(TeacherSlotSubject.slot_id == slot.id)
        ).all()
        for row in existing:
            db.delete(row)
        db.commit()
        for combo in payload.subject_levels:
            db.add(TeacherSlotSubject(slot_id=slot.id, subject_id=combo.subject_id, level_id=combo.level_id))
        db.commit()

    return _slot_to_response(slot, db)


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

    user = db.get(Profile, UUID(current_user["id"]))
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
        payout_rail=tp.payout_rail if tp else "bank",
        payout_phone=tp.payout_phone if tp else None,
    )


@router.get("/me/payments", response_model=List[TeacherPaymentItem])
async def list_my_payments(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Real per-lesson payment list for the teacher wallet page (replaces the
    old hardcoded ALL_TX list on the frontend).

    - "received" (green / "Reçu"): the TutoringSession is completed — its
      teacher_payout_amount has already been credited to wallet_balance_dzd
      (see POST /sessions/{id}/complete).
    - "pending" (orange / "En cours de réception"): the booking has been
      accepted by the teacher (status=confirmed) but this specific session
      hasn't completed yet — amount shown is the estimated per-lesson share.
    - Sessions whose booking is still "pending" (not yet accepted) or
      cancelled sessions are omitted entirely — nothing owed yet.
    """
    from app.models.booking import Booking, TutoringSession
    from app.services.pricing import PACK_SIZES

    uid = UUID(current_user["id"])
    sessions = db.exec(
        select(TutoringSession)
        .where(TutoringSession.teacher_id == uid)
        .order_by(TutoringSession.scheduled_at.desc())
    ).all()
    if not sessions:
        return []

    booking_ids = list({s.booking_id for s in sessions if s.booking_id})
    bookings = db.exec(select(Booking).where(Booking.id.in_(booking_ids))).all() if booking_ids else []
    booking_map = {b.id: b for b in bookings}

    student_ids = list({s.student_id for s in sessions})
    students = db.exec(select(Profile).where(Profile.id.in_(student_ids))).all() if student_ids else []
    student_map = {p.id: p for p in students}

    subject_ids = list({s.subject_id for s in sessions if s.subject_id})
    subjects = db.exec(select(Subject).where(Subject.id.in_(subject_ids))).all() if subject_ids else []
    subject_map = {s.id: s for s in subjects}

    items: List[TeacherPaymentItem] = []
    for s in sessions:
        booking = booking_map.get(s.booking_id) if s.booking_id else None
        if booking is None or booking.status == "pending":
            continue  # not yet accepted — nothing owed yet
        if s.status == "completed":
            payment_status = "received"
            amount = s.teacher_payout_amount
        elif s.status in ("cancelled",):
            continue  # refund logic (if any) is handled on the session itself, not payout
        elif booking.status == "confirmed":
            payment_status = "pending"
            amount = round(booking.amount / PACK_SIZES.get(booking.formula, 1))
        else:
            continue

        student = student_map.get(s.student_id)
        subj = subject_map.get(s.subject_id) if s.subject_id else None
        items.append(TeacherPaymentItem(
            session_id=s.id,
            booking_id=s.booking_id,
            student_name=(student.full_name if student else None) or "—",
            subject_name=subj.name if subj else None,
            scheduled_at=s.scheduled_at,
            formula=booking.formula,
            status=payment_status,
            amount=amount,
        ))
    return items


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
    """Accept a booking: capture Stripe payment. Wallet crediting happens per-lesson, on session completion."""
    from app.models.booking import Booking
    from app.services.stripe import capture_payment_intent, get_checkout_session

    teacher_id = UUID(current_user["id"])
    booking = db.get(Booking, booking_id)
    if booking is None or booking.teacher_id != teacher_id:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.status not in ("pending", "awaiting_teacher"):
        raise HTTPException(status_code=409, detail=f"Cannot accept a booking with status '{booking.status}'")
    if booking.payment_method in _MANUAL_PAYMENT_METHODS:
        raise HTTPException(
            status_code=409,
            detail=(
                "Ce mode de paiement (espèces / virement) doit d'abord être "
                "confirmé par l'administration avant de pouvoir accepter la réservation."
            ),
        )
    if booking.payment_method == "edahabia" and booking.chargily_paid_at is None:
        raise HTTPException(
            status_code=409,
            detail="L'élève n'a pas encore finalisé son paiement Edahabia — impossible d'accepter pour l'instant.",
        )

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

    # Mark the linked slot as booked so the teacher's calendar reflects it
    if booking.slot_id:
        slot = db.get(TeacherSlot, booking.slot_id)
        if slot and slot.teacher_id == teacher_id:
            slot.status = "booked"
            db.add(slot)

    # Wallet crediting no longer happens here. Payout is per-lesson, not
    # per-booking: accepting a booking unlocks it for scheduling, but each
    # lesson only credits the teacher's wallet when its own `TutoringSession`
    # is marked completed via POST /sessions/{id}/complete (see
    # app/routers/sessions.py) — Phase 2b of the implementation plan.
    db.commit()
    return {"status": "confirmed"}


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
    if booking.payment_method in _MANUAL_PAYMENT_METHODS:
        raise HTTPException(
            status_code=409,
            detail=(
                "Ce mode de paiement (espèces / virement) est géré par "
                "l'administration (approbation ou rejet du paiement) — pas par acceptation/refus direct."
            ),
        )

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

    # Chargily (edahabia) has no refund API — if the student already paid,
    # the money is already in the merchant account and can't be reversed
    # programmatically. Flag every admin so a manual refund gets processed
    # through the Chargily dashboard instead of silently vanishing.
    if booking.payment_method == "edahabia" and booking.chargily_paid_at is not None:
        from app.models.notification import Notification
        from app.models.profile import UserRole

        admin_roles = db.exec(select(UserRole).where(UserRole.role == "admin")).all()
        for ar in admin_roles:
            db.add(Notification(
                user_id=ar.user_id,
                type="system",
                title="⚠️ Remboursement Edahabia manuel requis",
                body=(
                    f"Le professeur a refusé une réservation payée en Edahabia ({booking.amount} DA). "
                    "Chargily ne permet pas le remboursement par API — traitez-le manuellement depuis "
                    "le tableau de bord Chargily."
                ),
                data={"booking_id": str(booking.id)},
            ))

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
    user = db.get(Profile, UUID(current_user["id"]))
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
    user = db.get(Profile, UUID(current_user["id"]))
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

    user = db.get(Profile, UUID(current_user["id"]))
    if user is None or review.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    review.rating = payload.rating
    review.comment = payload.comment
    db.add(review)
    db.commit()
    return {"message": "Evaluation updated"}


# `GET /{teacher_id}`, `/{teacher_id}/reviews`, `/{teacher_id}/schedule`,
# `/{teacher_id}/availability` used to live here — removed as dead code (see
# note near the top of this file). The equivalent, working, frontend-wired
# functionality is in app/routers/student_teachers.py under /api/student/teachers/*.


@router.get("/me/students-overview")
async def get_my_students_overview(
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Real per-student roster for the teacher 'Mes élèves' page (replaces the
    old hardcoded STUDENTS mock on the frontend).

    Groups every non-cancelled TutoringSession this teacher has had with each
    student — mirroring the students_taught/repeat_students grouping pattern
    from app/services/teacher_badge_engine.py. Per student we resolve:
    - the most frequent subject/level across their sessions together,
    - the completed session count,
    - the next upcoming session (if any),
    - a status ("active" if an upcoming session exists or the last completed
      session was within 30 days, "inactive" if older, "pending" if the
      student has only upcoming/no completed sessions yet),
    - a progress score from this teacher's Evaluation records for that
      student (score_global out of 20 -> percentage; 0 if none exist yet —
      the evaluation flow is real but may not have been used yet, which is
      the correct empty state rather than a fake number),
    - a trend comparing the two most recent evaluation scores.
    """
    from collections import Counter
    from datetime import datetime

    from app.models.booking import TutoringSession
    from app.models.evaluation import Evaluation

    teacher_id = UUID(current_user["id"])
    now = datetime.utcnow()

    sessions = db.exec(
        select(TutoringSession)
        .where(
            TutoringSession.teacher_id == teacher_id,
            TutoringSession.status.in_(["completed", "scheduled", "waiting", "live"]),
        )
        .order_by(TutoringSession.scheduled_at.asc())
    ).all()

    if not sessions:
        return []

    student_ids = list({s.student_id for s in sessions})
    students = db.exec(select(Profile).where(Profile.id.in_(student_ids))).all()
    student_map = {p.id: p for p in students}

    subject_ids = list({s.subject_id for s in sessions if s.subject_id})
    subjects = db.exec(select(Subject).where(Subject.id.in_(subject_ids))).all() if subject_ids else []
    subject_map = {s.id: s for s in subjects}

    level_ids = list({s.level_id for s in sessions if s.level_id})
    levels = db.exec(select(Level).where(Level.id.in_(level_ids))).all() if level_ids else []
    level_map = {lv.id: lv for lv in levels}

    evaluations = db.exec(
        select(Evaluation)
        .where(
            Evaluation.teacher_id == teacher_id,
            Evaluation.student_id.in_(student_ids),  # type: ignore[attr-defined]
        )
        .order_by(Evaluation.created_at.asc())
    ).all()
    evals_by_student: Dict[UUID, List[Evaluation]] = {}
    for e in evaluations:
        evals_by_student.setdefault(e.student_id, []).append(e)

    by_student: Dict[UUID, List[TutoringSession]] = {}
    for s in sessions:
        by_student.setdefault(s.student_id, []).append(s)

    result: List[Dict[str, Any]] = []
    for sid, s_sessions in by_student.items():
        profile = student_map.get(sid)
        if profile is None:
            continue

        completed = [s for s in s_sessions if s.status == "completed"]
        upcoming = [
            s for s in s_sessions
            if s.status in ("scheduled", "waiting", "live") and s.scheduled_at >= now
        ]

        subj_counter = Counter(s.subject_id for s in s_sessions if s.subject_id)
        top_subject_id = subj_counter.most_common(1)[0][0] if subj_counter else None
        subject_name = subject_map[top_subject_id].name if top_subject_id in subject_map else "—"

        lvl_counter = Counter(s.level_id for s in s_sessions if s.level_id)
        top_level_id = lvl_counter.most_common(1)[0][0] if lvl_counter else None
        level_label = level_map[top_level_id].label if top_level_id in level_map else "—"

        next_session = upcoming[0] if upcoming else None  # pre-sorted asc by scheduled_at

        if upcoming:
            status_val = "active"
        elif completed:
            last_completed_at = max(s.scheduled_at for s in completed)
            status_val = "active" if (now - last_completed_at).days <= 30 else "inactive"
        else:
            status_val = "pending"

        s_evals = evals_by_student.get(sid, [])
        scores = [e.score_global for e in s_evals if e.score_global is not None]
        progress = round((sum(scores) / len(scores)) / 20 * 100) if scores else 0

        trend = "flat"
        if len(scores) >= 2:
            if scores[-1] > scores[-2]:
                trend = "up"
            elif scores[-1] < scores[-2]:
                trend = "down"

        result.append({
            "student_id": str(sid),
            "full_name": profile.full_name or "—",
            "avatar_url": profile.avatar_url,
            "level": level_label,
            "subject": subject_name,
            "sessions": len(completed),
            "progress": progress,
            "next_session_at": next_session.scheduled_at.isoformat() if next_session else None,
            "trend": trend,
            "status": status_val,
        })

    result.sort(key=lambda r: r["full_name"])
    return result
