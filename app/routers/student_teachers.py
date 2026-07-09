from __future__ import annotations

import re
import unicodedata
from datetime import date as dt_date, date, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.booking import TutoringSession
from app.models.catalog import Level, Subject, TeacherDiploma, TeacherMode, TeacherSessionType, TeacherSubjectPrice
from app.models.profile import Profile, StudentProfile, TeacherProfile
from app.models.review import Review
from app.models.scheduling import TeacherSlot

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
    student_lesson_format: Optional[str] = None
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
            select(TeacherMode).where(TeacherMode.teacher_id.in_(teacher_ids), TeacherMode.mode == mode)
        ).all()
        mode_set = {r.teacher_id for r in mode_rows}
        teacher_ids = [tid for tid in teacher_ids if tid in mode_set]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    if session_type:
        st_rows = db.exec(
            select(TeacherSessionType).where(
                TeacherSessionType.teacher_id.in_(teacher_ids), TeacherSessionType.type == session_type,
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

    def _dominant_format(fmts: set[str]) -> str:
        clean = fmts - {"both"}
        if not clean or "both" in fmts:
            return "both"
        if clean == {"individual"}:
            return "individual"
        if clean == {"group"}:
            return "group"
        return "both"

    teacher_subjects: dict[UUID, list[str]] = {}
    teacher_level_codes: dict[UUID, list[str]] = {}
    teacher_level_labels: dict[UUID, list[str]] = {}
    teacher_min_price: dict[UUID, int] = {}
    teacher_lesson_formats: dict[UUID, set[str]] = {}
    teacher_subject_formats: dict[UUID, dict[str, set[str]]] = {}

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
        fmt = getattr(r, "lesson_format", "both") or "both"
        teacher_lesson_formats.setdefault(tid, set()).add(fmt)
        if sn:
            teacher_subject_formats.setdefault(tid, {}).setdefault(sn, set()).add(fmt)

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

    if level_list:
        norm_codes: set[str] = set()
        for lv in level_list:
            for code in LEVEL_GROUP_CODES.get(lv, []):
                norm_codes.add(_norm(code))
        teacher_ids = [
            tid for tid in teacher_ids
            if any(_norm(c) in norm_codes for c in teacher_level_codes.get(tid, []))
        ]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    profiles = db.exec(select(Profile).where(Profile.id.in_(teacher_ids))).all()
    prof_map: dict[UUID, Profile] = {p.id: p for p in profiles}

    if wilaya_list:
        norm_wilayas = {_norm(w) for w in wilaya_list}

        def _teacher_serves_any_wilaya(tid: UUID) -> bool:
            tp = tp_map[tid]
            if getattr(tp, "teaching_nationwide", False):
                return True
            tw_arr = getattr(tp, "teaching_wilayas", None) or []
            if tw_arr and any(_norm(w) in norm_wilayas for w in tw_arr):
                return True
            if _norm(tp.teaching_wilaya or "") in norm_wilayas:
                return True
            p = prof_map.get(tid)
            return bool(p and _norm(p.wilaya or "") in norm_wilayas)

        teacher_ids = [tid for tid in teacher_ids if _teacher_serves_any_wilaya(tid)]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    if price_min is not None:
        teacher_ids = [tid for tid in teacher_ids if tp_map[tid].price_per_session >= price_min]
    if price_max is not None:
        teacher_ids = [tid for tid in teacher_ids if tp_map[tid].price_per_session <= price_max]
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

    modes_all = db.exec(select(TeacherMode).where(TeacherMode.teacher_id.in_(teacher_ids))).all()
    teacher_modes: dict[UUID, list[str]] = {}
    for m in modes_all:
        teacher_modes.setdefault(m.teacher_id, []).append(m.mode)

    stypes_all = db.exec(select(TeacherSessionType).where(TeacherSessionType.teacher_id.in_(teacher_ids))).all()
    teacher_stypes: dict[UUID, list[str]] = {}
    for st in stypes_all:
        teacher_stypes.setdefault(st.teacher_id, []).append(st.type)

    def _format_compatible(tid: UUID) -> bool:
        if not student_lesson_format or student_lesson_format == "both":
            return True
        fmts = teacher_lesson_formats.get(tid, {"both"})
        return "both" in fmts or student_lesson_format in fmts

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
        key=lambda tid: (1 if tp_map[tid].sponsored else 0, rank_score(tid)),
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
            sponsored=tp.sponsored,
            badge=tp.badge,
            kp_reward=tp.kp_reward,
            subjects=teacher_subjects.get(tid, []),
            levels=teacher_level_labels.get(tid, []),
            modes=teacher_modes.get(tid, []),
            session_types=teacher_stypes.get(tid, []),
            lesson_formats=sorted(teacher_lesson_formats.get(tid, {"both"})),
            subject_formats={
                sn: _dominant_format(fmts)
                for sn, fmts in teacher_subject_formats.get(tid, {}).items()
            },
            languages=getattr(tp_map.get(tid), "languages", None) or [],
        ))

    return TeacherSearchResponse(items=items, total=len(items))


# ─── Teacher public profile ───────────────────────────────────────────────────

class TeacherSubjectItem(BaseModel):
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
    price_pack5: int = 0
    price_pack8: int = 0


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
        TeacherSubjectItem(name=subj.name, level=lvl.label, price=tsp.price_single)
        for tsp, subj, lvl in sp_rows
    ]

    mode_rows = db.exec(select(TeacherMode).where(TeacherMode.teacher_id == teacher_id)).all()
    modes = [m.mode for m in mode_rows]

    stype_rows = db.exec(select(TeacherSessionType).where(TeacherSessionType.teacher_id == teacher_id)).all()
    session_types = [st.type for st in stype_rows]

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
        bio=tp.bio_long or p.bio,
        headline=tp.headline,
        wilaya=tp.teaching_wilaya or p.wilaya,
        price_per_session=tp.price_per_session,
        kp_reward=tp.kp_reward,
        rating_avg=round(tp.rating_avg, 1),
        reviews_count=tp.reviews_count,
        verified=tp.verified,
        badge=tp.badge,
        experience_years=tp.experience_years,
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

    return [
        TeacherSlotItem(
            id=str(s.id),
            slot_date=s.slot_date.isoformat(),
            start_time=str(s.start_time)[:5],
            end_time=str(s.end_time)[:5],
            mode=s.mode,
            type=s.type,
            max_students=s.max_students,
            price=s.price,
            price_pack5=getattr(s, "price_pack5", 0) or 0,
            price_pack8=getattr(s, "price_pack8", 0) or 0,
        )
        for s in slots
    ]


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
    comment: Optional[str] = None


class BookingBody(BaseModel):
    slot_id: Optional[str] = None
    formula: str = "single"
    mode: str = "online"
    date: Optional[str] = None             # "YYYY-MM-DD" — required for single; inferred from pack_sessions[0] for packs
    slot_time: Optional[str] = None       # "HH:MM"
    end_time: Optional[str] = None        # "HH:MM" (for sub-slot selection)
    duration_min: int = 90
    amount: Optional[int] = None
    payment_method: str = "cib"           # cib | edahabia | transfer | cash
    subject: Optional[str] = None
    comment: Optional[str] = None
    session_type: Optional[str] = None    # override for "both" type slots
    pack_sessions: Optional[List[PackSessionItem]] = None  # for pack5/monthly


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
    - For formula=pack5|monthly: body.pack_sessions must contain all sessions.
    Returns { booking_id, checkout_url|None, amount, payment_method }.
    """
    import datetime as dt
    import json
    from uuid import UUID as _UUID
    from app.config import get_settings
    from app.models.booking import Booking

    settings = get_settings()
    tp = _resolve_teacher(db, teacher_ref)
    teacher_id = tp.user_id
    student_id = _UUID(current_user["id"])

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

    # Determine amount — use slot-level prices when available, else profile default
    resolved_slot = db.get(TeacherSlot, slot_id) if slot_id else None
    if body.amount:
        amount = body.amount
    elif body.formula == "pack5":
        pack5_price = getattr(resolved_slot, "price_pack5", 0) if resolved_slot else 0
        amount = pack5_price if pack5_price > 0 else round(tp.price_per_session * 5 * 0.9)
    elif body.formula == "monthly":
        pack8_price = getattr(resolved_slot, "price_pack8", 0) if resolved_slot else 0
        amount = pack8_price if pack8_price > 0 else round(tp.price_per_session * 8 * 0.92)
    else:
        slot_price = getattr(resolved_slot, "price", 0) if resolved_slot else 0
        amount = slot_price if slot_price > 0 else tp.price_per_session

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

    # For pack5/monthly: reserve all named slots
    if body.pack_sessions:
        for ps in body.pack_sessions:
            if ps.slot_id:
                try:
                    ps_slot_id = _UUID(ps.slot_id)
                    ps_slot = db.get(TeacherSlot, ps_slot_id)
                    if ps_slot and ps_slot.teacher_id == teacher_id and ps_slot.status == "open":
                        ps_slot.status = "booked"
                        db.add(ps_slot)
                except (ValueError, Exception):
                    pass
    elif slot_id:
        # Single booking: reserve the slot
        single_slot = db.get(TeacherSlot, slot_id)
        if single_slot:
            single_slot.status = "booked"
            db.add(single_slot)

    db.commit()
    db.refresh(booking)

    return {
        "booking_id": str(booking.id),
        "checkout_url": checkout_url,
        "amount": amount,
        "payment_method": body.payment_method,
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
