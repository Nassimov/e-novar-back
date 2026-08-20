from __future__ import annotations

import re
import unicodedata
from datetime import date as dt_date, date, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import func as sa_func, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.booking import Booking, BookingRefusal, TutoringSession
from app.models.catalog import Level, Subject, TeacherDeliveryOption, TeacherDiploma, TeacherSubjectPrice
from app.models.profile import Profile, StudentProfile, TeacherProfile
from app.models.review import Review
from app.models.scheduling import TeacherSlot, TeacherSlotSubject
from app.schemas.teacher import SlotSubjectLevelResponse
from app.services import matching
from app.services.boost import is_boost_active
from app.services.tenure import experience_years_from

router = APIRouter(tags=["Student"])

# LevelGroup ID → list of Level.code values that belong to that group
LEVEL_GROUP_CODES: dict[str, list[str]] = {
    "primary":    ["Primaire"],
    "middle":     ["BEM", "CEM", "3ème", "Moyen"],
    "high":       ["1AS", "2AS", "3AS", "BAC", "Terminale", "Bac"],
    "university": ["Université", "Licence", "Master", "Doctorat", "Univ"],
}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _make_teacher_slug(first_name: str, last_name: str, user_id: UUID) -> str:
    """Generate a URL-safe slug: prenom-nom-uuid8"""
    def slugify(s: str) -> str:
        s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
        return re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    fn = slugify(first_name) or "prof"
    ln = slugify(last_name) or "enovar"
    uid8 = str(user_id).replace("-", "")[:8]
    return f"{fn}-{ln}-{uid8}"


def _get_or_make_slug(tp: TeacherProfile, p: Profile) -> str:
    """Return stored slug or generate one on-the-fly (for teachers pre-migration)."""
    return tp.slug or _make_teacher_slug(p.first_name or "", p.last_name or "", tp.user_id)


def _resolve_teacher(db: Session, teacher_ref: str) -> TeacherProfile:
    """Look up a teacher by UUID (backwards-compat) or slug."""
    try:
        teacher_uuid = UUID(teacher_ref)
        tp = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == teacher_uuid)).first()
    except ValueError:
        tp = db.exec(select(TeacherProfile).where(TeacherProfile.slug == teacher_ref)).first()
    if tp is None or tp.status not in ("approved",):
        raise HTTPException(status_code=404, detail="Teacher not found")
    return tp


def _slot_subject_levels_for(slot_id: UUID, db: Session) -> List[SlotSubjectLevelResponse]:
    """All (subject, level) combos this slot accepts, with resolved names —
    student-facing mirror of the teacher-side helper in app.routers.teachers."""
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


def _first_subject_level_for_slot(slot_id: UUID, db: Session) -> tuple[Optional[UUID], Optional[UUID]]:
    """A slot can accept multiple subject/level combos (teacher_slot_subjects);
    when the caller hasn't confirmed a specific one (see _resolve_requested_subject_level),
    fall back to the first combo as the session's subject/level (best-effort metadata)."""
    row = db.exec(
        select(TeacherSlotSubject).where(TeacherSlotSubject.slot_id == slot_id)
    ).first()
    return (row.subject_id, row.level_id) if row else (None, None)


def _resolve_requested_subject_level(
    slot_id: Optional[UUID],
    requested_subject_id: Optional[UUID],
    requested_level_id: Optional[UUID],
    db: Session,
) -> tuple[Optional[UUID], Optional[UUID]]:
    """Validate an explicitly-requested (subject_id, level_id) against a slot's
    declared combos, or fall back to the slot's first combo when the caller
    didn't specify one (keeps clients that don't send a choice yet working).
    Raises 422 if the requested combo isn't one this slot actually accepts."""
    if not slot_id:
        return (requested_subject_id, requested_level_id)
    if requested_subject_id is None and requested_level_id is None:
        return _first_subject_level_for_slot(slot_id, db)
    combos = db.exec(
        select(TeacherSlotSubject).where(TeacherSlotSubject.slot_id == slot_id)
    ).all()
    for combo in combos:
        if combo.subject_id == requested_subject_id and combo.level_id == requested_level_id:
            return (combo.subject_id, combo.level_id)
    raise HTTPException(
        status_code=422,
        detail="Ce créneau n'accepte pas la matière/niveau demandé(e).",
    )


# ─── Booking safety rules ──────────────────────────────────────────────────────
# See docs/migrations/067_booking_safety_rules.sql for the full write-up of
# these three rules (race-safe slot claiming, refusal/no-response strikes,
# teacher no-response auto-cancel — the last one lives in
# app/workers/booking_tasks.py).

def _claim_slot_or_409(db: Session, slot_id: UUID, student_id: UUID) -> TeacherSlot:
    """
    Atomically claim a seat on a TeacherSlot, correct for both individual
    slots (max_students=1) and group slots (max_students>1) under
    concurrency: SELECT ... FOR UPDATE takes a row lock so two simultaneous
    requests for the same slot are serialized (the second sees the first
    request's just-added Booking row once it gets the lock), instead of both
    reading "1 seat free" and both succeeding — the exact double-booking race
    the student flagged.
    """
    slot = db.exec(
        select(TeacherSlot).where(TeacherSlot.id == slot_id).with_for_update()
    ).first()
    if slot is None or slot.status not in ("open",):
        raise HTTPException(
            status_code=409,
            detail="Ce créneau n'est plus disponible. Merci de rafraîchir et choisir un autre horaire.",
        )
    already = db.exec(
        select(sa_func.count()).select_from(Booking).where(
            Booking.slot_id == slot_id,
            Booking.student_id == student_id,
            Booking.status.in_(["pending", "confirmed"]),
        )
    ).one()
    if already:
        raise HTTPException(status_code=409, detail="Vous avez déjà réservé ce créneau.")
    active_count = db.exec(
        select(sa_func.count()).select_from(Booking).where(
            Booking.slot_id == slot_id,
            Booking.status.in_(["pending", "confirmed"]),
        )
    ).one()
    capacity = max(1, slot.max_students)
    if active_count >= capacity:
        raise HTTPException(status_code=409, detail="Ce créneau est complet.")
    if active_count + 1 >= capacity:
        slot.status = "booked"
        db.add(slot)
    return slot


def _check_no_slot_duplicate(
    db: Session, *, student_id: UUID, teacher_id: UUID, booking_date: date, slot_time,
) -> None:
    """Ad-hoc bookings (no published TeacherSlot to lock, e.g. a custom
    at_home/at_student time) have no row to serialize on — this is the best
    application-level guard for that case. docs/migrations/067 also adds a
    partial unique index as a last-resort DB-level backstop."""
    existing = db.exec(
        select(Booking).where(
            Booking.student_id == student_id,
            Booking.teacher_id == teacher_id,
            Booking.booking_date == booking_date,
            Booking.slot_time == slot_time,
            Booking.status.in_(["pending", "confirmed"]),
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="Vous avez déjà une réservation en attente ou confirmée pour cette date et cette heure avec ce professeur.",
        )


def _check_refusal_block(
    db: Session, *, student_id: UUID, teacher_id: UUID, subject_id: Optional[UUID],
    weekday: int, slot_time, threshold: int,
) -> None:
    """A teacher refusing (or never responding to) the same exact (subject,
    weekday, time) request `threshold` times means the student can no longer
    request that exact combo again with that teacher — they can still book
    any other slot, subject, or teacher."""
    count = db.exec(
        select(sa_func.count()).select_from(BookingRefusal).where(
            BookingRefusal.student_id == student_id,
            BookingRefusal.teacher_id == teacher_id,
            BookingRefusal.subject_id == subject_id,
            BookingRefusal.weekday == weekday,
            BookingRefusal.slot_time == slot_time,
        )
    ).one()
    if count >= threshold:
        raise HTTPException(
            status_code=409,
            detail=(
                "Ce professeur a déjà refusé (ou n'a pas répondu à) plusieurs de vos demandes "
                "pour ce créneau et cette matière précis. Vous ne pouvez plus le réserver à nouveau — "
                "choisissez un autre horaire, une autre matière ou un autre professeur."
            ),
        )


# ─── Search ──────────────────────────────────────────────────────────────────

class TeacherSearchItem(BaseModel):
    id: str
    slug: str
    first_name: str
    last_name: str
    full_name: str
    avatar_url: Optional[str] = None
    wilaya: Optional[str] = None
    bio: Optional[str] = None
    headline: Optional[str] = None
    price_per_session: int
    rating_avg: float
    reviews_count: int
    verified: bool
    sponsored: bool
    badge: Optional[str] = None
    kp_reward: int
    subjects: List[str]
    levels: List[str]
    modes: List[str]
    session_types: List[str]
    lesson_formats: List[str]
    subject_formats: Dict[str, str]
    languages: List[str] = []


class TeacherSearchResponse(BaseModel):
    items: List[TeacherSearchItem]
    total: int


@router.get("/teachers/search", response_model=TeacherSearchResponse)
async def student_teachers_search(
    q: Optional[str] = Query(None),
    subject: Optional[str] = Query(None),
    wilaya: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    session_type: Optional[str] = Query(None),
    price_min: Optional[int] = Query(None),
    price_max: Optional[int] = Query(None),
    min_rating: Optional[float] = Query(None),
    language: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wilaya_list: list[str] = [w.strip() for w in (wilaya or "").split(",") if w.strip()]
    level_list: list[str] = [l.strip() for l in (level or "").split(",") if l.strip()]
    language_list: list[str] = [l.strip().lower() for l in (language or "").split(",") if l.strip()]

    uid = UUID(current_user["id"])
    student_lesson_format: Optional[List[str]] = None
    try:
        sp_student = db.exec(select(StudentProfile).where(StudentProfile.user_id == uid)).first()
        if sp_student:
            student_lesson_format = sp_student.lesson_format
    except Exception:
        pass

    all_tp = db.exec(select(TeacherProfile).where(TeacherProfile.status == "approved")).all()
    if not all_tp:
        return TeacherSearchResponse(items=[], total=0)

    teacher_ids: list[UUID] = [tp.user_id for tp in all_tp]
    tp_map: dict[UUID, TeacherProfile] = {tp.user_id: tp for tp in all_tp}

    if mode:
        mode_rows = db.exec(
            select(TeacherDeliveryOption).where(
                TeacherDeliveryOption.teacher_id.in_(teacher_ids), TeacherDeliveryOption.mode == mode,
            )
        ).all()
        mode_set = {r.teacher_id for r in mode_rows}
        teacher_ids = [tid for tid in teacher_ids if tid in mode_set]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    if session_type:
        st_rows = db.exec(
            select(TeacherDeliveryOption).where(
                TeacherDeliveryOption.teacher_id.in_(teacher_ids), TeacherDeliveryOption.type == session_type,
            )
        ).all()
        st_set = {r.teacher_id for r in st_rows}
        teacher_ids = [tid for tid in teacher_ids if tid in st_set]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    sp_all = db.exec(
        select(TeacherSubjectPrice).where(
            TeacherSubjectPrice.teacher_id.in_(teacher_ids), TeacherSubjectPrice.active == True,  # noqa: E712
        )
    ).all()

    all_sub_ids = list({r.subject_id for r in sp_all})
    all_lev_ids = list({r.level_id for r in sp_all})
    sub_name_map: dict[UUID, str] = {}
    lev_code_map: dict[UUID, str] = {}
    lev_label_map: dict[UUID, str] = {}

    if all_sub_ids:
        subs = db.exec(select(Subject).where(Subject.id.in_(all_sub_ids))).all()
        sub_name_map = {s.id: s.name for s in subs}
    if all_lev_ids:
        levs = db.exec(select(Level).where(Level.id.in_(all_lev_ids))).all()
        lev_code_map = {l.id: l.code for l in levs}
        lev_label_map = {l.id: l.label for l in levs}

    def _dominant_format(types: set[str]) -> str:
        # Format is no longer a per-subject concept (Phase 4 dropped
        # TeacherSubjectPrice.lesson_format) — it's derived uniformly from the
        # teacher's global delivery-option session types (individual/group).
        has_individual = "individual" in types
        has_group = "group" in types
        if has_individual and has_group:
            return "both"
        if has_group:
            return "group"
        if has_individual:
            return "individual"
        return "both"  # no declared capability yet — neutral default, not exclusion

    teacher_subjects: dict[UUID, list[str]] = {}
    teacher_level_codes: dict[UUID, list[str]] = {}
    teacher_level_labels: dict[UUID, list[str]] = {}
    teacher_min_price: dict[UUID, int] = {}

    for r in sp_all:
        tid = r.teacher_id
        sn = sub_name_map.get(r.subject_id)
        lc = lev_code_map.get(r.level_id)
        ll = lev_label_map.get(r.level_id)
        if sn and sn not in teacher_subjects.get(tid, []):
            teacher_subjects.setdefault(tid, []).append(sn)
        if lc and lc not in teacher_level_codes.get(tid, []):
            teacher_level_codes.setdefault(tid, []).append(lc)
        if ll and ll not in teacher_level_labels.get(tid, []):
            teacher_level_labels.setdefault(tid, []).append(ll)
        if r.price_single > 0:
            if tid not in teacher_min_price or r.price_single < teacher_min_price[tid]:
                teacher_min_price[tid] = r.price_single

    if subject:
        subject_list = [s.strip() for s in subject.split(",") if s.strip()]
        if subject_list:
            norm_subs = {_norm(s) for s in subject_list}
            teacher_ids = [
                tid for tid in teacher_ids
                if any(_norm(s) in norm_subs for s in teacher_subjects.get(tid, []))
            ]
            if not teacher_ids:
                return TeacherSearchResponse(items=[], total=0)

    norm_codes: set[str] = set()
    if level_list:
        for lv in level_list:
            for code in LEVEL_GROUP_CODES.get(lv, []):
                norm_codes.add(_norm(code))
        teacher_ids = [
            tid for tid in teacher_ids
            if any(_norm(c) in norm_codes for c in teacher_level_codes.get(tid, []))
        ]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    # Resolve the requested subject/level names into catalog IDs so the
    # date-range check below can confirm an actually-bookable OPEN SLOT
    # carries this exact combo, not just "the teacher's general catalog
    # lists it somewhere" (Spec B #10 — combos must be used, not just stored).
    requested_subject_ids: set[UUID] = set()
    requested_level_ids: set[UUID] = set()
    if subject:
        norm_subs_req = {_norm(s) for s in [x.strip() for x in subject.split(",") if x.strip()]}
        requested_subject_ids = {
            r.subject_id for r in sp_all if _norm(sub_name_map.get(r.subject_id, "")) in norm_subs_req
        }
    if norm_codes:
        requested_level_ids = {
            r.level_id for r in sp_all if _norm(lev_code_map.get(r.level_id, "")) in norm_codes
        }

    profiles = db.exec(select(Profile).where(Profile.id.in_(teacher_ids))).all()
    prof_map: dict[UUID, Profile] = {p.id: p for p in profiles}

    if wilaya_list:
        norm_wilayas = {_norm(w) for w in wilaya_list}
        teacher_ids = [
            tid for tid in teacher_ids
            if matching.wilaya_compatible(tp_map[tid], prof_map.get(tid), norm_wilayas)
        ]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    if price_min is not None:
        teacher_ids = [
            tid for tid in teacher_ids
            if (teacher_min_price.get(tid) or tp_map[tid].price_per_session) >= price_min
        ]
    if price_max is not None:
        teacher_ids = [
            tid for tid in teacher_ids
            if (teacher_min_price.get(tid) or tp_map[tid].price_per_session) <= price_max
        ]
    if not teacher_ids:
        return TeacherSearchResponse(items=[], total=0)

    if min_rating is not None and min_rating > 0:
        teacher_ids = [tid for tid in teacher_ids if tp_map[tid].rating_avg >= min_rating]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    if language_list:
        teacher_ids = [
            tid for tid in teacher_ids
            if any(lang in (getattr(tp_map[tid], "languages", None) or []) for lang in language_list)
        ]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    if date_from or date_to:
        try:
            # If the student is filtering by subject/level, confirm an actual
            # OPEN slot in range carries that exact combo — not just that the
            # teacher's general catalog lists it. Falls back to plain
            # date-range slot existence when no subject/level filter is
            # active, unchanged from before.
            wants_combo_check = bool(requested_subject_ids or requested_level_ids)
            if wants_combo_check:
                slot_query = (
                    select(TeacherSlot)
                    .join(TeacherSlotSubject, TeacherSlotSubject.slot_id == TeacherSlot.id)
                    .where(TeacherSlot.teacher_id.in_(teacher_ids), TeacherSlot.status == "open")
                )
                if requested_subject_ids:
                    slot_query = slot_query.where(TeacherSlotSubject.subject_id.in_(requested_subject_ids))
                if requested_level_ids:
                    slot_query = slot_query.where(TeacherSlotSubject.level_id.in_(requested_level_ids))
            else:
                slot_query = select(TeacherSlot).where(
                    TeacherSlot.teacher_id.in_(teacher_ids), TeacherSlot.status == "open",
                )
            if date_from:
                try:
                    slot_query = slot_query.where(TeacherSlot.slot_date >= dt_date.fromisoformat(date_from))
                except ValueError:
                    pass
            if date_to:
                try:
                    slot_query = slot_query.where(TeacherSlot.slot_date <= dt_date.fromisoformat(date_to))
                except ValueError:
                    pass
            open_slots = db.exec(slot_query).all()
            teachers_with_slots = {s.teacher_id for s in open_slots}
            if teachers_with_slots:
                teacher_ids = [tid for tid in teacher_ids if tid in teachers_with_slots]
                if not teacher_ids:
                    return TeacherSearchResponse(items=[], total=0)
        except Exception:
            pass

    if q and q.strip():
        norm_q = _norm(q.strip())

        def q_matches(tid: UUID) -> bool:
            p = prof_map.get(tid)
            tp = tp_map.get(tid)
            if p and norm_q in _norm(p.full_name or ""):
                return True
            if tp and tp.headline and norm_q in _norm(tp.headline):
                return True
            return any(norm_q in _norm(s) for s in teacher_subjects.get(tid, []))

        teacher_ids = [tid for tid in teacher_ids if q_matches(tid)]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    delivery_all = db.exec(
        select(TeacherDeliveryOption).where(TeacherDeliveryOption.teacher_id.in_(teacher_ids))
    ).all()
    teacher_modes: dict[UUID, list[str]] = {}
    teacher_stypes: dict[UUID, list[str]] = {}
    teacher_delivery_pairs: dict[UUID, set[tuple[str, str]]] = {}
    for d in delivery_all:
        teacher_modes.setdefault(d.teacher_id, [])
        if d.mode not in teacher_modes[d.teacher_id]:
            teacher_modes[d.teacher_id].append(d.mode)
        teacher_stypes.setdefault(d.teacher_id, [])
        if d.type not in teacher_stypes[d.teacher_id]:
            teacher_stypes[d.teacher_id].append(d.type)
        teacher_delivery_pairs.setdefault(d.teacher_id, set()).add((d.mode, d.type))

    def _format_compatible(tid: UUID) -> bool:
        return matching.lesson_format_compatible(
            teacher_delivery_pairs.get(tid, set()), student_lesson_format
        )

    def rank_score(tid: UUID) -> float:
        tp = tp_map[tid]
        s = tp.rating_avg * max(tp.reviews_count, 1)
        if tp.verified:
            s += 5.0
        if _format_compatible(tid):
            s += 3.0
        return s

    teacher_ids_sorted = sorted(
        teacher_ids,
        key=lambda tid: (1 if is_boost_active(tp_map[tid]) else 0, rank_score(tid)),
        reverse=True,
    )

    items: list[TeacherSearchItem] = []
    for tid in teacher_ids_sorted:
        p = prof_map.get(tid)
        tp = tp_map.get(tid)
        if not p or not tp:
            continue
        items.append(TeacherSearchItem(
            id=str(tid),
            slug=_get_or_make_slug(tp, p),
            first_name=p.first_name or "",
            last_name=p.last_name or "",
            full_name=p.full_name or "",
            avatar_url=p.avatar_url,
            wilaya=tp.teaching_wilaya or p.wilaya,
            bio=tp.bio_long or p.bio,
            headline=tp.headline,
            price_per_session=teacher_min_price.get(tid) or tp.price_per_session,
            rating_avg=round(tp.rating_avg, 1),
            reviews_count=tp.reviews_count,
            verified=tp.verified,
            sponsored=is_boost_active(tp),
            badge=tp.badge,
            kp_reward=tp.kp_reward,
            subjects=teacher_subjects.get(tid, []),
            levels=teacher_level_labels.get(tid, []),
            modes=teacher_modes.get(tid, []),
            session_types=teacher_stypes.get(tid, []),
            lesson_formats=[_dominant_format(set(teacher_stypes.get(tid, [])))],
            subject_formats={
                sn: _dominant_format(set(teacher_stypes.get(tid, [])))
                for sn in teacher_subjects.get(tid, [])
            },
            languages=getattr(tp_map.get(tid), "languages", None) or [],
        ))

    return TeacherSearchResponse(items=items, total=len(items))


# ─── Teacher public profile ───────────────────────────────────────────────────

class TeacherSubjectItem(BaseModel):
    subject_id: str
    level_id: str
    name: str
    level: str
    price: int


class TeacherDiplomaItem(BaseModel):
    id: str
    name: str
    file_url: Optional[str] = None
    file_type: Optional[str] = None
    verified: bool = False


class TeacherPublicProfile(BaseModel):
    id: str
    slug: str
    first_name: str
    last_name: str
    full_name: str
    avatar_url: Optional[str] = None
    active_sticker_url: Optional[str] = None
    bio: Optional[str] = None
    headline: Optional[str] = None
    wilaya: Optional[str] = None
    price_per_session: int
    kp_reward: int
    rating_avg: float
    reviews_count: int
    verified: bool
    badge: Optional[str] = None
    experience_years: int
    success_rate: Optional[float] = None
    students_count: int
    hours_taught: int
    subjects: List[TeacherSubjectItem]
    modes: List[str]
    session_types: List[str]
    diplomas: List[TeacherDiplomaItem]


class TeacherReviewItem(BaseModel):
    id: str
    reviewer_name: str
    rating: int
    comment: Optional[str] = None
    created_at: str


class TeacherSlotItem(BaseModel):
    id: str
    slot_date: str
    start_time: str
    end_time: str
    mode: str
    type: str
    max_students: int = 1
    price: int = 0
    price_pack5: int = 0   # computed from `price` + platform discount config, never stored
    price_pack10: int = 0  # computed from `price` + platform discount config, never stored
    subject_levels: List[SlotSubjectLevelResponse] = []


@router.get("/teachers/{teacher_ref}", response_model=TeacherPublicProfile)
async def get_teacher_profile(
    teacher_ref: str,
    db: Session = Depends(get_db),
    _: Dict[str, Any] = Depends(get_current_user),
):
    tp = _resolve_teacher(db, teacher_ref)
    teacher_id = tp.user_id

    p = db.get(Profile, teacher_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    sp_rows = db.exec(
        select(TeacherSubjectPrice, Subject, Level)
        .join(Subject, Subject.id == TeacherSubjectPrice.subject_id)
        .join(Level, Level.id == TeacherSubjectPrice.level_id)
        .where(TeacherSubjectPrice.teacher_id == teacher_id)
        .where(TeacherSubjectPrice.active == True)  # noqa: E712
    ).all()
    subjects = [
        TeacherSubjectItem(
            subject_id=str(subj.id),
            level_id=str(lvl.id),
            name=subj.name,
            level=lvl.label,
            price=tsp.price_single,
        )
        for tsp, subj, lvl in sp_rows
    ]

    delivery_rows = db.exec(
        select(TeacherDeliveryOption).where(TeacherDeliveryOption.teacher_id == teacher_id)
    ).all()
    modes = list({d.mode for d in delivery_rows})
    session_types = list({d.type for d in delivery_rows})

    diploma_rows = db.exec(select(TeacherDiploma).where(TeacherDiploma.teacher_id == teacher_id)).all()
    diplomas = [
        TeacherDiplomaItem(
            id=str(d.id), name=d.name, file_url=d.file_url,
            file_type=d.file_type, verified=d.verified,
        )
        for d in diploma_rows
    ]

    return TeacherPublicProfile(
        id=str(teacher_id),
        slug=_get_or_make_slug(tp, p),
        first_name=p.first_name or "",
        last_name=p.last_name or "",
        full_name=p.full_name or "",
        avatar_url=p.avatar_url,
        active_sticker_url=p.active_sticker_url,
        bio=tp.bio_long or p.bio,
        headline=tp.headline,
        wilaya=tp.teaching_wilaya or p.wilaya,
        price_per_session=tp.price_per_session,
        kp_reward=tp.kp_reward,
        rating_avg=round(tp.rating_avg, 1),
        reviews_count=tp.reviews_count,
        verified=tp.verified,
        badge=tp.badge,
        experience_years=experience_years_from(tp.created_at),
        success_rate=tp.success_rate,
        students_count=tp.students_count,
        hours_taught=tp.hours_taught,
        subjects=subjects,
        modes=modes if modes else ["online"],
        session_types=session_types if session_types else ["individual"],
        diplomas=diplomas,
    )


@router.get("/teachers/{teacher_ref}/reviews")
async def get_teacher_reviews(
    teacher_ref: str,
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _: Dict[str, Any] = Depends(get_current_user),
):
    tp = _resolve_teacher(db, teacher_ref)
    teacher_id = tp.user_id

    reviews = db.exec(
        select(Review)
        .where(Review.teacher_id == teacher_id)
        .where(Review.status == "visible")
        .order_by(Review.created_at.desc())
    ).all()

    reviewer_ids = list({r.student_id for r in reviews})
    profiles = db.exec(select(Profile).where(Profile.id.in_(reviewer_ids))).all()
    prof_map = {p.id: p for p in profiles}

    total = len(reviews)
    offset = (page - 1) * size
    page_reviews = reviews[offset: offset + size]

    items = []
    for r in page_reviews:
        reviewer = prof_map.get(r.student_id)
        if reviewer:
            name_parts = (reviewer.full_name or "").split()
            reviewer_name = (
                f"{name_parts[0]} {name_parts[-1][0]}." if len(name_parts) >= 2
                else reviewer.full_name or "Étudiant"
            )
        else:
            reviewer_name = "Étudiant"
        items.append(TeacherReviewItem(
            id=str(r.id),
            reviewer_name=reviewer_name,
            rating=r.rating,
            comment=r.comment,
            created_at=r.created_at.isoformat(),
        ))

    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/teachers/{teacher_ref}/slots")
async def get_teacher_slots(
    teacher_ref: str,
    db: Session = Depends(get_db),
    _: Dict[str, Any] = Depends(get_current_user),
):
    tp = _resolve_teacher(db, teacher_ref)
    teacher_id = tp.user_id
    today = date.today()
    end = today + timedelta(days=30)

    slots = db.exec(
        select(TeacherSlot)
        .where(TeacherSlot.teacher_id == teacher_id)
        .where(TeacherSlot.status == "open")
        .where(TeacherSlot.slot_date >= today)
        .where(TeacherSlot.slot_date <= end)
        .order_by(TeacherSlot.slot_date, TeacherSlot.start_time)
    ).all()

    from app.services.pricing import compute_pack_prices, get_platform_settings

    settings = get_platform_settings(db)
    items = []
    for s in slots:
        packs = compute_pack_prices(s.price, settings)
        items.append(TeacherSlotItem(
            id=str(s.id),
            slot_date=s.slot_date.isoformat(),
            start_time=str(s.start_time)[:5],
            end_time=str(s.end_time)[:5],
            mode=s.mode,
            type=s.type,
            max_students=s.max_students,
            price=s.price,
            price_pack5=packs["pack5"],
            price_pack10=packs["pack10"],
            subject_levels=_slot_subject_levels_for(s.id, db),
        ))
    return items


# ─── Student reviews (authenticated student) ─────────────────────────────────

class MyReviewItem(BaseModel):
    id: str
    session_id: str
    rating: int
    comment: Optional[str] = None
    created_at: str


class EligibleSession(BaseModel):
    id: str
    scheduled_at: str
    subject: Optional[str] = None


class MyReviewsResponse(BaseModel):
    my_reviews: List[MyReviewItem]
    eligible_sessions: List[EligibleSession]


class ReviewSubmitBody(BaseModel):
    session_id: UUID
    rating: int = PydanticField(ge=1, le=5)
    comment: Optional[str] = None


@router.get("/teachers/{teacher_ref}/my-reviews", response_model=MyReviewsResponse)
async def get_my_reviews(
    teacher_ref: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return this student's existing reviews + sessions still eligible for review."""
    tp = _resolve_teacher(db, teacher_ref)
    teacher_id = tp.user_id
    student_id = UUID(current_user["id"])

    # Completed sessions between this student and teacher
    sessions = db.exec(
        select(TutoringSession)
        .where(TutoringSession.student_id == student_id)
        .where(TutoringSession.teacher_id == teacher_id)
        .where(TutoringSession.status == "completed")
        .order_by(TutoringSession.scheduled_at.desc())
    ).all()

    if not sessions:
        return MyReviewsResponse(my_reviews=[], eligible_sessions=[])

    session_ids = [s.id for s in sessions]
    session_map = {s.id: s for s in sessions}

    # Existing reviews by this student for this teacher
    existing_reviews = db.exec(
        select(Review)
        .where(Review.student_id == student_id)
        .where(Review.teacher_id == teacher_id)
        .where(Review.session_id.in_(session_ids))
    ).all()
    reviewed_session_ids = {r.session_id for r in existing_reviews}

    # Sessions that can still be reviewed
    eligible_ids = [sid for sid in session_ids if sid not in reviewed_session_ids]

    subject_ids = list({session_map[sid].subject_id for sid in eligible_ids if session_map[sid].subject_id})
    subject_name_map: dict[UUID, str] = {}
    if subject_ids:
        subjs = db.exec(select(Subject).where(Subject.id.in_(subject_ids))).all()
        subject_name_map = {s.id: s.name for s in subjs}

    return MyReviewsResponse(
        my_reviews=[
            MyReviewItem(
                id=str(r.id),
                session_id=str(r.session_id),
                rating=r.rating,
                comment=r.comment,
                created_at=r.created_at.isoformat(),
            )
            for r in existing_reviews
        ],
        eligible_sessions=[
            EligibleSession(
                id=str(sid),
                scheduled_at=session_map[sid].scheduled_at.isoformat(),
                subject=subject_name_map.get(session_map[sid].subject_id),
            )
            for sid in eligible_ids
        ],
    )


class PackSessionItem(BaseModel):
    date: str                          # "YYYY-MM-DD"
    slot_id: Optional[str] = None
    slot_time: str                     # "HH:MM"
    end_time: Optional[str] = None    # "HH:MM"
    session_type: str = "individual"  # "individual" | "group"
    subject: Optional[str] = None
    subject_id: Optional[UUID] = None  # must be one of the slot's declared combos, if provided
    level_id: Optional[UUID] = None
    comment: Optional[str] = None


class BookingBody(BaseModel):
    slot_id: Optional[str] = None
    formula: str = "single"
    mode: str = "online"
    date: Optional[str] = None             # "YYYY-MM-DD" — required for single; inferred from pack_sessions[0] for packs
    slot_time: Optional[str] = None       # "HH:MM"
    end_time: Optional[str] = None        # "HH:MM" (for sub-slot selection)
    duration_min: int = 90
    payment_method: str = "cib"           # cib | edahabia | transfer | cash | rib_cib | rib_edahabia
    # Snapshot of the student's RIB at booking time (rib_cib/rib_edahabia
    # only) — stored on the booking so admin reconciliation (see
    # app/routers/admin/bookings.py) reflects whichever RIB was actually
    # used, even if the student later changes their saved default.
    payment_rib: Optional[str] = None
    subject: Optional[str] = None
    subject_id: Optional[UUID] = None      # must be one of the slot's declared combos, if provided
    level_id: Optional[UUID] = None
    comment: Optional[str] = None
    session_type: Optional[str] = None    # override for "both" type slots
    pack_sessions: Optional[List[PackSessionItem]] = None  # for pack5/pack10


@router.post("/teachers/{teacher_ref}/book", status_code=201)
async def book_teacher_slot(
    teacher_ref: str,
    body: BookingBody,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a booking for the given teacher.
    - For payment_method=cib: creates Stripe Checkout Session, returns checkout_url.
    - For payment_method=edahabia|transfer|cash: creates pending booking, returns booking_id.
    - For formula=pack5|pack10: body.pack_sessions must contain all sessions.

    The charged amount is always computed server-side from the teacher's
    price_single plus the current admin-configured pack discounts
    (app.services.pricing) — the client cannot supply or influence the price.
    Returns { booking_id, checkout_url|None, amount, payment_method }.
    """
    import datetime as dt
    import json
    from uuid import UUID as _UUID
    from app.config import get_settings
    from app.models.booking import Booking
    from app.services.pricing import compute_pack_prices, get_platform_settings

    settings = get_settings()
    tp = _resolve_teacher(db, teacher_ref)
    teacher_id = tp.user_id
    student_id = _UUID(current_user["id"])

    # Repeated no-shows temporarily block NEW bookings only (see
    # app/services/booking_safety.py's apply_student_strike) — the student
    # keeps full access to their account/history/messages throughout.
    student_profile = db.get(StudentProfile, student_id)
    if student_profile is not None and student_profile.booking_suspended_until:
        if student_profile.booking_suspended_until > dt.datetime.now(dt.timezone.utc):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Réservations temporairement bloquées suite à des absences répétées. "
                    f"Réactivation le {student_profile.booking_suspended_until.strftime('%d/%m/%Y à %Hh%M')}."
                ),
            )
        student_profile.booking_suspended_until = None
        db.add(student_profile)
        db.commit()

    # Parse date — infer from first pack session when omitted
    raw_date = body.date
    if not raw_date and body.pack_sessions:
        raw_date = body.pack_sessions[0].date
    if not raw_date:
        raise HTTPException(status_code=422, detail="date is required for single-session bookings")
    try:
        booking_date = dt.date.fromisoformat(raw_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format — use YYYY-MM-DD")

    # Parse slot time
    slot_time: Optional[dt.time] = None
    if body.slot_time:
        try:
            slot_time = dt.time.fromisoformat(body.slot_time)
        except ValueError:
            pass

    # Validate / resolve slot
    slot_id: Optional[_UUID] = None
    if body.slot_id:
        try:
            slot_id = _UUID(body.slot_id)
            slot = db.get(TeacherSlot, slot_id)
            if slot and slot.status == "open":
                booking_date = slot.slot_date
                slot_time = slot.start_time
        except (ValueError, Exception):
            pass

    # Resolve session_type:
    # - if slot type is "both", use student's choice; default "individual"
    # - otherwise use slot's type; default "individual"
    resolved_session_type = "individual"
    if body.session_type in ("individual", "group"):
        resolved_session_type = body.session_type
    elif slot_id:
        s = db.get(TeacherSlot, slot_id)
        if s and s.type not in ("both",):
            resolved_session_type = s.type

    if body.formula not in ("single", "pack5", "pack10"):
        raise HTTPException(status_code=422, detail="formula must be one of: single, pack5, pack10")

    # Re-validate delivery capability server-side — the student may have
    # searched a while ago, and the teacher's declared capabilities can have
    # changed since. Never trust a stale search result.
    if not matching.delivery_option_compatible(db, teacher_id, body.mode, resolved_session_type):
        raise HTTPException(
            status_code=422,
            detail="Ce professeur ne propose pas ce format de cours (mode ou type de séance non disponible).",
        )

    if body.mode == "at_student":
        student_profile = db.get(Profile, student_id)
        student_wilaya = student_profile.wilaya if student_profile else None
        teacher_base_profile = db.get(Profile, teacher_id)
        if not student_wilaya or not matching.wilaya_compatible(tp, teacher_base_profile, student_wilaya):
            raise HTTPException(
                status_code=422,
                detail="Ce professeur ne se déplace pas dans votre wilaya.",
            )

    # Fail fast (before creating anything) if the client explicitly named a
    # subject/level that this slot doesn't actually offer — a slot can accept
    # several subject/level combos, and the client is never trusted to name
    # one it wasn't shown.
    if body.subject_id is not None or body.level_id is not None:
        _resolve_requested_subject_level(slot_id, body.subject_id, body.level_id, db)
    for ps in (body.pack_sessions or []):
        if ps.subject_id is not None or ps.level_id is not None:
            ps_slot_uuid = _UUID(ps.slot_id) if ps.slot_id else None
            _resolve_requested_subject_level(ps_slot_uuid, ps.subject_id, ps.level_id, db)

    # ── Booking safety rules — race-safe capacity claim, duplicate check, and
    # the refusal/no-response strike gate. Must run before we create anything
    # (including Stripe/Chargily checkout sessions further down) so a
    # rejected request never has payment-gateway side effects.
    platform_settings_for_gate = get_platform_settings(db)
    refusal_threshold = platform_settings_for_gate.booking_refusal_block_threshold

    def _gate_one(gate_slot_id: Optional[UUID], gate_date: date, gate_time, gate_subject_id: Optional[UUID], gate_level_id: Optional[UUID]) -> None:
        resolved_subject_id, _ = _resolve_requested_subject_level(gate_slot_id, gate_subject_id, gate_level_id, db)
        _check_refusal_block(
            db,
            student_id=student_id,
            teacher_id=teacher_id,
            subject_id=resolved_subject_id,
            weekday=gate_date.weekday(),
            slot_time=gate_time,
            threshold=refusal_threshold,
        )
        if gate_slot_id:
            _claim_slot_or_409(db, gate_slot_id, student_id)
        elif gate_time is not None:
            _check_no_slot_duplicate(db, student_id=student_id, teacher_id=teacher_id, booking_date=gate_date, slot_time=gate_time)

    if body.pack_sessions:
        for ps in body.pack_sessions:
            ps_slot_uuid = _UUID(ps.slot_id) if ps.slot_id else None
            try:
                ps_gate_time = dt.time.fromisoformat(ps.slot_time)
            except ValueError:
                ps_gate_time = None
            try:
                ps_gate_date = dt.date.fromisoformat(ps.date)
            except ValueError:
                ps_gate_date = booking_date
            _gate_one(ps_slot_uuid, ps_gate_date, ps_gate_time, ps.subject_id, ps.level_id)
    else:
        _gate_one(slot_id, booking_date, slot_time, body.subject_id, body.level_id)

    # Resolve the authoritative single-lesson price. Priority:
    #   1. slot-level price, when the teacher set one for this specific slot
    #   2. the teacher's per-subject/level price (TeacherSubjectPrice) — this
    #      is what the student was actually shown on the teacher profile /
    #      payment preview page (student.teacher.$teacherId.tsx's `combo.price`),
    #      so it MUST be consulted here too, otherwise a "book without picking
    #      a slot" flow silently falls through to price_per_session (a
    #      separate, often-unset/stale field defaulting to 0) and the Stripe
    #      Checkout Session ends up floored to the 50-cent minimum instead of
    #      the amount actually previewed to the student.
    #   3. the teacher's generic default rate, as a last resort.
    # The client never supplies or influences this.
    resolved_slot = db.get(TeacherSlot, slot_id) if slot_id else None
    slot_price = getattr(resolved_slot, "price", 0) if resolved_slot else 0

    pricing_subject_id = body.subject_id
    pricing_level_id = body.level_id
    if pricing_subject_id is None and pricing_level_id is None and body.pack_sessions:
        pricing_subject_id = body.pack_sessions[0].subject_id
        pricing_level_id = body.pack_sessions[0].level_id

    subject_price = 0
    if not resolved_slot and pricing_subject_id is not None and pricing_level_id is not None:
        tsp = db.exec(
            select(TeacherSubjectPrice).where(
                TeacherSubjectPrice.teacher_id == teacher_id,
                TeacherSubjectPrice.subject_id == pricing_subject_id,
                TeacherSubjectPrice.level_id == pricing_level_id,
                TeacherSubjectPrice.active == True,  # noqa: E712
            )
        ).first()
        subject_price = tsp.price_single if tsp else 0

    price_single = slot_price if slot_price > 0 else (subject_price if subject_price > 0 else tp.price_per_session)

    pack_prices = compute_pack_prices(price_single, get_platform_settings(db))
    # A group ("collective") session is always priced lower than an
    # individual single session — the admin-configured group discount off
    # price_single (see app.services.pricing) — regardless of `formula`,
    # which stays "single" for a one-off group booking (packs are
    # individual-only, see student.booking.session-type.tsx).
    amount = pack_prices["group"] if resolved_session_type == "group" else pack_prices[body.formula]

    # A pack purchase must name exactly as many sessions as the pack contains.
    required_sessions = {"single": None, "pack5": 5, "pack10": 10}[body.formula]
    if required_sessions is not None:
        if not body.pack_sessions or len(body.pack_sessions) != required_sessions:
            raise HTTPException(
                status_code=422,
                detail=f"formula={body.formula} requires exactly {required_sessions} pack_sessions entries",
            )

    # Serialize pack_sessions if provided
    pack_sessions_json: Optional[str] = None
    if body.pack_sessions:
        pack_sessions_json = json.dumps([s.model_dump() for s in body.pack_sessions])

    # Determine booking status based on payment method
    # - cash: pending admin approval then teacher acceptance
    # - edahabia/transfer: pending (payment promise)
    # - cib: pending (becomes confirmed after Stripe capture + teacher accept)
    booking_status = "pending"

    # Create booking
    booking = Booking(
        student_id=student_id,
        teacher_id=teacher_id,
        slot_id=slot_id,
        formula=body.formula,
        mode=body.mode,
        session_type=resolved_session_type,
        booking_date=booking_date,
        slot_time=slot_time,
        duration_min=body.duration_min,
        amount=amount,
        kp_reward=tp.kp_reward,
        status=booking_status,
        payment_method=body.payment_method,
        payment_rib=body.payment_rib if body.payment_method in ("rib_cib", "rib_edahabia") else None,
        subject=body.subject,
        comment=body.comment,
        pack_sessions=pack_sessions_json,
    )
    db.add(booking)
    db.flush()  # get booking.id without committing

    checkout_url: Optional[str] = None

    if body.payment_method == "cib":
        # Create Stripe Checkout Session
        from app.services.stripe import create_checkout_session
        teacher_profile_row = db.get(Profile, teacher_id)
        teacher_name = teacher_profile_row.full_name if teacher_profile_row else "Professeur E-NOVAR"
        base_url = settings.frontend_url or "http://localhost:5173"
        try:
            session = create_checkout_session(
                amount_dzd=amount,
                dzd_per_eur=platform_settings_for_gate.dzd_per_eur,
                booking_id=str(booking.id),
                teacher_name=teacher_name,
                success_url=f"{base_url}/student/payment/process?session_id={{CHECKOUT_SESSION_ID}}&status=success",
                cancel_url=f"{base_url}/student/payment?cancelled=1",
            )
            booking.stripe_cs_id = session["session_id"]
            checkout_url = session["url"]
        except Exception:
            # Stripe not configured — proceed anyway, booking stays pending
            checkout_url = f"{base_url}/student/payment/process?status=success&booking_id={booking.id}"

    elif body.payment_method == "edahabia":
        # Create a Chargily Checkout — real Edahabia rail. Chargily charges
        # immediately on completion (no auth/capture split); the webhook
        # (app/routers/chargily_webhook.py) sets chargily_paid_at, which is
        # what gates the teacher's accept_booking for this booking.
        from app.services.chargily import create_checkout as chargily_create_checkout
        base_url = settings.frontend_url or "http://localhost:5173"
        api_base_url = settings.app_url or base_url
        try:
            checkout = chargily_create_checkout(
                amount_dzd=amount,
                booking_id=str(booking.id),
                payment_method="edahabia",
                success_url=f"{base_url}/student/payment/process?status=success&booking_id={booking.id}",
                failure_url=f"{base_url}/student/payment?cancelled=1",
                webhook_url=f"{api_base_url}/api/payments/chargily/webhook",
                description=f"E-NOVAR — séance avec {teacher_id}",
            )
            booking.chargily_checkout_id = checkout["id"]
            checkout_url = checkout["checkout_url"]
        except Exception:
            # Chargily not configured (no CHARGILY_SECRET_KEY yet) — proceed
            # anyway, booking stays pending until an admin can confirm it
            # manually the same way cash/transfer are handled.
            checkout_url = None

    # Slot(s) were already atomically claimed above (_gate_one/_claim_slot_or_409)
    # before any payment-gateway session was created — nothing left to do here.

    # Materialize one TutoringSession row per lesson in the booking — this is
    # what /sessions/{id}/complete later credits payout against, one lesson
    # at a time, even for packs (see PACK_SIZES in app.services.pricing).
    created_sessions: List[TutoringSession] = []
    if body.pack_sessions:
        for ps in body.pack_sessions:
            ps_subject_id = None
            ps_level_id = None
            if ps.slot_id:
                try:
                    ps_subject_id, ps_level_id = _resolve_requested_subject_level(
                        _UUID(ps.slot_id), ps.subject_id, ps.level_id, db
                    )
                except ValueError:
                    pass
            try:
                ps_time = dt.time.fromisoformat(ps.slot_time)
            except ValueError:
                ps_time = dt.time(0, 0)
            try:
                ps_date = dt.date.fromisoformat(ps.date)
            except ValueError:
                ps_date = booking_date
            ps_session = TutoringSession(
                booking_id=booking.id,
                teacher_id=teacher_id,
                student_id=student_id,
                subject_id=ps_subject_id,
                level_id=ps_level_id,
                scheduled_at=dt.datetime.combine(ps_date, ps_time),
                mode=body.mode,
                status="scheduled",
            )
            db.add(ps_session)
            created_sessions.append(ps_session)
    else:
        single_subject_id, single_level_id = (
            _resolve_requested_subject_level(resolved_slot.id, body.subject_id, body.level_id, db)
            if resolved_slot else (body.subject_id, body.level_id)
        )
        single_session = TutoringSession(
            booking_id=booking.id,
            teacher_id=teacher_id,
            student_id=student_id,
            subject_id=single_subject_id,
            level_id=single_level_id,
            scheduled_at=dt.datetime.combine(booking_date, slot_time or dt.time(0, 0)),
            mode=body.mode,
            status="scheduled",
        )
        db.add(single_session)
        created_sessions.append(single_session)

    # One SessionValidation row per TutoringSession (1:1) — this is what
    # backs the whole proof-of-attendance/trust-score workflow (see
    # app/services/session_validation.py). Flush first so each session has
    # its generated id.
    db.flush()
    from app.models.session_validation import SessionValidation
    for s in created_sessions:
        db.add(SessionValidation(
            session_id=s.id,
            booking_id=booking.id,
            student_id=student_id,
            teacher_id=teacher_id,
            status="scheduled",
        ))

    try:
        db.commit()
    except IntegrityError:
        # Last-resort DB-level backstop (uq_bookings_active_student_slot,
        # see docs/migrations/067) — the row-lock claim above should already
        # catch this, this only fires if that ever gets bypassed.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Vous avez déjà une réservation en attente ou confirmée pour cette date et cette heure avec ce professeur.",
        )
    db.refresh(booking)

    # Notify the teacher that a booking is waiting for their confirmation —
    # this was previously only fired for the edahabia webhook / manual cash
    # approval paths, so the default cib (Stripe) flow never told the
    # teacher anything (no dashboard badge, nothing in their pending list
    # felt "new"). lesson_booked already has a seeded template (deep_link
    # /teacher/sessions) — see docs/migrations/071_notification_templates_seed.sql.
    from app.services.notification_engine import emit
    student_profile_row = db.get(Profile, student_id)
    emit(
        db, event_type="lesson_booked", user_id=teacher_id,
        context={
            "student_name": student_profile_row.full_name if student_profile_row else "Un élève",
            "date": str(booking.booking_date),
            "time": booking.slot_time.strftime("%H:%M") if booking.slot_time else "",
        },
        data={"booking_id": str(booking.id)},
        dedup_key=f"lesson_booked:{booking.id}",
    )

    return {
        "booking_id": str(booking.id),
        "checkout_url": checkout_url,
        "amount": amount,
        "payment_method": body.payment_method,
    }


@router.get("/bookings/lookup")
async def lookup_booking(
    session_id: Optional[str] = Query(default=None),
    booking_id: Optional[str] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Resolve a booking from the post-payment redirect — used by
    student.payment_.process.tsx instead of trusting the raw `status` query
    param on the redirect URL (which is client-controlled and proves
    nothing). Accepts either the Stripe Checkout Session ID (`session_id`,
    for CIB payments) or a direct `booking_id` (Chargily/edahabia redirects,
    and the no-Stripe-configured CIB fallback, neither of which carry a
    Stripe session id).

    Payment here is auth-only (capture_method=manual — see
    app.services.stripe.create_checkout_session): a completed Stripe
    checkout only means the card was verified and the charge authorized/
    held, not that money moved. The booking stays "pending" and the
    student is only actually charged once the teacher accepts (POST
    /teachers/me/bookings/{id}/accept -> capture_payment_intent). This
    endpoint reports that nuance via `payment_authorized` / `status`
    rather than a blanket "confirmed", so the frontend never overclaims.
    """
    from app.models.booking import Booking

    if not session_id and not booking_id:
        raise HTTPException(status_code=400, detail="session_id ou booking_id requis")

    student_id = UUID(current_user["id"])
    query = select(Booking)
    if session_id:
        query = query.where(Booking.stripe_cs_id == session_id)
    else:
        try:
            query = query.where(Booking.id == UUID(booking_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="booking_id invalide")

    booking = db.exec(query).first()
    if booking is None or booking.student_id != student_id:
        raise HTTPException(status_code=404, detail="Réservation introuvable")

    payment_authorized = booking.payment_method != "cib"  # non-card methods: nothing to "authorize" here
    if booking.payment_method == "cib" and booking.stripe_cs_id:
        try:
            from app.services.stripe import get_checkout_session
            session_data = get_checkout_session(booking.stripe_cs_id)
            payment_authorized = session_data.get("payment_intent_status") in ("requires_capture", "succeeded")
            pi_id = session_data.get("payment_intent")
            if pi_id and not booking.stripe_pi_id:
                booking.stripe_pi_id = pi_id
                db.add(booking)
                db.commit()
        except Exception:
            # Stripe unreachable — don't block the redirect page on a
            # transient error; fall back to whatever we know locally.
            payment_authorized = booking.status != "pending"

    teacher_profile = db.get(Profile, booking.teacher_id)

    return {
        "booking_id": str(booking.id),
        "status": booking.status,
        "payment_authorized": payment_authorized,
        "payment_method": booking.payment_method,
        "formula": booking.formula,
        "mode": booking.mode,
        "amount": booking.amount,
        "currency": booking.currency,
        "kp_reward": booking.kp_reward,
        "booking_date": booking.booking_date.isoformat() if booking.booking_date else None,
        "slot_time": str(booking.slot_time)[:5] if booking.slot_time else None,
        "duration_min": booking.duration_min,
        "subject": booking.subject,
        "teacher_id": str(booking.teacher_id),
        "teacher_name": teacher_profile.full_name if teacher_profile else None,
        "teacher_avatar_url": teacher_profile.avatar_url if teacher_profile else None,
    }


@router.post("/teachers/{teacher_ref}/reviews", status_code=201)
async def submit_teacher_review(
    teacher_ref: str,
    body: ReviewSubmitBody,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Submit a review for a teacher. Requires a completed session with that teacher."""
    tp = _resolve_teacher(db, teacher_ref)
    teacher_id = tp.user_id
    student_id = UUID(current_user["id"])

    # Verify session: must belong to this student-teacher pair and be completed
    session = db.exec(
        select(TutoringSession)
        .where(TutoringSession.id == body.session_id)
        .where(TutoringSession.student_id == student_id)
        .where(TutoringSession.teacher_id == teacher_id)
        .where(TutoringSession.status == "completed")
    ).first()
    if session is None:
        raise HTTPException(status_code=403, detail="Session not found or not eligible for review")

    # One review per session
    existing = db.exec(
        select(Review)
        .where(Review.student_id == student_id)
        .where(Review.session_id == body.session_id)
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="You already reviewed this session")

    review = Review(
        student_id=student_id,
        teacher_id=teacher_id,
        session_id=body.session_id,
        rating=body.rating,
        comment=body.comment,
        status="visible",
    )
    db.add(review)
    db.commit()
    db.refresh(review)

    return {
        "id": str(review.id),
        "rating": review.rating,
        "comment": review.comment,
        "created_at": review.created_at.isoformat(),
    }
